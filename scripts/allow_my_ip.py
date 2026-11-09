#!/usr/bin/env python3
"""allow_my_ip.py —— 把本机当前公网出口 IP 写进实例白名单的专用分组。

家宽/办公网出口 IP 经常会变，一变 Redis 就连不上（表现为页面一直转圈、
脚本报 Timeout connecting to server）。这个脚本一键把当前 IP 写进白名单。

两条安全约束（以前的版本没有）：
  1. 出口 IP 只走 HTTPS 探测，并且至少两个来源结果一致才采信。
     明文 HTTP 可能被中间人篡改，把别人的 IP 写进你的白名单。
  2. 只改专用分组（默认 dev_auto），默认「覆盖」该分组。
     以前是往 default 分组无限追加，换一次网络多一个 IP，旧 IP 永远不清理。
     覆盖专用分组后，旧 IP 自动失效，其它分组（含 default）完全不受影响。

用法：
    python allow_my_ip.py                     # 探测出口 IP，覆盖 dev_auto 分组
    python allow_my_ip.py --ip 1.2.3.4        # 手动指定（探测结果不一致时用）
    python allow_my_ip.py --group dev_mac     # 每台机器一个分组，互不覆盖
    python allow_my_ip.py --append            # 追加到分组而不是覆盖（多台机器共用一个分组时）
    python allow_my_ip.py --show              # 只看当前白名单，不改

注意：这是写操作，需要 AccessKey 具备 AliyunKvstoreFullAccess（或自定义的改白名单权限）。
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import shutil
import subprocess
import sys
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 让脚本能 import tair 包

# 只用 HTTPS 来源；curl 加 --proto/--proto-redir =https，连重定向到 http 也不跟
IP_ENDPOINTS = (
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://checkip.amazonaws.com",
    "https://ifconfig.me/ip",
)
MIN_AGREE = 2  # 至少这么多个来源给出同一个 IP 才采信

DEFAULT_GROUP = "dev_auto"
# 绝不覆盖的分组：default 是默认分组（可能放着 ECS 等内网地址），其余为阿里云服务自动生成
PROTECTED_GROUPS = frozenset({"default", "ali_dms_group", "hdm_security_ips"})
GROUP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,118}[a-z0-9]$")  # 2~120 位，小写字母开头


# ------------------------------------------------------------------ 出口 IP 探测
def _fetch(url: str) -> str:
    """优先用 curl：本机装了代理/分流时，Python 直连看到的出口 IP 可能和浏览器不一致。"""
    if shutil.which("curl"):
        try:
            out = subprocess.run(  # noqa: S603 - 固定参数，没有用户输入
                [
                    "curl",
                    "-4",
                    "-s",
                    "-L",
                    "--proto",
                    "=https",
                    "--proto-redir",
                    "=https",
                    "--max-time",
                    "6",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return out.stdout.strip()
        except Exception:  # noqa: BLE001
            return ""
    try:
        with urllib.request.urlopen(url, timeout=6) as resp:  # noqa: S310 - 固定的 https 地址
            if not resp.geturl().startswith("https://"):
                return ""  # 被重定向到了明文地址，不采信
            return resp.read(64).decode(errors="ignore").strip()
    except Exception:  # noqa: BLE001
        return ""


def parse_public_ipv4(text: str) -> str | None:
    """只接受合法的公网 IPv4；内网 / 保留地址 / 乱码一律视为无效。"""
    try:
        ip = ipaddress.IPv4Address((text or "").strip())
    except ValueError:
        return None
    return str(ip) if ip.is_global else None


def detect_ip(fetch: Callable[[str], str] = _fetch, endpoints: Iterable[str] = IP_ENDPOINTS) -> str:
    """问遍所有来源，要求结果唯一且至少 MIN_AGREE 个来源一致，否则拒绝自动写入。"""
    answers: dict[str, str] = {}
    for url in endpoints:
        ip = parse_public_ipv4(fetch(url))
        if ip:
            answers[url] = ip
    votes = Counter(answers.values())
    if not votes:
        raise SystemExit("[x] 取不到本机公网 IP，请用 --ip 手动指定")
    if len(votes) > 1:
        detail = "\n".join(f"      {ip:<16} ← {url}" for url, ip in answers.items())
        raise SystemExit(
            "[x] 各探测来源给出的出口 IP 不一致，不自动写入：\n"
            f"{detail}\n"
            "    可能开了代理分流，也可能有来源被篡改。确认后用 --ip 手动指定。"
        )
    ip, count = votes.most_common(1)[0]
    if count < MIN_AGREE:
        raise SystemExit(
            f"[x] 只有 {count} 个来源返回了结果（{ip}），无法交叉核对；稍后重试或用 --ip 手动指定"
        )
    return ip


def parse_manual_ip(text: str) -> str:
    """--ip 允许单个 IPv4 或 CIDR；拒绝 0.0.0.0/0 这类放行全网的写法。"""
    try:
        net = ipaddress.IPv4Network(text.strip(), strict=False)
    except ValueError:
        raise SystemExit(f"[x] --ip 不是合法的 IPv4 地址或网段：{text!r}") from None
    if net.prefixlen < 24:
        raise SystemExit(f"[x] 网段 {net} 太大（前缀小于 /24），拒绝写入白名单")
    return str(net.network_address) if net.prefixlen == 32 else str(net)


def check_group(group: str, mode: str) -> None:
    if not GROUP_NAME_RE.match(group):
        raise SystemExit(f"[x] 分组名 {group!r} 不合法：2~120 位小写字母/数字/下划线，字母开头")
    if group in PROTECTED_GROUPS and mode == "Cover":
        raise SystemExit(
            f"[x] 拒绝覆盖分组 {group}：它可能放着别的地址（ECS / 阿里云服务）。"
            "请换一个专用分组，或加 --append 只追加"
        )


# ------------------------------------------------------------------ OpenAPI
def show_whitelist(client, instance_id: str) -> None:
    from alibabacloud_r_kvstore20150101 import models as kv_models
    from alibabacloud_tea_util import models as util_models

    resp = client.describe_security_ips_with_options(
        kv_models.DescribeSecurityIpsRequest(instance_id=instance_id),
        util_models.RuntimeOptions(),
    )
    groups = resp.body.to_map().get("SecurityIpGroups", {}).get("SecurityIpGroup", [])
    for group in groups:
        print(f"  {group.get('SecurityIpGroupName'):<18} {group.get('SecurityIpList')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="把本机出口 IP 写进 Tair 实例白名单的专用分组")
    parser.add_argument("--ip", help="手动指定 IP 或网段（不小于 /24），默认自动探测")
    parser.add_argument("--instance-id", help="实例 ID，默认取 .env 里的 TAIR_INSTANCE_ID 或第一台")
    parser.add_argument("--group", default=DEFAULT_GROUP, help=f"白名单分组，默认 {DEFAULT_GROUP}")
    parser.add_argument("--append", action="store_true", help="追加到分组，而不是覆盖该分组")
    parser.add_argument("--show", action="store_true", help="只显示白名单，不修改")
    args = parser.parse_args()

    from alibabacloud_r_kvstore20150101 import models as kv_models
    from alibabacloud_tea_util import models as util_models

    from tair import config
    from tair.openapi import build_client, describe_instances_overview, explain_error

    mode = "Append" if args.append else "Cover"
    if not args.show:
        check_group(args.group, mode)

    settings = config.load_settings()
    if not settings.has_access_key:
        print(config.MISSING_AK_HINT)
        return 1

    instance_id = args.instance_id or settings.instance_id
    if not instance_id:
        items = describe_instances_overview(settings).get("Instances") or []
        if not items:
            print("[x] 该地域没有实例")
            return 1
        instance_id = items[0].get("InstanceId", "")

    client = build_client(settings)
    print(f"实例: {instance_id}   地域: {settings.region_id}")

    if args.show:
        print("当前白名单:")
        show_whitelist(client, instance_id)
        return 0

    ip = parse_manual_ip(args.ip) if args.ip else detect_ip()
    print(f"本机出口 IP: {ip}")

    try:
        client.modify_security_ips_with_options(
            kv_models.ModifySecurityIpsRequest(
                instance_id=instance_id,
                security_ips=ip,
                security_ip_group_name=args.group,
                modify_mode=mode,
            ),
            util_models.RuntimeOptions(),
        )
        if mode == "Cover":
            print(f"[✓] 分组 {args.group} 已设为 {ip}（该分组里的旧 IP 已失效，其它分组不受影响）")
        else:
            print(f"[✓] 已追加 {ip} 到分组 {args.group}")
    except Exception as exc:  # noqa: BLE001
        code, message, _ = explain_error(exc)
        if "Duplicate" in str(code) or "Duplicate" in message:
            print(f"[i] {ip} 本来就在分组 {args.group} 里，无需改动")
        else:
            print(f"[x] 失败 {code}: {message[:200]}")
            return 1

    print("当前白名单:")
    show_whitelist(client, instance_id)
    print(
        "\n提示: 旧版本脚本把 IP 追加在 default 分组里，那些旧 IP 不会被自动清理；"
        "\n      确认不再需要后，请到控制台「白名单设置」里手动删掉。"
        "\n下一步: 刷新看板，或重跑 02_redis_commands.py 验证连通性"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
