#!/usr/bin/env python3
"""00_doctor.py —— 配好 AccessKey 之后先跑这个，一次性自检。

检查项：
    1）Python 版本 / 依赖包是否装齐
    2）AccessKey 是否读到
    3）OpenAPI 是否真的调得通（只读接口，不会动任何资源）
    4）Redis 实例是否连得上（没配 REDIS_HOST 就跳过）

用法：
    python 00_doctor.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 让脚本能 import tair 包

from tair import config

OK = "  [✓]"
NO = "  [x]"
SKIP = "  [-]"


def check_packages() -> bool:
    missing = []
    for mod, pkg in (
        ("redis", "redis"),
        ("alibabacloud_r_kvstore20150101", "alibabacloud_r_kvstore20150101"),
        ("alibabacloud_tea_openapi", "alibabacloud-tea-openapi"),
    ):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"{NO} 缺少依赖: {', '.join(missing)}")
        print("       执行: pip install -r requirements.txt")
        return False
    print(f"{OK} 依赖齐全")
    return True


def check_openapi(settings: config.Settings) -> bool:
    from tair.openapi import describe_instances_overview, explain_error

    if not settings.has_access_key:
        print(f"{NO} 没有读到 AccessKey")
        print(config.MISSING_AK_HINT)
        return False

    print(f"{OK} AccessKey 已读到: {settings.masked_ak()}  地域: {settings.region_id}")
    try:
        data = describe_instances_overview(settings)
    except Exception as exc:  # noqa: BLE001 - 演示脚本，统一兜住打印
        code, message, tip = explain_error(exc)
        print(f"{NO} OpenAPI 调用失败  错误码: {code or '(无)'}")
        print(f"       {message[:200]}")
        if tip:
            print(f"       建议: {tip}")
        return False

    count = data.get("TotalCount", 0)
    print(
        f"{OK} OpenAPI 调用成功，该地域共 {count} 个实例（RequestId: {data.get('RequestId', '?')}）"
    )
    return True


def check_redis(settings: config.Settings) -> bool:
    if not settings.has_redis:
        print(f"{SKIP} 未配置 REDIS_HOST，跳过数据面检查（只跑 OpenAPI 可以忽略）")
        return True

    from tair.redis_conn import build_client, server_summary

    client = build_client(settings)
    try:
        client.ping()
        summary = server_summary(client)
    except Exception as exc:  # noqa: BLE001
        print(f"{NO} Redis 连接失败: {exc}")
        print(config.CONNECTION_HINT)
        return False

    print(f"{OK} Redis 连接成功  版本 {summary['redis_version']}  角色 {summary['role']}")
    return True


def main() -> int:
    print("=" * 64)
    print(" 阿里云 Tair（兼容 Redis）示例项目 · 环境自检")
    print("=" * 64)

    settings = config.load_settings()
    packages_ok = check_packages()
    openapi_ok = check_openapi(settings) if packages_ok else False
    redis_ok = check_redis(settings) if packages_ok else False

    print("-" * 64)
    if openapi_ok and redis_ok:
        print(" 全部通过，可以继续跑 02 / 03 了。")
        return 0
    print(" 还有检查项没通过，按上面提示修一下再跑。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
