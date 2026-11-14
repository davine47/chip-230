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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import alibabacloud_oss_v2 as oss
from tablestore import INF_MAX, INF_MIN, Condition, Row, RowExistenceExpectation

from . import oss as oss_helper
from . import tablestore as ts_helper
from .config import Settings

COLUMNS = ["name", "url", "describe", "category", "create_time", "tags"]
# 临时链接两列：签名地址（谁拿到都能下载）+ 它的过期时间（毫秒时间戳）。
# 只有按 id 查单条时才读，列表扫描绝不带出来，免得打开页面就把所有链接都发给浏览器。
LINK_COLUMNS = ["tmp_public_url", "tmp_expire_time"]
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

_oss = oss_helper.shared_client
_ts = ts_helper.shared_client
key_from_url = oss_helper.key_from_url


MAX_NAME_CHARS = 120


def safe_name(filename: str, limit: int = MAX_NAME_CHARS) -> str:
    """只保留文件名部分，去掉路径分隔符和非法字符（中文保留）。

    超长时只截文件名主干、保留扩展名：以前直接 [:120]，会把 .pdf 之类截掉，
    content-type 判断跟着出错，下载回来也打不开。
    """
    name = _BAD_CHARS.sub("_", Path(filename or "").name.strip())
    if len(name) > limit:
        suffix = Path(name).suffix
        if len(suffix) > 16:  # 异常长的「后缀」多半不是真后缀，不保留
            suffix = ""
        name = name[: limit - len(suffix)].rstrip() + suffix
    return name or "unnamed"


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
        tmp_public_url: str = "",
        tmp_expire_time: int = 0,
    ) -> dict[str, Any]:
        """往索引表写一行，返回给前端的完整字段。"""
        doc_id = uuid.uuid4().hex
        now_ms = int(time.time() * 1000)
        attrs = [("name", name), ("url", url)]
        if description:  # 描述可选：空字符串就不写这一列
            attrs.append(("describe", description))
        attrs += [("category", category), ("create_time", now_ms)]
        if tmp_public_url:  # 只有要了临时链接才写这两列
            attrs += [("tmp_public_url", tmp_public_url), ("tmp_expire_time", tmp_expire_time)]
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
            "tmp_public_url": tmp_public_url,
            "tmp_expire_time": tmp_expire_time,
        }

    def save_from_file(
        self,
        filename: str,
        filepath: str,
        category: str,
        description: str,
        tmp_hours: float | None = None,
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
                *self._tmp_link(key, tmp_hours),
            )
        except Exception:
            # 索引没写进去，对象就是孤儿：看板列不出来、也删不掉，所以立刻回滚
            oss_helper.discard_object(_oss(settings), settings.oss_bucket, key)
            raise

    # ---------------------------------------------------------------- 浏览器直传
    def prepare_direct_upload(self, filename: str, hours: int = 1) -> dict[str, Any]:
        """浏览器直传第一步：定好对象 key，签一个限时的 PUT 地址交给浏览器。

        Content-Type 一定显式指定并参与签名：不指定的话浏览器会按文件类型自己带一个，
        和签名对不上就是 SignatureDoesNotMatch。浏览器必须原样带上返回的 headers。
        """
        settings = self.settings
        name = safe_name(filename)
        key = self._unique_key(name)
        signed = _oss(settings).presign(
            oss.PutObjectRequest(
                bucket=settings.oss_bucket,
                key=key,
                content_type=guess_content_type(name) or "application/octet-stream",
            ),
            expires=timedelta(hours=hours),
        )
        headers = {
            k: v for k, v in (signed.signed_headers or {}).items() if k.lower() != "host"
        }  # Host 浏览器不允许手动设置
        return {
            "name": name,
            "key": key,
            "url": signed.url,
            "method": signed.method or "PUT",
            "headers": headers,
        }

    def complete_direct_upload(
        self,
        name: str,
        key: str,
        category: str,
        description: str,
        max_bytes: int,
        tmp_hours: float | None = None,
    ) -> dict[str, Any]:
        """浏览器直传第二步：确认对象真的在 OSS 上、大小合规，再写索引。

        签名 URL 限制不了上传大小，所以以 OSS 上的实际大小为准；不合规就删掉对象并报错。
        """
        settings = self.settings
        client = _oss(settings)
        try:
            head = client.head_object(oss.HeadObjectRequest(bucket=settings.oss_bucket, key=key))
        except Exception as exc:
            if "404" in str(exc) or "NoSuchKey" in str(exc):
                raise ValueError("OSS 上没有找到刚上传的文件，请重新上传") from None
            raise
        size = int(getattr(head, "content_length", 0) or 0)
        if size == 0 or size > max_bytes:
            oss_helper.discard_object(client, settings.oss_bucket, key)
            if size == 0:
                raise ValueError("文件是空的")
            raise ValueError(f"文件超过 {max_bytes // 1024 // 1024}MB 上限，已删除")
        try:
            return self._record(
                name,
                oss_helper.object_url(settings, settings.oss_bucket, key),
                category,
                description,
                size,
                (getattr(head, "etag", "") or "").strip('"'),
                "",
                *self._tmp_link(key, tmp_hours),
            )
        except Exception:
            oss_helper.discard_object(client, settings.oss_bucket, key)
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
        _, row, _ = _ts(self.settings).get_row(
            self.table, [("id", doc_id)], columns_to_get=COLUMNS + LINK_COLUMNS
        )
        if row is None:
            return None
        item = ts_helper.primary_key_dict(row)
        item.update(ts_helper.row_to_dict(row))
        return item

    def _sign_key(self, key: str, hours: float) -> tuple[str, int]:
        """给对象 key 签一个限时下载地址，返回 (地址, 过期时间毫秒戳)。桶是私有的，直接给 url 打不开。

        过期时刻先算好再交给 SDK（expiration=），存下来的时间和签名里的就是同一个值，
        不会像各自取 now() 那样差出几秒。
        """
        expiration = datetime.now(timezone.utc) + timedelta(hours=hours)
        signed = _oss(self.settings).presign(
            oss.GetObjectRequest(bucket=self.settings.oss_bucket, key=key),
            expiration=expiration,
        )
        return signed.url, int(expiration.timestamp() * 1000)

    def _tmp_link(self, key: str, hours: float | None) -> tuple[str, int]:
        """上传时要不要顺带签临时链接：没选有效期就返回空。"""
        return self._sign_key(key, hours) if hours else ("", 0)

    def presign_link(self, doc_id: str, hours: float = 1) -> tuple[str, int]:
        """按 id 现签一条链接，返回 (地址, 过期时间毫秒戳)。"""
        doc = self.get(doc_id)
        if not doc:
            raise KeyError("文档不存在")
        return self._sign_key(key_from_url(doc["url"]), hours)

    def presign(self, doc_id: str, hours: float = 1) -> str:
        """按 id 现签一条链接（列表里的「打开」用）。"""
        return self.presign_link(doc_id, hours)[0]

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
