"""计划库：OSS 存 .md 计划文件 + Tablestore 存索引（plans 表）。

表结构（主键两列）：
    uuid        STRING   分区键，应用生成
    id          INTEGER  自增，服务端生成（时间型 16 位整数，唯一且递增）
属性列：
    plan_name / trace_id / trace_info / plan_info / create_time

上传约束：
    - 只收 .md 文件
    - plan_info 可选
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
COLUMNS = [
    "plan_name",
    "trace_id",
    "trace_info",
    "plan_info",
    "create_time",
    "oss_url",
    "github_project_name",
    "github_project_url",
    "github_project_branch",
]
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

_oss = oss_helper.shared_client
_ts = ts_helper.shared_client
key_from_url = oss_helper.key_from_url


def validate_repo_url(url: str) -> str:
    """项目地址只接受 https:// 链接，空串表示不填。

    这个值会被前端渲染成 <a href>，不校验的话可以存进 javascript:... ，别人一点就在看板里执行脚本。
    """
    url = (url or "").strip()
    if not url:
        return ""
    if len(url) > 500:
        raise ValueError("github_project_url 太长（上限 500 字符）")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.netloc or any(c in url for c in " \t\r\n<>\"'"):
        raise ValueError("github_project_url 必须是 https:// 开头的有效链接")
    return url


def plans_prefix(settings: Settings) -> str:
    raw = (settings.oss_plans_prefix or "plans/").strip()
    return raw.strip("/") + "/"


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
    github_project_name: str = "",
    github_project_url: str = "",
    github_project_branch: str = "",
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

    attrs = [
        ("plan_name", name),
        ("trace_id", trace_id),
        ("trace_info", trace_info),
        ("create_time", now_ms),
        # 计划名可能重名（会加时间戳），把完整地址存下来，取/删才不用猜
        ("oss_url", url),
        ("github_project_name", github_project_name),
        ("github_project_branch", github_project_branch),
    ]
    # 这几个是可选的，空着就不写对应的列
    if plan_info:
        attrs.insert(3, ("plan_info", plan_info))
    if github_project_url:
        attrs.append(("github_project_url", github_project_url))
    row = Row([("uuid", doc_uuid), ("id", PK_AUTO_INCR)], attrs)
    try:
        _, ret = _ts(settings).put_row(
            TABLE,
            row,
            Condition(RowExistenceExpectation.IGNORE),
            return_type=ReturnType.RT_PK,
        )
    except Exception:
        # 索引没写进去：回滚刚上传的 .md，免得留下看板看不到的孤儿对象
        oss_helper.discard_object(_oss(settings), bucket, key)
        raise
    auto_id = dict(ret.primary_key).get("id") if ret and ret.primary_key else None

    return {
        "uuid": doc_uuid,
        "id": auto_id,
        "plan_name": name,
        "trace_id": trace_id,
        "trace_info": trace_info,
        "plan_info": plan_info,
        "create_time": now_ms,
        "github_project_name": github_project_name,
        "github_project_url": github_project_url,
        "github_project_branch": github_project_branch,
        "url": url,
        "size": len(data),
        "etag": put.etag,
        "request_id": put.request_id,
    }


def scan_plans(settings: Settings) -> tuple[list[dict[str, Any]], bool]:
    """分页扫完 plans 表，按 id（自增）倒序返回 (全部计划, 是否超过扫描上限被截断)。

    分区键是随机 uuid，get_range 按 uuid 排序；只取第一页再按 id 排序会漏掉最新的计划。
    """
    rows, truncated = ts_helper.scan_all(
        _ts(settings),
        TABLE,
        [("uuid", INF_MIN), ("id", INF_MIN)],
        [("uuid", INF_MAX), ("id", INF_MAX)],
        COLUMNS,
    )
    plans = []
    for row in rows:
        item = ts_helper.primary_key_dict(row)
        item.update(ts_helper.row_to_dict(row))
        plans.append(item)
    plans.sort(key=lambda p: p.get("id") or 0, reverse=True)
    return plans, truncated


def list_plans(settings: Settings, limit: int = 200) -> list[dict[str, Any]]:
    """最新的 limit 份（按自增 id 倒序）。"""
    return scan_plans(settings)[0][:limit]


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


def presign_plan_link(
    settings: Settings, plan_uuid: str, plan_id: int, hours: int = 1
) -> tuple[str, int]:
    """按计划主键现签一条链接，返回 (地址, 过期时间毫秒戳)。"""
    plan = get_plan(settings, plan_uuid, plan_id)
    if not plan:
        raise KeyError("计划不存在")
    key = key_from_url(plan.get("oss_url", "")) or (
        f"{plans_prefix(settings)}{plan.get('plan_name', '')}.md"
    )
    expiration = datetime.now(timezone.utc) + timedelta(hours=hours)
    signed = _oss(settings).presign(
        oss.GetObjectRequest(bucket=settings.oss_bucket, key=key),
        expiration=expiration,
    )
    return signed.url, int(expiration.timestamp() * 1000)


def presign_plan(settings: Settings, plan_uuid: str, plan_id: int, hours: int = 1) -> str:
    return presign_plan_link(settings, plan_uuid, plan_id, hours)[0]


def delete_plan(settings: Settings, plan_uuid: str, plan_id: int) -> bool:
    """先删 OSS 对象（幂等），再删索引行；顺序原因同 docs.DocumentStore.delete。"""
    plan = get_plan(settings, plan_uuid, plan_id)
    if not plan:
        return False
    key = key_from_url(plan.get("oss_url", "")) or (
        f"{plans_prefix(settings)}{plan.get('plan_name', '')}.md"
    )
    if key:
        _oss(settings).delete_object(oss.DeleteObjectRequest(bucket=settings.oss_bucket, key=key))
    _ts(settings).delete_row(
        TABLE,
        [("uuid", plan_uuid), ("id", plan_id)],
        Condition(RowExistenceExpectation.IGNORE),
    )
    return True


def count_plans(settings: Settings) -> int:
    """真实总数（以前到 1000 就封顶）。"""
    return len(scan_plans(settings)[0])
