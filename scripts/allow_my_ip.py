#!/usr/bin/env python3
"""allow_my_ip.py —— 把本机当前公网出口 IP 加进实例白名单。

家宽/办公网出口 IP 经常会变，一变 Redis 就连不上（表现为页面一直转圈、
脚本报 Timeout connecting to server）。这个脚本一键把当前 IP 追加进白名单。

用法：
    python allow_my_ip.py                 # 自动探测出口 IP 并追加
    python allow_my_ip.py --ip 1.2.3.4    # 手动指定
    python allow_my_ip.py --show          # 只看当前白名单，不改

注意：这是写操作，需要 AccessKey 具备 AliyunKvstoreFullAccess（或自定义的改白名单权限）。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from alibabacloud_r_kvstore20150101 import models as kv_models
from alibabacloud_tea_util import models as util_models

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 让脚本能 import tair 包

from tair import config
from tair.openapi import build_client, explain_error

IP_ENDPOINTS = (
    "http://ipv4.icanhazip.com",
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
)


def _via_curl() -> str:
    """优先用 curl：本机装了代理/分流时，Python 直连看到的出口 IP 可能和浏览器不一致。"""
    if not shutil.which("curl"):
        return ""
    for url in IP_ENDPOINTS:
        try:
            out = subprocess.run(  # noqa: S603 - 固定参数，没有用户输入
                ["curl", "-4", "-s", "--max-time", "6", url],
                capture_output=True,
                text=True,
                timeout=10,
            )
            ip = out.stdout.strip()
            if ip.count(".") == 3:
                return ip
        except Exception:  # noqa: BLE001
            continue
    return ""


def _via_urllib() -> str:
    for url in IP_ENDPOINTS:
        try:
            with urllib.request.urlopen(url, timeout=6) as resp:  # noqa: S310
                ip = resp.read().decode().strip()
            if ip.count(".") == 3:
                return ip
        except Exception:  # noqa: BLE001
            continue
    return ""


def current_ip() -> str:
    ip = _via_curl() or _via_urllib()
    if not ip:
        raise SystemExit("[x] 取不到本机公网 IP，请用 --ip 手动指定")
    return ip


def show_whitelist(client, instance_id: str) -> None:
    resp = client.describe_security_ips_with_options(
        kv_models.DescribeSecurityIpsRequest(instance_id=instance_id),
        util_models.RuntimeOptions(),
    )
    groups = resp.body.to_map().get("SecurityIpGroups", {}).get("SecurityIpGroup", [])
    for group in groups:
        print(f"  {group.get('SecurityIpGroupName'):<18} {group.get('SecurityIpList')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="把本机出口 IP 加进 Tair 实例白名单")
    parser.add_argument("--ip", help="手动指定 IP，默认自动探测")
    parser.add_argument("--instance-id", help="实例 ID，默认取 .env 里的 TAIR_INSTANCE_ID 或第一台")
    parser.add_argument("--show", action="store_true", help="只显示白名单，不修改")
    args = parser.parse_args()

    settings = config.load_settings()
    if not settings.has_access_key:
        print(config.MISSING_AK_HINT)
        return 1

    instance_id = args.instance_id or settings.instance_id
    if not instance_id:
        from tair.openapi import describe_instances_overview

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

    ip = args.ip or current_ip()
    print(f"本机出口 IP: {ip}")

    try:
        client.modify_security_ips_with_options(
            kv_models.ModifySecurityIpsRequest(
                instance_id=instance_id, security_ips=ip, modify_mode="Append"
            ),
            util_models.RuntimeOptions(),
        )
        print(f"[✓] 已追加 {ip}")
    except Exception as exc:  # noqa: BLE001
        code, message, _ = explain_error(exc)
        if "Duplicate" in str(code) or "Duplicate" in message:
            print(f"[i] {ip} 本来就在白名单里，无需改动")
        else:
            print(f"[x] 失败 {code}: {message[:200]}")
            return 1

    print("当前白名单:")
    show_whitelist(client, instance_id)
    print("\n下一步: 刷新看板，或重跑 02_redis_commands.py 验证连通性")
    return 0


if __name__ == "__main__":
    sys.exit(main())
