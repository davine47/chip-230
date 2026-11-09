"""文档库：OSS 存文件 + Tablestore 存索引，两边一起用。

同一套逻辑服务多个看板，只是前缀和表名不同：

    文档库  /documents   agents/docs/       + oss_documents
    davine  /davine      davine_exchange/   + davine_oss_documents

上传流程：文件写 OSS → 往表里插一行元数据；读取时按 id 查表，下载时再生成预签名链接。

两边不是一个事务，靠顺序 + 补偿保证不留孤儿：
    上传：先写 OSS，写表失败就删掉刚传的对象
    删除：先删 OSS（幂等），再删索引行；删行失败时索引还在，重试即可
漏网之鱼由 scripts/06_reconcile.py 对账清理。
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
        try:
            return self._record(
                name,
                oss_helper.object_url(settings, settings.oss_bucket, key),
                category,
                description,
                Path(filepath).stat().st_size,
                getattr(result, "etag", "") or "",
                "",
            )
        except Exception:
            # 索引没写进去，对象就是孤儿：看板列不出来、也删不掉，所以立刻回滚
            oss_helper.discard_object(_oss(settings), settings.oss_bucket, key)
            raise

    # ---------------------------------------------------------------- 读
    def scan(self) -> tuple[list[dict[str, Any]], bool]:
        """分页扫完整张表，按创建时间倒序返回 (全部文档, 是否超过扫描上限被截断)。

        主键 id 是随机 uuid，get_range 返回的是 uuid 字典序而不是时间序，
        所以必须扫完再排序；只取第一页再排序，最新上传的文档可能根本不在里面。
        文档数超过 ts_helper.SCAN_MAX_ROWS 时，应改用多元索引按 create_time 排序。
        """
        rows, truncated = ts_helper.scan_all(
            _ts(self.settings), self.table, [("id", INF_MIN)], [("id", INF_MAX)], COLUMNS
        )
        docs = []
        for row in rows:
            item = ts_helper.primary_key_dict(row)
            item.update(ts_helper.row_to_dict(row))
            docs.append(item)
        docs.sort(key=lambda d: d.get("create_time") or 0, reverse=True)
        return docs, truncated

    def list(self, limit: int = 200) -> list[dict[str, Any]]:
        """最新的 limit 份（按创建时间倒序）。"""
        return self.scan()[0][:limit]

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
        """先删 OSS 对象，再删索引行。

        顺序不能反：先删行的话，一旦删对象失败，就留下一个没有索引、看板上永远删不掉的对象。
        现在这样，删对象失败时索引还在，可以重试；OSS 删除是幂等的，对象已不在也返回成功。
        """
        doc = self.get(doc_id)
        if not doc:
            return False
        key = key_from_url(doc.get("url", ""))
        if key:
            _oss(self.settings).delete_object(
                oss.DeleteObjectRequest(bucket=self.settings.oss_bucket, key=key)
            )
        _ts(self.settings).delete_row(
            self.table, [("id", doc_id)], Condition(RowExistenceExpectation.IGNORE)
        )
        return True

    def count(self) -> int:
        """真实总数（以前是 len(list(limit=1000))，到 1000 就封顶）。"""
        return len(self.scan()[0])


def documents(settings: Settings) -> DocumentStore:
    """文档库看板：agents/docs/ + oss_documents。"""
    return DocumentStore(settings, settings.oss_docs_prefix, settings.ts_table)


def davine(settings: Settings) -> DocumentStore:
    """davine 看板：davine_exchange/ + davine_oss_documents。"""
    return DocumentStore(settings, settings.oss_davine_prefix, settings.ts_davine_table)
