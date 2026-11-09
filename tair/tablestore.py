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


def row_to_dict(row) -> dict:
    """把 SDK 的 Row 转成普通 dict。

    attribute_columns 是 (列名, 值, 时间戳) 三元组，别直接 dict()。
    """
    if row is None:
        return {}
    return {name: value for name, value, _ts in (row.attribute_columns or [])}


def primary_key_dict(row) -> dict:
    return dict(row.primary_key or []) if row is not None else {}
