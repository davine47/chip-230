#!/usr/bin/env python3
"""03_end2end.py —— 控制面 + 数据面串起来跑：查实例 → 拿连接地址 → 读写验证。

流程（这就是阿里云 Redis 的完整「使用方法」）：
    1. 用 AccessKey 调 OpenAPI 拿到实例列表        （控制面，管资源）
    2. 再用实例 ID 查详情，得到连接地址/端口/白名单  （控制面，拿参数）
    3. 用地址 + 密码走 Redis 协议读写数据           （数据面，跑业务）

用法：
    python 03_end2end.py
    python 03_end2end.py --instance-id r-bp1xxxxxxxx
    python 03_end2end.py --host r-bp1xxxx.redis.rds.aliyuncs.com   # 手动指定公网地址
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 让脚本能 import tair 包

from tair import config
from tair.console import line
from tair.openapi import (
    describe_instance_attribute,
    describe_instances_overview,
    explain_error,
)
from tair.redis_conn import build_client


def pick_instance(instances: list[dict], wanted_id: str = "") -> dict:
    """挑一台实例：指定了 ID 就按 ID，否则优先取状态正常的第一台。"""
    if not instances:
        raise RuntimeError("该地域下没有实例")
    if wanted_id:
        for item in instances:
            if item.get("InstanceId") == wanted_id:
                return item
        raise RuntimeError(f"地域内没找到实例 {wanted_id}")
    for item in instances:
        if item.get("InstanceStatus") == "Normal":
            return item
    return instances[0]


def conn_from_attribute(attr: dict) -> tuple[str, int, bool]:
    """从 DescribeInstanceAttribute 的返回里取 (地址, 端口, 是否需要 TLS)。

    注意：这里拿到的是「内网地址」，只能在同 VPC 的 ECS 上连；
    本机调试要去控制台申请公网地址，然后用 --host 或 REDIS_HOST 覆盖。
    """
    host = attr.get("ConnectionDomain", "")
    port = int(attr.get("Port", 6379) or 6379)
    if not host:
        raise RuntimeError("实例没有返回连接地址，可能还在创建中或未开放网络访问")
    return host, port, False


def print_instance(attr: dict) -> None:
    rows = [
        ("实例 ID", attr.get("InstanceId")),
        ("实例名称", attr.get("InstanceName") or "(未命名)"),
        ("运行状态", attr.get("InstanceStatus")),
        ("引擎版本", attr.get("EngineVersion")),
        ("架构", attr.get("ArchitectureType")),
        ("规格", attr.get("InstanceClass")),
        ("容量", f"{attr.get('Capacity')} MB"),
        ("连接地址", f"{attr.get('ConnectionDomain')}:{attr.get('Port')}"),
        ("网络类型", attr.get("NetworkType")),
        ("可用区", attr.get("ZoneId")),
        ("白名单", attr.get("SecurityIPList") or "(空)"),
    ]
    for label, value in rows:
        line(label, value, width=14)


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenAPI + Redis 端到端示例")
    parser.add_argument("--instance-id", help="指定实例 ID，默认取第一台正常实例")
    parser.add_argument("--host", help="手动覆盖连接地址（比如公网地址）")
    parser.add_argument("--keep", action="store_true", help="保留写入的示例 Key")
    args = parser.parse_args()

    settings = config.load_settings()
    if not settings.has_access_key:
        print(config.MISSING_AK_HINT)
        return 1

    # ---------- 第 1 步：控制面，查实例列表 ----------
    print(f"\n[1/4] 调 OpenAPI 查询实例列表（地域 {settings.region_id}）...")
    try:
        overview = describe_instances_overview(settings)
    except Exception as exc:  # noqa: BLE001
        code, message, tip = explain_error(exc)
        print(f"      [x] 失败  错误码: {code or '(无)'}\n      {message[:300]}")
        if tip:
            print(f"      建议: {tip}")
        return 1

    instances = overview.get("Instances", [])
    target = pick_instance(instances, args.instance_id or settings.instance_id)
    print(f"      共 {overview.get('TotalCount', 0)} 个实例，选中 {target.get('InstanceId')}")

    # ---------- 第 2 步：控制面，查详情拿连接信息 ----------
    print("\n[2/4] 调 OpenAPI 查询实例详情...")
    try:
        attr = describe_instance_attribute(settings, target["InstanceId"])
    except Exception as exc:  # noqa: BLE001
        code, message, tip = explain_error(exc)
        print(f"      [x] 失败  错误码: {code or '(无)'}\n      {message[:300]}")
        if tip:
            print(f"      建议: {tip}")
        return 1
    print()
    print_instance(attr)

    # ---------- 第 3 步：数据面，连上去 ----------
    host, port, _ = conn_from_attribute(attr)
    if args.host:
        host, port = args.host, settings.redis_port
    elif settings.redis_host:
        host, port = settings.redis_host, settings.redis_port
    else:
        print(
            "\n    [i] 用的是 OpenAPI 返回的内网地址，只有同 VPC 的 ECS 能连。\n"
            "        本机调试请先在控制台申请「公网连接地址」，再加 --host 参数，\n"
            "        或把公网地址写进 .env 的 REDIS_HOST。"
        )

    if not settings.redis_password:
        print("\n    [x] 还没有配置 REDIS_PASSWORD（实例密码）")
        print(config.MISSING_REDIS_HINT)
        return 1

    print(f"\n[3/4] 连接 {host}:{port} ...")
    client = build_client(settings, host=host, port=port)
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001
        print(f"      [x] 连接失败: {exc}")
        print(config.CONNECTION_HINT)
        return 1
    print("      [✓] PING 成功")

    # ---------- 第 4 步：读写验证 ----------
    print("\n[4/4] 读写验证...")
    key = "{tair:demo}:end2end:health"
    payload = f"hello-{int(time.time())}"
    try:
        client.set(key, payload, ex=300)
        got = client.get(key)
        print(f"      SET {key} = {payload}  (EX 300)")
        print(f"      GET {key} -> {got}")
        print(f"      TTL -> {client.ttl(key)} 秒")
        if args.keep:
            print("      (--keep) 已保留，可自行查看")
        else:
            client.delete(key)
            print("      已删除测试 Key")
    except Exception as exc:  # noqa: BLE001
        print(f"      [x] 读写失败: {exc}")
        return 1
    finally:
        client.close()

    print("\n链路验证通过。用 redis-cli 连同一台实例：")
    print(f"    redis-cli -h {host} -p {port} -a '<你的密码>' --no-auth-warning")
    print("    (集群架构请用控制台的连接地址，多 Key 命令记得用 Hash Tag 保证同 Slot)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
