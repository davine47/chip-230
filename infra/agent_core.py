"""AI Agent 只读查询核心。

被 ``infra/agent_query.py``（无服务的命令行工具/Skill）复用，本身不启动任何服务。

- 只能访问白名单内的三张表（documents / davine / plans）
- 只读，不提供写入、删除接口
- 文档列表默认不返回临时签名列，下载链接按请求现签
- 标签查询同时匹配 ``category`` 和可选的 ``tags`` 列，兼容现有数据

Tablestore 目前没有按属性列过滤的通用能力，所以查询仍是「扫描 + 内存过滤」，
受 ``ts_helper.SCAN_MAX_ROWS``（10,000 行）限制；响应里的 ``truncated`` 会提示。
数据量增长后应改用多元索引或 Elasticsearch。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from tair import docs as docs_service
from tair import plans as plans_service
from tair.config import Settings

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_OFFSET = 10_000
MAX_EXPIRES_HOURS = 168  # OSS 预签名 URL 最长 7 天

DOCUMENT_COLUMNS = [
    {"name": "id", "type": "STRING", "role": "primary_key", "description": "文档 UUID"},
    {"name": "name", "type": "STRING", "role": "attribute", "description": "文件名"},
    {"name": "url", "type": "STRING", "role": "attribute", "description": "OSS 对象地址"},
    {"name": "describe", "type": "STRING", "role": "attribute", "description": "描述（可选）"},
    {"name": "category", "type": "STRING", "role": "attribute", "description": "分类/单标签"},
    {
        "name": "tags",
        "type": "STRING",
        "role": "attribute",
        "optional": True,
        "description": "多标签，逗号分隔或 JSON 数组；当前上传流程未写入",
    },
    {"name": "create_time", "type": "INTEGER", "role": "attribute", "description": "毫秒时间戳"},
]

PLAN_COLUMNS = [
    {"name": "uuid", "type": "STRING", "role": "primary_key", "description": "计划 UUID"},
    {"name": "id", "type": "INTEGER", "role": "primary_key", "description": "服务端自增 ID"},
    {"name": "plan_name", "type": "STRING", "role": "attribute", "description": "计划名"},
    {"name": "trace_id", "type": "STRING", "role": "attribute", "description": "链路 ID"},
    {"name": "trace_info", "type": "STRING", "role": "attribute", "description": "链路说明"},
    {"name": "plan_info", "type": "STRING", "role": "attribute", "description": "计划摘要"},
    {"name": "create_time", "type": "INTEGER", "role": "attribute", "description": "毫秒时间戳"},
    {"name": "oss_url", "type": "STRING", "role": "attribute", "description": "Markdown 对象地址"},
    {
        "name": "github_project_name",
        "type": "STRING",
        "role": "attribute",
        "description": "GitHub 项目名",
    },
    {
        "name": "github_project_url",
        "type": "STRING",
        "role": "attribute",
        "description": "GitHub 项目地址",
    },
    {
        "name": "github_project_branch",
        "type": "STRING",
        "role": "attribute",
        "description": "GitHub 分支",
    },
]

_LINK_COLUMNS = {"tmp_public_url", "tmp_expire_time"}


def table_specs(settings: Settings) -> list[dict[str, Any]]:
    """当前允许 Agent 查询的表；表名取自配置，不把任意表名开放给调用方。"""
    specs = [
        {
            "name": settings.ts_table,
            "logical_name": "documents",
            "kind": "documents",
            "description": "文档库元数据；对象在 OSS 的 agents/docs/ 前缀下",
            "primary_key": ["id"],
            "columns": DOCUMENT_COLUMNS,
            "tags_supported": True,
        },
        {
            "name": settings.ts_davine_table,
            "logical_name": "davine",
            "kind": "davine",
            "description": "davine 文件交换元数据；对象在 davine_exchange/ 前缀下",
            "primary_key": ["id"],
            "columns": DOCUMENT_COLUMNS,
            "tags_supported": True,
        },
        {
            "name": "plans",
            "logical_name": "plans",
            "kind": "plans",
            "description": "Plan 库；对象在 plans/ 前缀下，主键为 uuid + 自增 id",
            "primary_key": ["uuid", "id"],
            "columns": PLAN_COLUMNS,
            "tags_supported": False,
        },
    ]

    # OTS_TABLE 和 OTS_DAVINE_TABLE 配成同一张表时只保留一条，避免列举重复。
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in specs:
        if spec["name"] in seen:
            continue
        seen.add(spec["name"])
        result.append(spec)
    return result


def resolve_table(settings: Settings, table: str) -> dict[str, Any]:
    """按真实表名或逻辑名（documents / davine / plans）取表定义。"""
    wanted = (table or "").strip()
    specs = table_specs(settings)
    for spec in specs:
        if wanted in {spec["name"], spec["logical_name"]}:
            return spec
    known = "、".join(f"{spec['name']}（{spec['logical_name']}）" for spec in specs)
    raise KeyError(f"未知表：{wanted or '(空)'}；可用：{known}")


def _iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _tag_set(value: Any) -> set[str]:
    """把 category / tags / 查询参数统一成小写标签集合。

    支持三种写法：逗号或分号分隔的字符串、JSON 数组字符串、Python 列表。
    """
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        merged: set[str] = set()
        for item in value:
            merged |= _tag_set(item)
        return merged
    text = str(value).strip()
    if not text:
        return set()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        parts = [str(item) for item in parsed] if isinstance(parsed, list) else [text]
    else:
        parts = re.split(r"[,;]", text)
    return {part.strip().casefold() for part in parts if part.strip()}


def _document_tags(document: dict[str, Any]) -> set[str]:
    return _tag_set(document.get("category")) | _tag_set(document.get("tags"))


def _text_matches(row: dict[str, Any], query: str, fields: list[str]) -> bool:
    needle = (query or "").strip().casefold()
    if not needle:
        return True
    return any(needle in str(row.get(field) or "").casefold() for field in fields)


def _matches_tags(row: dict[str, Any], wanted: set[str], mode: str) -> bool:
    if not wanted:
        return True
    actual = _document_tags(row)
    return wanted <= actual if mode == "all" else bool(wanted & actual)


def _strip_link_columns(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in _LINK_COLUMNS}


def query_table(
    settings: Settings,
    table: str,
    *,
    tags: Any = None,
    tag_mode: str = "any",
    category: str = "",
    query: str = "",
    since: int | None = None,
    until: int | None = None,
    project_name: str = "",
    branch: str = "",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """查询一张白名单表，返回分页后的行。

    文档表按 ``create_time`` 倒序，plans 按自增 ``id`` 倒序；``truncated=true``
    表示底层扫描达到 10,000 行上限，结果可能不完整。
    """
    spec = resolve_table(settings, table)
    kind = spec["kind"]

    try:
        limit = int(limit)
        offset = int(offset)
    except (TypeError, ValueError):
        raise ValueError("limit / offset 必须是整数") from None
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit 必须在 1 到 {MAX_LIMIT} 之间")
    if not 0 <= offset <= MAX_OFFSET:
        raise ValueError(f"offset 必须在 0 到 {MAX_OFFSET} 之间")

    mode = (tag_mode or "any").strip().lower()
    if mode not in {"any", "all"}:
        raise ValueError("tag_mode 只能是 any 或 all")

    wanted = _tag_set(tags)
    if wanted and not spec["tags_supported"]:
        raise ValueError(f"{spec['name']} 不支持按标签过滤")
    if kind == "plans" and category:
        raise ValueError(f"{spec['name']} 不支持 category 过滤")
    if since is not None and until is not None and since > until:
        raise ValueError("since 不能大于 until")

    if kind in {"documents", "davine"}:
        store = (
            docs_service.documents(settings)
            if kind == "documents"
            else docs_service.davine(settings)
        )
        rows, truncated = store.scan()
        rows = [_strip_link_columns(row) for row in rows]
        text_fields = ["name", "describe"]
    else:
        rows, truncated = plans_service.scan_plans(settings)
        text_fields = ["plan_name", "plan_info", "trace_info"]

    def keep(row: dict[str, Any]) -> bool:
        if category and str(row.get("category", "")).casefold() != category.strip().casefold():
            return False
        if not _matches_tags(row, wanted, mode):
            return False
        if not _text_matches(row, query, text_fields):
            return False
        created = _as_int(row.get("create_time"))
        if since is not None and created < since:
            return False
        if until is not None and created > until:
            return False
        if kind == "plans":
            if project_name and str(row.get("github_project_name", "")).casefold() != (
                project_name.strip().casefold()
            ):
                return False
            if branch and str(row.get("github_project_branch", "")).casefold() != (
                branch.strip().casefold()
            ):
                return False
        return True

    rows = [row for row in rows if keep(row)]
    total = len(rows)
    page = rows[offset : offset + limit]
    has_more = offset + len(page) < total
    next_offset = offset + len(page) if has_more else None

    return {
        "table": spec["name"],
        "logical_name": spec["logical_name"],
        "kind": kind,
        "filters": {
            "tag": sorted(wanted),
            "tag_mode": mode if wanted else None,
            "category": category or None,
            "q": query or None,
            "since": since,
            "until": until,
            "github_project_name": project_name or None,
            "github_project_branch": branch or None,
        },
        "limit": limit,
        "offset": offset,
        "count": len(page),
        "total": total,
        "truncated": truncated,
        "has_more": has_more,
        "next_offset": next_offset,
        "rows": page,
    }


def get_document(settings: Settings, source: str, doc_id: str, hours: int = 1) -> dict[str, Any]:
    """取单份文档的元数据，并现签一条 OSS 下载链接。"""
    source = (source or "documents").strip().lower()
    if source == "documents":
        store = docs_service.documents(settings)
    elif source == "davine":
        store = docs_service.davine(settings)
    else:
        raise ValueError("source 只能是 documents 或 davine")

    doc_id = (doc_id or "").strip()
    if not doc_id:
        raise ValueError("doc_id 不能为空")

    document = store.get(doc_id)
    if not document:
        raise KeyError("文档不存在")
    document = _strip_link_columns(document)

    url, expires_ms = store.presign_link(doc_id, hours)
    return {
        "source": source,
        "table": store.table,
        "document": document,
        "download_url": url,
        "expires_at": _iso_ms(expires_ms),
        "expires_hours": hours,
    }


def get_plan(settings: Settings, plan_uuid: str, plan_id: int, hours: int = 1) -> dict[str, Any]:
    """取单条计划，并现签一条 OSS 下载链接。"""
    plan = plans_service.get_plan(settings, plan_uuid, plan_id)
    if not plan:
        raise KeyError("计划不存在")
    url, expires_ms = plans_service.presign_plan_link(settings, plan_uuid, plan_id, hours)
    return {
        "table": "plans",
        "plan": plan,
        "download_url": url,
        "expires_at": _iso_ms(expires_ms),
        "expires_hours": hours,
    }
