#!/usr/bin/env python3
"""01_openapi_list_instances.py —— 完全对应官方文档示例：查询实例列表。

对应接口：DescribeInstancesOverview（查询实例概览）
权限策略：AliyunKvstoreReadOnlyAccess
依赖配置：ALIBABA_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET / REGION

用法：
    python 01_openapi_list_instances.py
    python 01_openapi_list_instances.py --region cn-beijing
    python 01_openapi_list_instances.py --instance-id r-bp1xxxxxxxx
    python 01_openapi_list_instances.py --raw     # 打印文档里那种原始 JSON
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 让脚本能 import tair 包

from tair import config
from tair.openapi import describe_instances_overview, explain_error


def print_table(instances: list[dict]) -> None:
    """按行打印，字段名和文档输出里的保持一致。"""
    for idx, item in enumerate(instances, start=1):
        status = item.get("InstanceStatus", "")
        flag = "正常" if status == "Normal" else status
        print(
            f"  {idx}. {item.get('InstanceId', '?')}  [{flag}]  "
            f"Redis {item.get('EngineVersion', '?')}  "
            f"{item.get('ArchitectureType', '?')}  "
            f"{item.get('InstanceClass', '?')}"
        )
        print(f"     名称: {item.get('InstanceName') or '(未命名)'}")
        print(
            f"     地址: {item.get('ConnectionDomain', '?')}:6379   "
            f"规格: {item.get('Capacity', '?')} MB   {item.get('ChargeType', '?')}"
        )
        print(f"     可用区: {item.get('ZoneId', '?')}   创建时间: {item.get('CreateTime', '?')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="调用 DescribeInstancesOverview 查询实例列表")
    parser.add_argument("--region", help="覆盖地域 ID，例如 cn-hangzhou")
    parser.add_argument("--instance-id", help="只看某个实例，多个用逗号分隔")
    parser.add_argument("--raw", action="store_true", help="打印接口原始返回 JSON")
    args = parser.parse_args()

    settings = config.load_settings()
    if args.region:
        settings = replace(settings, region_id=args.region)

    if not settings.has_access_key:
        print(config.MISSING_AK_HINT)
        return 1

    print(f"地域: {settings.region_id}    身份: {settings.masked_ak()}")
    print("-" * 68)

    try:
        data = describe_instances_overview(settings, args.instance_id or "")
    except Exception as exc:  # noqa: BLE001 - 演示脚本统一兜住
        code, message, tip = explain_error(exc)
        print(f"[x] 调用失败  错误码: {code or '(无)'}")
        print(f"    {message[:300]}")
        if tip:
            print(f"    建议: {tip}")
        # 排查小抄
        print("\n    还不行就检查：AK 是否属于该账号 / 地域是否写对 / RAM 是否授权")
        return 1

    instances = data.get("Instances", [])
    print(f"共 {data.get('TotalCount', 0)} 个实例  (RequestId: {data.get('RequestId', '?')})\n")

    if not instances:
        print("  该地域下没有实例。可以换个 --region 试试，或者先去控制台创建一台。")
        return 0

    print_table(instances)

    if args.raw:
        print("\n----- 原始返回（和文档里的输出一致）-----")
        print(json.dumps(data, ensure_ascii=False, indent=4))

    print("\n下一步：把上面某个实例的「地址」和密码填进 .env，跑 02_redis_commands.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
