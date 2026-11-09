#!/usr/bin/env python3
"""07_oss_cors.py —— 给 bucket 配置跨域规则，让看板能「浏览器直传 OSS」。

浏览器直接 PUT 到 https://<bucket>.oss-xxx.aliyuncs.com 属于跨域请求，
bucket 上没有放行看板来源（如 http://127.0.0.1:8080）的 CORS 规则时，浏览器会拦下来。
看板此时会自动退回「服务器中转」，能用但慢；配上这条规则就能直传。

用法：
    python scripts/07_oss_cors.py                       # 只读：看当前规则，以及要加的规则
    python scripts/07_oss_cors.py --apply               # 追加规则（保留 bucket 上已有的其它规则）
    python scripts/07_oss_cors.py --origin http://localhost:9000 --apply   # ssh -L 用了别的端口时

注意：PutBucketCors 是整体覆盖，本脚本会先读出已有规则再合并写回，不会冲掉别的规则。
需要 AccessKey 有 oss:PutBucketCors / oss:GetBucketCors 权限。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import alibabacloud_oss_v2 as oss

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 让脚本能 import tair 包

from tair import config  # noqa: E402
from tair import oss as oss_helper  # noqa: E402
from tair.console import line, section  # noqa: E402


def default_origins() -> list[str]:
    port = os.getenv("WEB_PORT", "8080")
    return [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]


def current_rules(client: oss.Client, bucket: str) -> list:
    try:
        result = client.get_bucket_cors(oss.GetBucketCorsRequest(bucket=bucket))
    except Exception as exc:  # noqa: BLE001
        if "NoSuchCORSConfiguration" in str(exc) or "404" in str(exc):
            return []  # bucket 还没有任何 CORS 规则
        raise
    conf = result.cors_configuration
    return list(conf.cors_rules or []) if conf else []


def covers(rule, origins: list[str]) -> bool:
    """已有规则是否已经放行了这些来源的 PUT（含通配 *）。"""
    allowed = set(rule.allowed_origins or [])
    methods = {m.upper() for m in (rule.allowed_methods or [])}
    return "PUT" in methods and ("*" in allowed or set(origins) <= allowed)


def main() -> int:
    parser = argparse.ArgumentParser(description="为看板直传配置 bucket CORS")
    parser.add_argument(
        "--origin", action="append", help="放行的来源，可重复；默认 127.0.0.1/localhost 的 WEB_PORT"
    )
    parser.add_argument("--apply", action="store_true", help="真正写入（默认只读）")
    args = parser.parse_args()

    settings = config.load_settings()
    if not (settings.has_oss and settings.oss_bucket):
        print("[x] 需要在 .env 里配置 AccessKey 和 OSS_BUCKET", file=sys.stderr)
        return 2

    origins = args.origin or default_origins()
    bucket = settings.oss_bucket
    client = oss_helper.shared_client(settings)

    section(f"bucket {bucket} 当前的 CORS 规则")
    rules = current_rules(client, bucket)
    if not rules:
        line("（无）", "")
    for i, rule in enumerate(rules, 1):
        line(f"规则 {i} 来源", ", ".join(rule.allowed_origins or []))
        line("       方法", ", ".join(rule.allowed_methods or []))

    if any(covers(rule, origins) for rule in rules):
        print(f"\n[✓] 已放行 {', '.join(origins)} 的 PUT，无需修改")
        return 0

    new_rule = oss.CORSRule(
        allowed_origins=origins,
        allowed_methods=["PUT"],
        allowed_headers=["*"],  # 签名 URL 要求浏览器带上 Content-Type 等签名头
        expose_headers=["ETag", "x-oss-request-id"],
        max_age_seconds=600,
    )
    section("将追加的规则")
    line("来源", ", ".join(origins))
    line("方法 / 请求头", "PUT / *")

    if not args.apply:
        print("\n只读模式，未修改。确认无误后加 --apply 写入。")
        return 0

    client.put_bucket_cors(
        oss.PutBucketCorsRequest(
            bucket=bucket,
            cors_configuration=oss.CORSConfiguration(cors_rules=[*rules, new_rule]),
        )
    )
    print(f"\n[✓] 已写入，共 {len(rules) + 1} 条规则。刷新看板再上传即可走直传。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
