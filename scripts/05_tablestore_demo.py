#!/usr/bin/env python3
"""05_tablestore_demo.py —— Tablestore（表格存储）宽表模型：建表 / 写入 / 查询 / 更新 / 删除。

对应文档：https://help.aliyun.com/document_detail/27293.html

表设计（oss_documents，用来索引 OSS 上的文档地址）：

    ┌──────────────┬──────────┬─────────────────────────────────────────────┐
    │ 列           │ 类型     │ 说明                                        │
    ├──────────────┼──────────┼─────────────────────────────────────────────┤
    │ id           │ STRING   │ 主键（分区键），写入时给 UUID               │
    │ name         │ STRING   │ 文档名                                       │
    │ url          │ STRING   │ OSS 对象地址                                 │
    │ describe     │ STRING   │ 描述                                         │
    │ category     │ STRING   │ 分类                                         │
    │ create_time  │ INTEGER  │ 创建时间，毫秒时间戳                         │
    └──────────────┴──────────┴─────────────────────────────────────────────┘

Tablestore 只需要定义主键，属性列写入时带上即可，不用提前建列。

用法：
    python scripts/05_tablestore_demo.py              # 完整流程，结束删除演示行
    python scripts/05_tablestore_demo.py --keep       # 保留写入的行
    python scripts/05_tablestore_demo.py --create     # 表不存在时先创建
    python scripts/05_tablestore_demo.py --list       # 只列表和表结构
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

from tablestore import (
    INF_MAX,
    INF_MIN,
    CapacityUnit,
    Condition,
    ReservedThroughput,
    Row,
    RowExistenceExpectation,
    TableMeta,
    TableOptions,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 让脚本能 import tair 包

from tair import config  # noqa: E402
from tair import tablestore as ts_helper  # noqa: E402
from tair.console import line, section  # noqa: E402


def ensure_table(client, table: str) -> None:
    """表不存在就按约定结构建一张（主键 id 字符串，按量付费）。"""
    if table in client.list_table():
        return
    meta = TableMeta(table, [("id", "STRING")])
    client.create_table(
        meta,
        TableOptions(time_to_live=-1, max_version=1),  # 永不过期，保留 1 个版本
        ReservedThroughput(CapacityUnit(0, 0)),  # 0/0 = 按量付费（容量计费）
    )
    line("已创建表", table)


def main() -> int:
    parser = argparse.ArgumentParser(description="Tablestore 宽表模型演示")
    parser.add_argument("--create", action="store_true", help="表不存在时自动创建")
    parser.add_argument("--list", action="store_true", help="只列表和表结构，不写数据")
    parser.add_argument("--keep", action="store_true", help="保留写入的演示行")
    parser.add_argument("--name", default="hello.txt", help="文档名")
    parser.add_argument(
        "--url",
        default="https://davine-datanode-std.oss-cn-beijing.aliyuncs.com/hello.txt",
        help="OSS 对象地址",
    )
    parser.add_argument("--category", default="demo", help="分类")
    args = parser.parse_args()

    settings = config.load_settings()
    if not settings.has_tablestore:
        print("[!] 还没配 OTS_INSTANCE，请在 .env 里填实例名")
        return 1

    table = settings.ts_table
    section("0. 连接实例")
    client = ts_helper.build_client(settings)
    line("实例", settings.ts_instance)
    line("Endpoint", settings.ts_endpoint_or_default)
    line("表", table)
    line("凭证", settings.masked_ak())

    section("1. list_table —— 实例下的所有表")
    tables = client.list_table()
    line("表数量", len(tables))
    for name in tables:
        line("  " + name, "ok")

    if args.create:
        ensure_table(client, table)

    if table not in tables:
        print(f"\n[x] 表 {table} 不存在，加 --create 自动建表")
        return 1

    section("2. describe_table —— 表结构")
    info = client.describe_table(table)
    line("主键", info.table_meta.schema_of_primary_key)
    line("预定义列", info.table_meta.defined_columns)
    line("TTL", f"{info.table_options.time_to_live}（-1 表示永不过期）")
    cu = info.reserved_throughput_details.capacity_unit
    line("预留 CU（读/写）", f"{cu.read} / {cu.write}（0/0 = 按量付费）")

    if args.list:
        return 0

    doc_id = uuid.uuid4().hex
    try:
        section("3. put_row —— 写入一行（属性列随写随建）")
        now_ms = int(time.time() * 1000)
        row = Row(
            [("id", doc_id)],
            [
                ("name", args.name),
                ("url", args.url),
                ("describe", "OSS 上传的文档，由 05_tablestore_demo.py 写入"),
                ("category", args.category),
                ("create_time", now_ms),
            ],
        )
        consumed, _ = client.put_row(table, row, Condition(RowExistenceExpectation.IGNORE))
        line("put_row", f"id={doc_id[:12]}…  消耗 CU={consumed.read}/{consumed.write}")

        section("4. get_row —— 按主键读回")
        consumed, got, _ = client.get_row(
            table,
            [("id", doc_id)],
            columns_to_get=["name", "url", "describe", "category", "create_time"],
        )
        data = ts_helper.row_to_dict(got)
        for key in ("name", "url", "describe", "category", "create_time"):
            line(f"  {key}", data.get(key))
        line("消耗 CU", f"{consumed.read}/{consumed.write}")

        section("5. get_range —— 扫描（这里取前 10 行）")
        consumed, _next_pk, rows, _token = client.get_range(
            table,
            "FORWARD",
            [("id", INF_MIN)],
            [("id", INF_MAX)],
            columns_to_get=["name", "category", "create_time"],
            limit=10,
        )
        line("返回行数", len(rows))
        for item in rows[:5]:
            attrs = ts_helper.row_to_dict(item)
            line(
                f"  {ts_helper.primary_key_dict(item)['id'][:8]}…",
                f"{attrs.get('name')} / {attrs.get('category')}",
            )

        section("6. update_row —— 只改 category，其余列不动")
        # 注意：UpdateRow 的属性列是「操作字典」，不是普通 dict
        upd = Row([("id", doc_id)], {"put": [("category", "demo-updated")]})
        consumed, _ = client.update_row(table, upd, Condition(RowExistenceExpectation.EXPECT_EXIST))
        line("update_row", f"消耗 CU={consumed.read}/{consumed.write}")
        _, again, _ = client.get_row(table, [("id", doc_id)], columns_to_get=["category"])
        line("改后 category", ts_helper.row_to_dict(again).get("category"))

        section("7. 清理")
        if args.keep:
            line("保留演示行（--keep）", f"id={doc_id}")
        else:
            consumed, _ = client.delete_row(
                table, [("id", doc_id)], Condition(RowExistenceExpectation.EXPECT_EXIST)
            )
            line("delete_row", f"消耗 CU={consumed.read}/{consumed.write}")
            _, gone, _ = client.get_row(table, [("id", doc_id)])
            line("删除后是否还在", bool(gone))
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "error_code", "") or type(exc).__name__
        print(f"\n[x] 执行中断: {code}: {str(exc)[:300]}")
        return 1

    print("\n全部操作完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
