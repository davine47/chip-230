"""计划库：OSS 存 .md 计划文件 + Tablestore 存索引（plans 表）。

表结构（主键两列）：
    uuid        STRING   分区键，应用生成
    id          INTEGER  自增，服务端生成（时间型 16 位整数，唯一且递增）
属性列：
    plan_name / trace_id / trace_info / plan_info / create_time

上传约束：
    - 只收 .md 文件
    - plan_info 必填
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
    PK_AUTO_INCR,
    Condition,
    ReturnType,
    Row,
    RowExistenceExpectation,
)

from . import oss as oss_helper
from . import tablestore as ts_helper
from .config import Settings

TABLE = "plans"
COLUMNS = ["plan_name", "trace_id", "trace_info", "plan_info", "create_time", "oss_key"]
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

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


def plans_prefix(settings: Settings) -> str:
    raw = (settings.oss_plans_prefix or "plans/").strip()
    return raw.strip("/") + "/"


def key_from_url(url: str) -> str:
    return unquote(urlsplit(url).path.lstrip("/"))


def safe_plan_name(name: str) -> str:
    """计划名当文件名用：去掉非法字符，强制 .md 后缀。"""
    base = Path(name or "").name.strip()
    base = _BAD_CHARS.sub("_", base)
    if base.lower().endswith(".md"):
        base = base[:-3]
    base = base.strip()[:80] or "plan"
    return base


def _unique_key(settings: Settings, plan_name: str) -> str:
    client = _oss(settings)
    key = f"{plans_prefix(settings)}{plan_name}.md"
    if not client.is_object_exist(bucket=settings.oss_bucket, key=key):
        return key
    return f"{plans_prefix(settings)}{plan_name}-{time.strftime('%Y%m%d%H%M%S')}.md"


def save_plan(
    settings: Settings,
    filename: str,
    data: bytes,
    plan_name: str,
    trace_id: str,
    trace_info: str,
    plan_info: str,
) -> dict[str, Any]:
    """上传 .md 到 OSS 的 plans/，并往 plans 表插一行。"""
    name = safe_plan_name(plan_name or filename)
    key = _unique_key(settings, name)
    bucket = settings.oss_bucket

    put = _oss(settings).put_object(
        oss.PutObjectRequest(
            bucket=bucket,
            key=key,
            body=data,
            content_type="text/markdown; charset=utf-8",
        )
    )

    doc_uuid = uuid.uuid4().hex
    now_ms = int(time.time() * 1000)
    url = oss_helper.object_url(settings, bucket, key)

    row = Row(
        [("uuid", doc_uuid), ("id", PK_AUTO_INCR)],
        [
            ("plan_name", name),
            ("trace_id", trace_id),
            ("trace_info", trace_info),
            ("plan_info", plan_info),
            ("create_time", now_ms),
            # 计划名可能重名（会加时间戳），把真实 key 存下来，取/删才不用猜
            ("oss_key", key),
        ],
    )
    _, ret = _ts(settings).put_row(
        TABLE,
        row,
        Condition(RowExistenceExpectation.IGNORE),
        return_type=ReturnType.RT_PK,
    )
    auto_id = dict(ret.primary_key).get("id") if ret and ret.primary_key else None

    return {
        "uuid": doc_uuid,
        "id": auto_id,
        "plan_name": name,
        "trace_id": trace_id,
        "trace_info": trace_info,
        "plan_info": plan_info,
        "create_time": now_ms,
        "url": url,
        "size": len(data),
        "etag": put.etag,
        "request_id": put.request_id,
    }


def list_plans(settings: Settings, limit: int = 200) -> list[dict[str, Any]]:
    """按 id（自增）倒序返回，最新的在最前面。"""
    _, _, rows, _ = _ts(settings).get_range(
        TABLE,
        "FORWARD",
        [("uuid", INF_MIN), ("id", INF_MIN)],
        [("uuid", INF_MAX), ("id", INF_MAX)],
        columns_to_get=COLUMNS,
        limit=limit,
    )
    plans = []
    for row in rows or []:
        item = ts_helper.primary_key_dict(row)
        item.update(ts_helper.row_to_dict(row))
        plans.append(item)
    plans.sort(key=lambda p: p.get("id") or 0, reverse=True)
    return plans


def get_plan(settings: Settings, plan_uuid: str, plan_id: int) -> dict[str, Any] | None:
    _, row, _ = _ts(settings).get_row(
        TABLE,
        [("uuid", plan_uuid), ("id", plan_id)],
        columns_to_get=COLUMNS,
    )
    if row is None:
        return None
    item = ts_helper.primary_key_dict(row)
    item.update(ts_helper.row_to_dict(row))
    return item


def presign_plan(settings: Settings, plan_uuid: str, plan_id: int, hours: int = 1) -> str:
    plan = get_plan(settings, plan_uuid, plan_id)
    if not plan:
        raise KeyError("计划不存在")
    key = plan.get("oss_key") or f"{plans_prefix(settings)}{plan.get('plan_name', '')}.md"
    signed = _oss(settings).presign(
        oss.GetObjectRequest(bucket=settings.oss_bucket, key=key),
        expires=timedelta(hours=hours),
    )
    return signed.url


def delete_plan(settings: Settings, plan_uuid: str, plan_id: int) -> bool:
    plan = get_plan(settings, plan_uuid, plan_id)
    if not plan:
        return False
    _ts(settings).delete_row(
        TABLE,
        [("uuid", plan_uuid), ("id", plan_id)],
        Condition(RowExistenceExpectation.IGNORE),
    )
    key = plan.get("oss_key") or f"{plans_prefix(settings)}{plan.get('plan_name', '')}.md"
    if key:
        _oss(settings).delete_object(oss.DeleteObjectRequest(bucket=settings.oss_bucket, key=key))
    return True


def count_plans(settings: Settings) -> int:
    return len(list_plans(settings, limit=1000))
