"""文档库：OSS 存文件 + Tablestore 存索引，两边一起用。

同一套逻辑服务多个看板，只是前缀和表名不同：

    文档库  /documents   agents/docs/       + oss_documents
    davine  /davine      davine_exchange/   + davine_oss_documents

上传流程：文件写 OSS → 往表里插一行元数据；读取时按 id 查表，下载时再生成预签名链接。
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
from tablestore import INF_MAX, INF_MIN, Condition, Row, RowExistenceExpectation

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


def safe_name(filename: str) -> str:
    """只保留文件名部分，去掉路径分隔符和非法字符（中文保留）。"""
    name = Path(filename or "").name.strip()
    name = _BAD_CHARS.sub("_", name)
    return name[:120] or "unnamed"


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


class DocumentStore:
    """一个「前缀 + 表」的组合，代表一块文档看板的存储。"""

    def __init__(self, settings: Settings, prefix: str, table: str):
        self.settings = settings
        self.prefix = (prefix or "").strip("/") + "/"
        self.table = table

    # ---------------------------------------------------------------- 内部
    def _unique_key(self, name: str) -> str:
        client = _oss(self.settings)
        key = f"{self.prefix}{name}"
        if not client.is_object_exist(bucket=self.settings.oss_bucket, key=key):
            return key
        stem, suffix = Path(name).stem, Path(name).suffix
        return f"{self.prefix}{stem}-{time.strftime('%Y%m%d%H%M%S')}{suffix}"

    # ---------------------------------------------------------------- 写
    def save(self, filename: str, data: bytes, category: str, description: str) -> dict[str, Any]:
        """上传到 OSS 并写入索引表，返回这一行的完整字段。"""
        settings = self.settings
        name = safe_name(filename)
        key = self._unique_key(name)
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
        _ts(settings).put_row(self.table, row, Condition(RowExistenceExpectation.IGNORE))

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

    # ---------------------------------------------------------------- 读
    def list(self, limit: int = 200) -> list[dict[str, Any]]:
        """扫全表，按创建时间倒序返回（表上还没建二级索引，量小够用）。"""
        _, _, rows, _ = _ts(self.settings).get_range(
            self.table,
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

    def get(self, doc_id: str) -> dict[str, Any] | None:
        _, row, _ = _ts(self.settings).get_row(
            self.table, [("id", doc_id)], columns_to_get=COLUMNS
        )
        if row is None:
            return None
        item = ts_helper.primary_key_dict(row)
        item.update(ts_helper.row_to_dict(row))
        return item

    def presign(self, doc_id: str, hours: int = 1) -> str:
        """按 id 生成临时下载链接；桶是私有的，直接给 url 打不开。"""
        doc = self.get(doc_id)
        if not doc:
            raise KeyError("文档不存在")
        signed = _oss(self.settings).presign(
            oss.GetObjectRequest(bucket=self.settings.oss_bucket, key=key_from_url(doc["url"])),
            expires=timedelta(hours=hours),
        )
        return signed.url

    # ---------------------------------------------------------------- 删
    def delete(self, doc_id: str) -> bool:
        """删索引行 + 删 OSS 对象。"""
        doc = self.get(doc_id)
        if not doc:
            return False
        _ts(self.settings).delete_row(
            self.table, [("id", doc_id)], Condition(RowExistenceExpectation.IGNORE)
        )
        key = key_from_url(doc.get("url", ""))
        if key:
            _oss(self.settings).delete_object(
                oss.DeleteObjectRequest(bucket=self.settings.oss_bucket, key=key)
            )
        return True

    def count(self) -> int:
        return len(self.list(limit=1000))


def documents(settings: Settings) -> DocumentStore:
    """文档库看板：agents/docs/ + oss_documents。"""
    return DocumentStore(settings, settings.oss_docs_prefix, settings.ts_table)


def davine(settings: Settings) -> DocumentStore:
    """davine 看板：davine_exchange/ + davine_oss_documents。"""
    return DocumentStore(settings, settings.oss_davine_prefix, settings.ts_davine_table)
