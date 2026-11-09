"""文档库：OSS 存文件 + Tablestore 存索引，两边一起用。

上传流程：
    1. 文件写进 OSS 的 agents/docs/ 前缀下（同名会自动加时间戳）
    2. 往 Tablestore 的 oss_documents 表插一行元数据（id/name/url/describe/category/create_time）
    3. 读的时候按 id 查表，需要下载时再生成 OSS 预签名链接（桶是私有的）
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import alibabacloud_oss_v2 as oss
from tablestore import (
    INF_MAX,
    INF_MIN,
    Condition,
    Row,
    RowExistenceExpectation,
)

from . import oss as oss_helper
from . import tablestore as ts_helper
from .config import Settings

COLUMNS = ["name", "url", "describe", "category", "create_time"]
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# 客户端建一次就复用，别每个请求都重新握手
_oss_client = None
_ts_client = None


def _oss(settings: Settings):
    global _oss_client
    if _oss_client is None:
        _oss_client = oss_helper.build_client(settings)
    return _oss_client


def _ts(settings: Settings):
    global _ts_client
    if _ts_client is None:
        _ts_client = ts_helper.build_client(settings)
    return _ts_client


def key_from_url(url: str) -> str:
    """从对象地址反推 OSS key（URL 里的中文/空格是百分号编码的，要还原）。"""
    return unquote(urlsplit(url).path.lstrip("/"))


def docs_prefix(settings: Settings) -> str:
    """对象前缀，固定以 / 结尾，默认 agents/docs/。"""
    raw = (settings.oss_docs_prefix or "agents/docs/").strip()
    return raw.strip("/") + "/"


def safe_name(filename: str) -> str:
    """只保留文件名部分，去掉路径分隔符和非法字符（中文保留）。"""
    name = Path(filename or "").name.strip()
    name = _BAD_CHARS.sub("_", name)
    return name[:120] or "unnamed"


def _unique_key(settings: Settings, filename: str) -> str:
    """同名文件已存在时，加时间戳避免覆盖。"""
    client = _oss(settings)
    key = f"{docs_prefix(settings)}{filename}"
    if not client.is_object_exist(bucket=settings.oss_bucket, key=key):
        return key
    stem, suffix = Path(filename).stem, Path(filename).suffix
    return f"{docs_prefix(settings)}{stem}-{time.strftime('%Y%m%d%H%M%S')}{suffix}"


def guess_content_type(filename: str) -> str | None:
    """常见文档类型显式指定，其余交给 SDK 按后缀猜。"""
    suffix = Path(filename).suffix.lower()
    return {
        ".md": "text/markdown; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".csv": "text/csv; charset=utf-8",
    }.get(suffix)


def save_document(
    settings: Settings,
    filename: str,
    data: bytes,
    category: str,
    description: str,
) -> dict[str, Any]:
    """上传到 OSS 并写入索引表，返回这一行的完整字段。"""
    name = safe_name(filename)
    key = _unique_key(settings, name)
    bucket = settings.oss_bucket

    put = _oss(settings).put_object(
        oss.PutObjectRequest(
            bucket=bucket,
            key=key,
            body=data,
            content_type=guess_content_type(name),
        )
    )

    doc_id = uuid.uuid4().hex
    now_ms = int(time.time() * 1000)
    url = oss_helper.object_url(settings, bucket, key)
    row = Row(
        [("id", doc_id)],
        [
            ("name", name),
            ("url", url),
            ("describe", description),
            ("category", category),
            ("create_time", now_ms),
        ],
    )
    _ts(settings).put_row(settings.ts_table, row, Condition(RowExistenceExpectation.IGNORE))

    return {
        "id": doc_id,
        "name": name,
        "url": url,
        "describe": description,
        "category": category,
        "create_time": now_ms,
        "size": len(data),
        "etag": put.etag,
        "request_id": put.request_id,
    }


def list_documents(settings: Settings, limit: int = 200) -> list[dict[str, Any]]:
    """扫全表，按创建时间倒序返回（表上还没建二级索引，量小够用）。"""
    _, _, rows, _ = _ts(settings).get_range(
        settings.ts_table,
        "FORWARD",
        [("id", INF_MIN)],
        [("id", INF_MAX)],
        columns_to_get=COLUMNS,
        limit=limit,
    )
    docs = []
    for row in rows or []:
        item = ts_helper.primary_key_dict(row)
        item.update(ts_helper.row_to_dict(row))
        docs.append(item)
    docs.sort(key=lambda d: d.get("create_time") or 0, reverse=True)
    return docs


def get_document(settings: Settings, doc_id: str) -> dict[str, Any] | None:
    _, row, _ = _ts(settings).get_row(settings.ts_table, [("id", doc_id)], columns_to_get=COLUMNS)
    if row is None:
        return None
    item = ts_helper.primary_key_dict(row)
    item.update(ts_helper.row_to_dict(row))
    return item


def presign_document(settings: Settings, doc_id: str, hours: int = 1) -> str:
    """按 id 生成临时下载链接；桶是私有的，直接给 url 打不开。"""
    doc = get_document(settings, doc_id)
    if not doc:
        raise KeyError("文档不存在")
    key = key_from_url(doc["url"])
    signed = _oss(settings).presign(
        oss.GetObjectRequest(bucket=settings.oss_bucket, key=key),
        expires=timedelta(hours=hours),
    )
    return signed.url


def delete_document(settings: Settings, doc_id: str) -> bool:
    """删索引行 + 删 OSS 对象。"""
    doc = get_document(settings, doc_id)
    if not doc:
        return False
    _ts(settings).delete_row(
        settings.ts_table, [("id", doc_id)], Condition(RowExistenceExpectation.IGNORE)
    )
    url = doc.get("url", "")
    key = key_from_url(url) if url else ""
    if key:
        _oss(settings).delete_object(oss.DeleteObjectRequest(bucket=settings.oss_bucket, key=key))
    return True


def count_documents(settings: Settings) -> int:
    return len(list_documents(settings, limit=1000))
