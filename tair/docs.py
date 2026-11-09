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

import alibabacloud_oss_v2 as oss
from tablestore import INF_MAX, INF_MIN, Condition, Row, RowExistenceExpectation

from . import oss as oss_helper
from . import tablestore as ts_helper
from .config import Settings

COLUMNS = ["name", "url", "describe", "category", "create_time"]
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

_oss = oss_helper.shared_client
_ts = ts_helper.shared_client
key_from_url = oss_helper.key_from_url


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
    def _record(
        self,
        name: str,
        url: str,
        category: str,
        description: str,
        size: int,
        etag: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        """往索引表写一行，返回给前端的完整字段。"""
        doc_id = uuid.uuid4().hex
        now_ms = int(time.time() * 1000)
        attrs = [("name", name), ("url", url)]
        if description:  # 描述可选：空字符串就不写这一列
            attrs.append(("describe", description))
        attrs += [("category", category), ("create_time", now_ms)]
        _ts(self.settings).put_row(
            self.table, Row([("id", doc_id)], attrs), Condition(RowExistenceExpectation.IGNORE)
        )
        return {
            "id": doc_id,
            "name": name,
            "url": url,
            "describe": description,
            "category": category,
            "create_time": now_ms,
            "size": size,
            "etag": etag,
            "request_id": request_id,
        }

    def save(self, filename: str, data: bytes, category: str, description: str) -> dict[str, Any]:
        """小文件：直接整块 PUT。"""
        settings = self.settings
        name = safe_name(filename)
        key = self._unique_key(name)
        put = _oss(settings).put_object(
            oss.PutObjectRequest(
                bucket=settings.oss_bucket,
                key=key,
                body=data,
                content_type=guess_content_type(name),
            )
        )
        return self._record(
            name,
            oss_helper.object_url(settings, settings.oss_bucket, key),
            category,
            description,
            len(data),
            put.etag,
            put.request_id,
        )

    def save_from_file(
        self, filename: str, filepath: str, category: str, description: str
    ) -> dict[str, Any]:
        """大文件：交给 uploader 做分片上传，内存占用和文件大小无关。"""
        settings = self.settings
        name = safe_name(filename)
        key = self._unique_key(name)
        result = (
            _oss(settings)
            .uploader()
            .upload_file(
                oss.PutObjectRequest(
                    bucket=settings.oss_bucket,
                    key=key,
                    content_type=guess_content_type(name),
                ),
                filepath,
            )
        )
        return self._record(
            name,
            oss_helper.object_url(settings, settings.oss_bucket, key),
            category,
            description,
            Path(filepath).stat().st_size,
            getattr(result, "etag", "") or "",
            "",
        )

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
        _, row, _ = _ts(self.settings).get_row(self.table, [("id", doc_id)], columns_to_get=COLUMNS)
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
