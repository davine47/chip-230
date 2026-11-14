"""Tablestore（表格存储）宽表模型封装。

对应文档：https://help.aliyun.com/document_detail/27293.html

要点：
  - 数据面 SDK 是 `tablestore`（不是 alibabacloud_* 那套），连的是「实例公网域名」
  - 表只需要定义主键，其余列都是属性列，写入时带上即可（免 schema）
  - 第一个主键是分区键，且**不能自增**；自增列只能出现在后面的主键上
"""

from __future__ import annotations

from tablestore import OTSClient

from .config import Settings


def build_client(settings: Settings) -> OTSClient:
    """按 .env 里的实例名/地域拼出 endpoint，连上数据面。"""
    return OTSClient(
        settings.ts_endpoint_or_default,
        settings.access_key_id,
        settings.access_key_secret,
        settings.ts_instance,
    )


_SHARED: list = []


def shared_client(settings: Settings):
    """进程内复用的客户端。"""
    if not _SHARED:
        _SHARED.append(build_client(settings))
    return _SHARED[0]


def row_to_dict(row) -> dict:
    """把 SDK 的 Row 转成普通 dict。

    attribute_columns 是 (列名, 值, 时间戳) 三元组，别直接 dict()。
    """
    if row is None:
        return {}
    return {name: value for name, value, _ts in (row.attribute_columns or [])}


def primary_key_dict(row) -> dict:
    return dict(row.primary_key or []) if row is not None else {}


# get_range 单次最多返回 5000 行 / 4MB，超过就要拿 next_start_primary_key 接着扫。
# 看板的文档量级在几千以内，扫全表再排序足够；超过这个上限说明该上多元索引了。
SCAN_PAGE_ROWS = 5000
SCAN_MAX_ROWS = 10_000


def scan_all(
    client,
    table: str,
    start_pk: list,
    end_pk: list,
    columns: list[str] | None = None,
    max_rows: int | None = SCAN_MAX_ROWS,
) -> tuple[list, bool]:
    """分页扫完 [start_pk, end_pk) 整个范围，返回 (行列表, 是否因超过 max_rows 被截断)。

    只调一次 get_range 只能拿到第一页；而且行是按主键排序的，不是按时间，
    所以「取前 N 行再按时间排序」得到的并不是最新的 N 行。

    max_rows=None 表示不设上限、一直扫到表尾（此时第二个返回值恒为 False）。
    看板列表要防止一次请求扫太久，用默认上限；同名更新这类「漏一条就出错」的场景用 None。
    """
    rows: list = []
    next_pk = start_pk
    while next_pk is not None:
        _, next_pk, page, _ = client.get_range(
            table,
            "FORWARD",
            next_pk,
            end_pk,
            columns_to_get=columns,
            limit=SCAN_PAGE_ROWS if max_rows is None else min(SCAN_PAGE_ROWS, max_rows - len(rows)),
        )
        rows.extend(page or [])
        if max_rows is not None and len(rows) >= max_rows:
            return rows, next_pk is not None
    return rows, False
