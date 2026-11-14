#!/usr/bin/env python3
"""chip-230 一次性查询工具（不启动 Web 服务）。

给 AI Agent / 运维脚本直接调用，输出 JSON，复用仓库根目录 `.env` 中的
OSS / Tablestore / AccessKey 配置。只读，不提供写入和删除。

用法：
    ./.venv/bin/python infra/agent_query.py health
    ./.venv/bin/python infra/agent_query.py tables
    ./.venv/bin/python infra/agent_query.py documents --tag env
    ./.venv/bin/python infra/agent_query.py rows oss_documents --tag env --limit 20
    ./.venv/bin/python infra/agent_query.py document <doc_id> --expires 24
    ./.venv/bin/python infra/agent_query.py download-document <doc_id> --out /tmp/doc.md
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

# Tablestore SDK 在缺少原生扩展时会用 warning 打印提示，CLI 的 stderr 只保留 JSON。
logging.getLogger("tablestore").setLevel(logging.ERROR)

# 默认按仓库内位置定位；把本工具复制到其它 Agent 的 skill 目录时，用
# CHIP230_ROOT 指向 chip-230 仓库根目录。
ROOT_DIR = (
    Path(os.getenv("CHIP230_ROOT") or Path(__file__).resolve().parent.parent).expanduser().resolve()
)
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

if not (ROOT_DIR / "tair" / "config.py").is_file():
    json.dump(
        {
            "ok": False,
            "error": f"找不到 chip-230 仓库：{ROOT_DIR}；请设置 CHIP230_ROOT 指向仓库根目录",
        },
        sys.stderr,
        ensure_ascii=False,
    )
    sys.stderr.write("\n")
    raise SystemExit(3)

try:
    from . import agent_core
except ImportError:  # 直接以脚本方式运行：python infra/agent_query.py
    import agent_core  # type: ignore[no-redef]

from tair import config  # noqa: E402


class ToolUnavailable(RuntimeError):
    """缺少 OSS / Tablestore 配置。"""


def _emit(payload: dict, compact: bool) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=None if compact else 2)
    sys.stdout.write("\n")


def _fail(message: str, code: int = 2) -> int:
    json.dump({"ok": False, "error": message}, sys.stderr, ensure_ascii=False)
    sys.stderr.write("\n")
    return code


def _require_docstore(settings) -> None:
    if settings.missing_docstore:
        raise ToolUnavailable("文档存储未配置：" + "、".join(settings.missing_docstore))


def _expires(value: int) -> int:
    if not 1 <= value <= agent_core.MAX_EXPIRES_HOURS:
        raise ValueError(f"expires 必须在 1 到 {agent_core.MAX_EXPIRES_HOURS} 小时之间")
    return value


def _download(url: str, out: str) -> tuple[Path, int]:
    path = Path(out).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urlopen(url, timeout=60) as response, path.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except HTTPError as exc:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"下载失败：HTTP {exc.code}") from exc
    except (URLError, OSError) as exc:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"下载失败：{exc}") from exc
    return path, path.stat().st_size


def _add_query_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="TAG",
        help="标签过滤；可重复，也可用逗号分隔（如 env 或 paper,agents）",
    )
    parser.add_argument("--tag-mode", choices=("any", "all"), default="any")
    parser.add_argument("--category", default="", help="文档分类精确过滤")
    parser.add_argument("--q", default="", help="文件名/描述/计划字段的子串搜索")
    parser.add_argument("--since", type=int, help="create_time 下限（毫秒时间戳）")
    parser.add_argument("--until", type=int, help="create_time 上限（毫秒时间戳）")
    parser.add_argument("--github-project-name", default="", help="仅 plans 使用")
    parser.add_argument("--github-project-branch", default="", help="仅 plans 使用")
    parser.add_argument("--limit", type=int, default=agent_core.DEFAULT_LIMIT)
    parser.add_argument("--offset", type=int, default=0)


def _query_filters(args: argparse.Namespace) -> dict:
    return {
        "tags": args.tag,
        "tag_mode": args.tag_mode,
        "category": args.category,
        "query": args.q,
        "since": args.since,
        "until": args.until,
        "project_name": args.github_project_name,
        "branch": args.github_project_branch,
        "limit": args.limit,
        "offset": args.offset,
    }


class _Parser(argparse.ArgumentParser):
    """参数错误也输出 JSON，方便 Agent 统一解析。"""

    def error(self, message: str) -> None:
        json.dump({"ok": False, "error": message}, sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    common = _Parser(add_help=False)
    common.add_argument("--compact", action="store_true", help="输出单行 JSON")

    parser = _Parser(description="chip-230 一次性只读查询工具（不启动 Web 服务）")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    sub.add_parser("health", parents=[common], help="检查配置并列出可查询表")
    sub.add_parser("tables", parents=[common], help="列出表结构")

    rows = sub.add_parser("rows", parents=[common], help="查询任意白名单表")
    rows.add_argument("table", help="真实表名或逻辑名：oss_documents / davine / plans")
    _add_query_args(rows)

    documents = sub.add_parser("documents", parents=[common], help="查询 oss_documents")
    _add_query_args(documents)

    plans = sub.add_parser("plans", parents=[common], help="查询 plans")
    _add_query_args(plans)

    document = sub.add_parser("document", parents=[common], help="取单份文档元数据和下载链接")
    document.add_argument("doc_id")
    document.add_argument("--source", choices=("documents", "davine"), default="documents")
    document.add_argument("--expires", type=int, default=1, help="下载链接有效期（小时）")

    plan = sub.add_parser("plan", parents=[common], help="取单条计划和下载链接")
    plan.add_argument("plan_uuid")
    plan.add_argument("plan_id", type=int)
    plan.add_argument("--expires", type=int, default=1, help="下载链接有效期（小时）")

    download_doc = sub.add_parser(
        "download-document", parents=[common], help="下载单份文档到本地文件"
    )
    download_doc.add_argument("doc_id")
    download_doc.add_argument("--source", choices=("documents", "davine"), default="documents")
    download_doc.add_argument("--expires", type=int, default=1)
    download_doc.add_argument("--out", required=True, help="输出文件路径")

    download_plan = sub.add_parser("download-plan", parents=[common], help="下载单条计划到本地文件")
    download_plan.add_argument("plan_uuid")
    download_plan.add_argument("plan_id", type=int)
    download_plan.add_argument("--expires", type=int, default=1)
    download_plan.add_argument("--out", required=True, help="输出文件路径")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = config.load_settings()

    try:
        if args.command == "health":
            _emit(
                {
                    "ok": True,
                    "docstore": settings.has_docstore,
                    "missing": settings.missing_docstore,
                    "tables": [spec["name"] for spec in agent_core.table_specs(settings)],
                },
                args.compact,
            )
            return 0

        if args.command == "tables":
            tables = [
                {key: value for key, value in spec.items() if key != "query_endpoint"}
                for spec in agent_core.table_specs(settings)
            ]
            _emit({"ok": True, "tables": tables}, args.compact)
            return 0

        _require_docstore(settings)

        if args.command == "rows":
            result = agent_core.query_table(settings, args.table, **_query_filters(args))
            _emit({"ok": True, **result}, args.compact)
            return 0

        if args.command == "documents":
            result = agent_core.query_table(settings, settings.ts_table, **_query_filters(args))
            documents = result.pop("rows")
            _emit({"ok": True, **result, "documents": documents}, args.compact)
            return 0

        if args.command == "plans":
            result = agent_core.query_table(settings, "plans", **_query_filters(args))
            plans = result.pop("rows")
            _emit({"ok": True, **result, "plans": plans}, args.compact)
            return 0

        if args.command == "document":
            result = agent_core.get_document(
                settings, args.source, args.doc_id, _expires(args.expires)
            )
            _emit({"ok": True, **result}, args.compact)
            return 0

        if args.command == "plan":
            result = agent_core.get_plan(
                settings, args.plan_uuid, args.plan_id, _expires(args.expires)
            )
            _emit({"ok": True, **result}, args.compact)
            return 0

        if args.command == "download-document":
            result = agent_core.get_document(
                settings, args.source, args.doc_id, _expires(args.expires)
            )
            path, size = _download(result["download_url"], args.out)
            _emit(
                {
                    "ok": True,
                    "path": str(path),
                    "bytes": size,
                    "source": result["source"],
                    "table": result["table"],
                    "id": result["document"].get("id"),
                    "name": result["document"].get("name"),
                    "expires_at": result["expires_at"],
                },
                args.compact,
            )
            return 0

        if args.command == "download-plan":
            result = agent_core.get_plan(
                settings, args.plan_uuid, args.plan_id, _expires(args.expires)
            )
            path, size = _download(result["download_url"], args.out)
            _emit(
                {
                    "ok": True,
                    "path": str(path),
                    "bytes": size,
                    "table": "plans",
                    "uuid": result["plan"].get("uuid"),
                    "id": result["plan"].get("id"),
                    "name": result["plan"].get("plan_name"),
                    "expires_at": result["expires_at"],
                },
                args.compact,
            )
            return 0

        return _fail(f"未知命令：{args.command}", 2)
    except ToolUnavailable as exc:
        return _fail(str(exc), 3)
    except KeyError as exc:
        return _fail(str(exc).strip("'"), 4)
    except ValueError as exc:
        return _fail(str(exc), 2)
    except Exception as exc:  # noqa: BLE001 - CLI 把错误转成 JSON，便于 Agent 解析
        return _fail(f"内部错误：{exc}", 1)


if __name__ == "__main__":
    raise SystemExit(main())
