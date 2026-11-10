#!/usr/bin/env python3
"""web/app.py —— chip-230 工作台：Redis 键空间与实例、文档库、davine、Plan 库。

用法：
    ./.venv/bin/python web/app.py                   # http://127.0.0.1:8080
    WEB_PORT=8090 ./.venv/bin/python web/app.py     # 换端口

注意：这个服务能直接改线上实例的数据，默认只监听 127.0.0.1，
      不要把 WEB_HOST 改成 0.0.0.0 暴露到公网。
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import redis
from flask import Flask, jsonify, redirect, render_template, request

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR.parent))
sys.path.insert(0, str(BASE_DIR))

import guard  # noqa: E402  web/guard.py：Host 白名单 + 防跨站写

from tair import config, redis_conn, redis_ops  # noqa: E402  （要在 sys.path 之后导入）
from tair import docs as docs_service  # noqa: E402
from tair import plans as plans_service  # noqa: E402
from tair.openapi import describe_instances_overview  # noqa: E402

SETTINGS = config.load_settings()

MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 全局硬上限，各看板另有更小的限制
DOC_LIMIT_MB = {"documents": 25, "davine": 512}
PLAN_MAX_BYTES = 5 * 1024 * 1024  # 计划只收 .md，5MB 足够；别沿用 512MB 的全局上限
MULTIPART_SLACK = 2 * 1024 * 1024  # multipart 表单除文件外的开销，粗筛 Content-Length 时留的余量

# 上传成功后顺带签一条临时下载链接。OSS 签名 URL 最长 7 天（168 小时），超过会被服务端拒掉。
EXPIRY_OPTIONS = [(1, "1 小时"), (6, "6 小时"), (24, "24 小时"), (72, "3 天"), (168, "7 天")]
EXPIRY_DEFAULT_HOURS = 24.0
EXPIRY_MAX_HOURS = 168.0

# 浏览器直传 OSS：默认开启；bucket 没配 CORS 时前端会自动退回服务器中转。设 0 强制走中转。
DIRECT_UPLOAD = os.getenv("OSS_DIRECT_UPLOAD", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
DIRECT_UPLOAD_TTL = 2 * 3600  # 发出去的直传凭证多久内有效（秒）

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)

# 改模板/静态文件后不用重启（Flask 默认只在 debug 下自动重载模板）
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
app.json.ensure_ascii = False  # 中文直接输出，别转成 \uXXXX
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

ALLOWED_HOSTS = guard.allowed_hostnames()


@app.before_request
def enforce_local_only():
    """所有请求先过这一关：Host 必须是本机名；写请求必须带防跨站头。原理见 web/guard.py。"""
    reason = guard.check_request(
        request.method,
        request.headers.get("Host"),
        request.headers.get("Origin"),
        request.headers.get(guard.CSRF_HEADER),
        ALLOWED_HOSTS,
    )
    if reason:
        return jsonify({"error": reason}), 403
    return None


# 连接与实例信息都做轻量缓存，避免每刷新一次页面就打一次 OpenAPI / PING
_client: redis.Redis | None = None
_instance_cache: dict[str, object] = {"ts": 0.0, "data": None, "ttl": 0.0}
STATUS_CACHE: dict[str, object] = {"ts": 0.0, "data": None, "ttl": 0.0}
DOC_COUNT_CACHE: dict[str, object] = {"ts": 0.0, "count": 0, "latest": []}
PLAN_COUNT_CACHE: dict[str, object] = {"ts": 0.0, "count": 0, "latest": []}
DAVINE_COUNT_CACHE: dict[str, object] = {"ts": 0.0, "count": 0, "latest": []}
DOC_COUNT_TTL = 30
FEED_LIMIT = 6  # 首页「最近的改动」最多几条
INSTANCE_TTL = 300  # 秒
STATUS_TTL_OK = 3
STATUS_TTL_FAIL = 15
STATUS_HARD_TIMEOUT = 3.0  # 秒：探测最长等这么久，绝不把页面拖住
PROBE = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="redis-probe")
MAX_KEYS = 200
SCAN_BATCH = 500  # 每轮 SCAN 的 COUNT
SCAN_MAX_ROUNDS = 40  # 每次请求最多 SCAN 这么多轮（约 2 万个 Key），稀疏匹配时不至于扫完整个库
MAX_VALUE_CHARS = redis_ops.MAX_VALUE_CHARS  # 预览上限；超过的值看板只读，见 write_string_key
LIST_LIMIT = 200  # 列表页最多返回多少条（总数另给）


def get_client(refresh: bool = False) -> redis.Redis:
    """页面用的客户端：超时压到 2.5s，Redis 挂掉时接口能快速报错而不是一直等。"""
    global _client
    if _client is None or refresh:
        _client = redis_conn.build_client(SETTINGS, connect_timeout=2.5, socket_timeout=2.5)
    return _client


def reset_client() -> None:
    global _client
    _client = None


# --------------------------------------------------------------------------
# 数据采集
# --------------------------------------------------------------------------
def _probe_redis() -> dict:
    """真正去打 Redis 的函数，跑在线程里，超时由上层兜住。"""
    data = {
        "ok": False,
        "host": SETTINGS.redis_host or "(未配置)",
        "port": SETTINGS.redis_port,
        "latency_ms": None,
        "version": "?",
        "dbsize": None,
        "error": "",
    }
    started = time.perf_counter()
    client = None
    try:
        client = redis_conn.build_client(SETTINGS, connect_timeout=1.5, socket_timeout=1.5)
        client.ping()
        data["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        data["ok"] = True
        try:
            data["version"] = str(client.info("server").get("redis_version", "?"))
        except Exception:  # noqa: BLE001 - INFO 个别代理不支持，不影响主流程
            pass
        try:
            data["dbsize"] = client.dbsize()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        data["error"] = str(exc)[:200]
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
    return data


def redis_status(force: bool = False) -> dict:
    """带缓存和硬超时的状态探测。

    白名单没放行 / 网络不通时，TCP 连接可能要几十秒才失败；
    这里用线程 + 3s 超时兜住，失败结果缓存 15s，保证页面永远能秒开。
    """
    now = time.time()
    cached = STATUS_CACHE["data"]
    if not force and cached and now - float(STATUS_CACHE["ts"]) < float(STATUS_CACHE["ttl"]):
        return cached  # type: ignore[return-value]

    data: dict = {
        "ok": False,
        "host": SETTINGS.redis_host or "(未配置)",
        "port": SETTINGS.redis_port,
        "latency_ms": None,
        "version": "?",
        "dbsize": None,
        "error": "",
    }
    if not SETTINGS.redis_host:
        data["error"] = "未配置 REDIS_HOST"
    else:
        future = PROBE.submit(_probe_redis)
        try:
            data = future.result(timeout=STATUS_HARD_TIMEOUT)
        except concurrent.futures.TimeoutError:
            data["error"] = f"连接超时（{STATUS_HARD_TIMEOUT:g}s 内没连上）"
            reset_client()
        except Exception as exc:  # noqa: BLE001
            data["error"] = str(exc)[:200]
            reset_client()

    STATUS_CACHE.update(
        ts=time.time(), data=data, ttl=STATUS_TTL_OK if data["ok"] else STATUS_TTL_FAIL
    )
    return data


def instance_summary(force: bool = False) -> dict:
    """调 OpenAPI 拿实例概览，缓存 5 分钟。"""
    now = time.time()
    cached = _instance_cache["data"]
    cached_ttl = float(_instance_cache["ttl"] or INSTANCE_TTL)
    if not force and cached and now - float(_instance_cache["ts"]) < cached_ttl:
        return cached  # type: ignore[return-value]

    summary = {
        "ok": False,
        "error": "",
        "id": "",
        "name": "",
        "status": "",
        "engine": "",
        "class": "",
        "capacity": 0,
        "region": SETTINGS.region_id,
        "zone": "",
        "charge": "",
        "created": "",
        "address": "",
    }
    if not SETTINGS.has_access_key:
        summary["error"] = "未配置 AccessKey"
        return summary

    try:
        items = describe_instances_overview(SETTINGS).get("Instances") or []
        if not items:
            summary["error"] = "该地域没有实例"
            return summary
        target = items[0]
        if SETTINGS.instance_id:
            target = next((i for i in items if i.get("InstanceId") == SETTINGS.instance_id), target)
        summary["ok"] = True
        summary["id"] = target.get("InstanceId", "")
        summary["name"] = target.get("InstanceName", "")
        summary["status"] = target.get("InstanceStatus", "")
        summary["engine"] = target.get("EngineVersion", "")
        summary["class"] = target.get("InstanceClass", "")
        summary["capacity"] = target.get("Capacity", 0)
        summary["region"] = target.get("RegionId", SETTINGS.region_id)
        summary["zone"] = target.get("ZoneId", "")
        summary["charge"] = target.get("ChargeType", "")
        summary["created"] = target.get("CreateTime", "")
        summary["address"] = target.get("ConnectionDomain", "")
        _instance_cache.update(ts=now, data=summary, ttl=INSTANCE_TTL)
    except Exception as exc:  # noqa: BLE001
        summary["error"] = str(exc)[:200]
        # 失败只缓存 30s，别让一次网络抖动把实例页钉死 5 分钟
        _instance_cache.update(ts=now, data=summary, ttl=30)
    return summary


def read_value(client: redis.Redis, key: str) -> tuple[str, str, bool]:
    """按类型读出可展示的值，返回 (类型, 文本, 是否被截断)。"""
    kind = client.type(key)
    if kind == "none":
        raise KeyError("Key 不存在")

    if kind == "string":
        # 超大字符串只取前 N 个字符，别把几十 MB 的值整段拉到内存里
        if client.strlen(key) > MAX_VALUE_CHARS:
            return kind, client.getrange(key, 0, MAX_VALUE_CHARS - 1) or "", True
        return kind, client.get(key) or "", False

    if kind == "hash":
        raw = client.hgetall(key)
        return kind, _dump(raw), len(raw) > 2000
    if kind == "list":
        raw = client.lrange(key, 0, 1999)
        return kind, _dump(raw), client.llen(key) > len(raw)
    if kind == "set":
        raw = sorted(client.smembers(key))
        return kind, _dump(raw[:2000]), len(raw) > 2000
    if kind == "zset":
        raw = client.zrange(key, 0, 1999, withscores=True)
        return kind, _dump([{"member": m, "score": s} for m, s in raw]), False
    if kind == "stream":
        raw = client.xrange(key, "-", "+", count=100)
        return kind, _dump([{"id": i, "fields": f} for i, f in raw]), True
    return kind, "(该类型暂不支持预览)", False


def _dump(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _logged_error(exc: Exception, public: str, status: int):
    """内部异常：完整信息只进服务端日志，前端只拿到通用说明 + 错误号，凭错误号去日志里查。

    云 SDK 的异常文本里常带 endpoint、bucket、RequestId、内部路径等，不该原样回给浏览器。
    """
    err_id = uuid.uuid4().hex[:8]
    app.logger.error("[%s] %s %s 失败", err_id, request.method, request.path, exc_info=exc)
    return jsonify({"error": f"{public}（错误号 {err_id}，详情见服务端日志）"}), status


def api_view(fn):
    """把异常统一转成 JSON 错误，前端好统一 toast。

    只有「我们自己写的、面向用户的」提示原样返回（KeyError / ValueError / Redis 服务端回复）；
    其余一律走 _logged_error，不把内部细节暴露给前端。
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KeyError as exc:
            return jsonify({"error": str(exc).strip("'")}), 404
        except redis.ResponseError as exc:
            # Redis 服务端的回复（如 ERR unknown command / WRONGTYPE），对排障有用、不含内部信息
            return jsonify({"error": f"Redis 拒绝了这条命令：{exc}"}), 502
        except redis.RedisError as exc:
            reset_client()
            return _logged_error(exc, "连接 Redis 失败，检查白名单或网络", 502)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return _logged_error(exc, "服务器内部错误", 500)

    return wrapper


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
@app.context_processor
def inject_globals() -> dict:
    return {
        "status": redis_status(),
        "rendered": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


@app.get("/")
def board():
    boards = (
        ("文档库", "/documents", "#10b981", document_stats()),
        ("davine", "/davine", "#0ea5e9", davine_stats()),
        ("Plan 库", "/plans", "#8b5cf6", plan_stats()),
    )
    feed = [
        {**item, "label": label, "href": href, "color": color}
        for label, href, color, stats in boards
        for item in stats["latest"]
    ]
    feed.sort(key=lambda item: item["create_time"], reverse=True)
    return render_template(
        "index.html",
        docs=boards[0][3],
        davine=boards[1][3],
        plans=boards[2][3],
        feed=feed[:FEED_LIMIT],
        ago=humanize_ago,
    )


def humanize_ago(ms: int) -> str:
    """把毫秒时间戳说成「3 分钟前」。首页在服务端渲染，省得再为相对时间跑一趟接口。"""
    if not ms:
        return "时间未知"
    minutes = max(0, int((time.time() * 1000 - int(ms)) / 60000))
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = round(minutes / 60)
    if hours < 24:
        return f"{hours} 小时前"
    return f"{round(hours / 24)} 天前"


def _stats(cache: dict, loader, name_key: str) -> dict:
    """首页卡片用：一次扫描同时得到总量和最近几条，结果缓存 30 秒。

    以前首页先渲染计数（各扫一次），前端再打三个列表接口（又各扫一次），
    一屏最多扫 6 遍表；现在一次扫描把两件事都办了。
    """
    if SETTINGS.missing_docstore:
        return {
            "ok": False,
            "count": 0,
            "latest": [],
            "error": f"未配置 {'、'.join(SETTINGS.missing_docstore)}",
        }
    now = time.time()
    cached = cache.get("latest")
    if now - float(cache["ts"]) < DOC_COUNT_TTL and cached is not None:
        return {"ok": True, "count": int(cache["count"]), "latest": cached}
    try:
        items = loader()
        latest = [
            {
                "name": item.get(name_key) or "(未命名)",
                "create_time": int(item.get("create_time") or 0),
            }
            for item in items[:FEED_LIMIT]
        ]
        cache.update(ts=now, count=len(items), latest=latest)
        return {"ok": True, "count": len(items), "latest": latest}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "count": 0, "latest": [], "error": str(exc)[:120]}


def document_stats() -> dict:
    return _stats(DOC_COUNT_CACHE, lambda: docs_service.documents(SETTINGS).scan()[0], "name")


def davine_stats() -> dict:
    return _stats(DAVINE_COUNT_CACHE, lambda: docs_service.davine(SETTINGS).scan()[0], "name")


DOC_PAGES = {
    "documents": {
        "accent": "#10b981",
        "texture": "rows",  # 与首页功能区同一种版图纹理
        "title": "文档库",
        "subtitle": "OSS + Tablestore",
        "desc": (
            "文件上传到 OSS 的 agents/docs/，同时在 Tablestore 的 oss_documents 表里写一行索引"
            "（文件名、分类、描述、地址、时间）。列表支持「打开」和「删除」。"
        ),
        "api": "/api/docs",
        "categories": None,
        "desc_required": True,
        "limit_mb": DOC_LIMIT_MB["documents"],
    },
    "davine": {
        "accent": "#0ea5e9",
        "texture": "hatch",
        "title": "davine",
        "subtitle": "OSS + Tablestore",
        "desc": (
            "和文档库同一套逻辑：文件写到 OSS 的 davine_exchange/，元数据写进 Tablestore 的"
            " davine_oss_documents 表，字段与文档库完全一致。"
        ),
        "api": "/api/davine",
        "categories": ["others", "paper", "books", "agents"],
        "desc_required": False,
        "limit_mb": DOC_LIMIT_MB["davine"],
        # davine 上传成功后会返回一条临时下载链接，有效期由上传时选
        "expiry": True,
        "expiry_options": EXPIRY_OPTIONS,
        "expiry_default": int(EXPIRY_DEFAULT_HOURS),
    },
}


def render_doc_page(kind: str, stats: dict):
    conf = DOC_PAGES[kind]
    store = (docs_service.documents if kind == "documents" else docs_service.davine)(SETTINGS)
    return render_template(
        "docstore.html",
        conf=conf,
        stats=stats,
        bucket=SETTINGS.oss_bucket,
        prefix=store.prefix,
        table=store.table,
    )


@app.get("/documents")
def documents_page():
    return render_doc_page("documents", document_stats())


@app.get("/davine")
def davine_page():
    return render_doc_page("davine", davine_stats())


def plan_stats() -> dict:
    return _stats(PLAN_COUNT_CACHE, lambda: plans_service.scan_plans(SETTINGS)[0], "plan_name")


@app.get("/plans")
def plans_page():
    return render_template(
        "plans.html",
        stats=plan_stats(),
        limit_mb=PLAN_MAX_BYTES // 1024 // 1024,
        bucket=SETTINGS.oss_bucket,
        prefix=plans_service.plans_prefix(SETTINGS),
        table=plans_service.TABLE,
    )


@app.get("/redis")
def redis_page():
    """Redis 二级看板：键空间（默认页）。"""
    return render_template("redis.html", instance=instance_summary())


@app.get("/redis/instance")
def redis_instance_page():
    return render_template("instance.html", instance=instance_summary(force=True))


@app.get("/redis/commands")
def redis_commands_page():
    return render_template(
        "commands.html", groups=COMMAND_GROUPS, versions=VERSION_NOTES, limits=LIMITS
    )


# 老地址 301 到二级看板下的新路径，避免收藏夹失效
@app.get("/instance")
def legacy_instance_page():
    return redirect("/redis/instance", code=301)


@app.get("/commands")
def legacy_commands_page():
    return redirect("/redis/commands", code=301)


# --------------------------------------------------------------------------
# Redis CRUD API
# --------------------------------------------------------------------------
@app.get("/api/keys")
@api_view
def api_keys():
    pattern = (request.args.get("pattern") or "*").strip() or "*"
    limit = min(int(request.args.get("limit", MAX_KEYS)), 1000)

    client = get_client()
    keys: list[str] = []
    # 手动推进游标并限制轮数：scan_iter 在匹配很稀疏时会一直扫到库尾，大库上请求会被拖很久
    cursor = max(0, int(request.args.get("cursor", 0)))
    rounds = 0
    while True:
        cursor, batch = client.scan(cursor=cursor, match=pattern, count=SCAN_BATCH)
        keys.extend(batch)
        rounds += 1
        if cursor == 0 or len(keys) >= limit or rounds >= SCAN_MAX_ROUNDS:
            break
    complete = cursor == 0 and len(keys) <= limit
    keys = keys[:limit]

    rows = []
    if keys:
        pipe = client.pipeline(transaction=False)
        for key in keys:
            pipe.type(key)
            pipe.ttl(key)
        result = pipe.execute()
        for idx, key in enumerate(keys):
            rows.append({"key": key, "type": result[idx * 2], "ttl": result[idx * 2 + 1]})
        rows.sort(key=lambda row: row["key"])

    return jsonify(
        {
            "keys": rows,
            "total": client.dbsize(),
            "returned": len(rows),
            "pattern": pattern,
            # complete=False：没扫完（达到条数或轮数上限），可带 cursor=next_cursor 继续
            "complete": complete,
            "next_cursor": 0 if complete else cursor,
            "scanned_rounds": rounds,
        }
    )


@app.get("/api/key")
@api_view
def api_get_key():
    name = request.args.get("name", "")
    if not name:
        raise ValueError("缺少 name 参数")
    client = get_client()
    kind, value, truncated = read_value(client, name)
    return jsonify(
        {
            "key": name,
            "type": kind,
            "value": value,
            "ttl": client.ttl(name),
            "truncated": truncated,
        }
    )


@app.put("/api/key")
@api_view
def api_put_key():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("key", "")).strip()
    value = payload.get("value", "")
    ttl_raw = payload.get("ttl")
    create = bool(payload.get("create"))  # 前端「新建」时为 true：已存在就报错，不覆盖

    if not name:
        raise ValueError("Key 不能为空")
    if len(name) > 512:
        raise ValueError("Key 太长（上限 512 字节）")
    ttl = None if ttl_raw in (None, "", 0, "0") else int(ttl_raw)

    client = get_client()
    # 类型检查 + 截断保护 + SET EX 原子写入都在这里，见 tair/redis_ops.py
    redis_ops.write_string_key(client, name, value, ttl=ttl, create=create)
    return jsonify({"ok": True, "key": name, "ttl": client.ttl(name)})


@app.delete("/api/key")
@api_view
def api_delete_key():
    name = request.args.get("name", "")
    if not name:
        raise ValueError("缺少 name 参数")
    deleted = get_client().delete(name)
    return jsonify({"ok": True, "deleted": deleted, "key": name})


def require_docstore() -> None:
    """文档库 / davine / Plan 库都要 OSS bucket + Tablestore 实例；缺了直接说清楚缺什么。"""
    missing = SETTINGS.missing_docstore
    if missing:
        raise ValueError(f"未配置 {'、'.join(missing)}，请在 .env 里补上后重启看板")


class PendingUploads:
    """发给浏览器的直传凭证：upload_id → {看板, key, 文件名, 分类, 描述}。

    complete 时只认这里登记过的 key，浏览器没法让服务端给任意对象写索引。
    只存在内存里：看板重启后未完成的直传作废，留下的对象由 06_reconcile.py 清理。
    """

    def __init__(self, ttl: int):
        self.ttl = ttl
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()

    def add(self, **entry) -> str:
        upload_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._items = {k: v for k, v in self._items.items() if v["expires"] > now}
            self._items[upload_id] = {**entry, "expires": now + self.ttl}
        return upload_id

    def get(self, upload_id: str, board: str) -> dict:
        with self._lock:
            entry = self._items.get(upload_id)
        if not entry or entry["board"] != board or entry["expires"] <= time.time():
            raise KeyError("上传凭证不存在或已过期，请重新上传")
        return entry

    def discard(self, upload_id: str) -> None:
        with self._lock:
            self._items.pop(upload_id, None)


PENDING_UPLOADS = PendingUploads(DIRECT_UPLOAD_TTL)


def _expire_hours(raw) -> float:
    """解析「链接过期时间」（小时）。空值用默认 24 小时，越界直接拒绝。"""
    try:
        hours = float(raw) if raw not in (None, "") else EXPIRY_DEFAULT_HOURS
    except (TypeError, ValueError):
        raise ValueError("链接过期时间必须是小时数") from None
    if not 0 < hours <= EXPIRY_MAX_HOURS:
        raise ValueError(f"链接过期时间必须在 0 ~ {int(EXPIRY_MAX_HOURS)} 小时之间")
    return hours


def _iso_ms(ms: int) -> str | None:
    """毫秒时间戳 → ISO 8601（UTC），前端用 new Date() 按本地时区显示。"""
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _attach_temp_link(doc: dict, hours: float) -> dict:
    """上传结果里补上临时链接和过期时间。

    链接在上传时就签好，连同过期时间（tmp_expire_time）一起存进表里，刷新页面后还能查回来；
    这里直接用存下的过期时间，和签名里的是同一个值，不再另取 now() 推算。
    """
    return {
        **doc,
        "temporary_url": doc.get("tmp_public_url", ""),
        "expires_at": _iso_ms(int(doc.get("tmp_expire_time") or 0)),
        "expires_hours": hours,
    }


def register_doc_api(
    name: str,
    factory,
    cache: dict,
    allowed_categories=None,
    require_description=True,
    max_bytes: int = 25 * 1024 * 1024,
    expiry: bool = False,
):
    """一块文档看板的接口：列表 / 直传（申请 + 确认）/ 中转上传 / 预签名 / 删除。"""

    def check_meta(category: str, description: str) -> None:
        if not category:
            raise ValueError("category 不能为空")
        if require_description and not description:
            raise ValueError("description 不能为空")
        if allowed_categories and category not in allowed_categories:
            raise ValueError(f"category 只能是：{' / '.join(allowed_categories)}")

    def list_view():
        require_docstore()
        docs, truncated = factory(SETTINGS).scan()
        shown = docs[:LIST_LIMIT]
        return jsonify(
            {"documents": shown, "count": len(shown), "total": len(docs), "truncated": truncated}
        )

    def direct_init_view():
        """直传第一步：校验元数据和大小，签发 PUT 地址。OSS_DIRECT_UPLOAD=0 时告诉前端走中转。"""
        require_docstore()
        payload = request.get_json(silent=True) or {}
        filename = str(payload.get("filename") or "").strip()
        category = str(payload.get("category") or "").strip()
        description = str(payload.get("description") or "").strip()
        size = int(payload.get("size") or 0)
        if not filename:
            raise ValueError("请选择要上传的文件")
        check_meta(category, description)
        if size <= 0:
            raise ValueError("文件是空的")
        if size > max_bytes:
            raise ValueError(f"文件超过 {max_bytes // 1024 // 1024}MB 上限")
        if not DIRECT_UPLOAD:
            return jsonify({"mode": "relay"})

        hours = _expire_hours(payload.get("expire_hours")) if expiry else None
        info = factory(SETTINGS).prepare_direct_upload(filename)
        upload_id = PENDING_UPLOADS.add(
            board=name,
            key=info["key"],
            name=info["name"],
            category=category,
            description=description,
            expire_hours=hours,
        )
        return jsonify(
            {
                "mode": "direct",
                "upload_id": upload_id,
                "url": info["url"],
                "method": info["method"],
                "headers": info["headers"],
            }
        )

    def direct_complete_view():
        """直传第二步：浏览器 PUT 完成后调用，核对 OSS 上的对象再写索引。"""
        require_docstore()
        upload_id = str((request.get_json(silent=True) or {}).get("upload_id") or "")
        entry = PENDING_UPLOADS.get(upload_id, board=name)
        try:
            doc = factory(SETTINGS).complete_direct_upload(
                entry["name"],
                entry["key"],
                entry["category"],
                entry["description"],
                max_bytes,
                tmp_hours=entry.get("expire_hours"),
            )
        except ValueError:
            PENDING_UPLOADS.discard(upload_id)  # 对象已被删掉，这张凭证也没用了
            raise
        PENDING_UPLOADS.discard(upload_id)
        if entry.get("expire_hours"):
            doc = _attach_temp_link(doc, entry["expire_hours"])
        cache["ts"] = 0.0
        return jsonify({"ok": True, "document": doc})

    def upload_view():
        """服务器中转上传：直传不可用（bucket 没配 CORS 等）时前端退回这里。"""
        require_docstore()
        # 先按 Content-Length 粗筛，必须在读 request.files 之前：
        # 一访问 request.files，Werkzeug 就会把整个请求体收下来落盘，超限也已经白传了一遍
        if request.content_length and request.content_length > max_bytes + MULTIPART_SLACK:
            raise ValueError(f"文件超过 {max_bytes // 1024 // 1024}MB 上限")

        file = request.files.get("file")
        category = (request.form.get("category") or "").strip()
        description = (request.form.get("description") or "").strip()

        if file is None or not file.filename:
            raise ValueError("请选择要上传的文件")
        check_meta(category, description)

        hours = _expire_hours(request.form.get("expire_hours")) if expiry else None

        # 大文件不能整块读进内存：落到临时文件，再交给 OSS uploader 分片上传
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename).suffix)
        tmp.close()
        try:
            file.save(tmp.name)
            size = os.path.getsize(tmp.name)
            if size == 0:
                raise ValueError("文件是空的")
            if size > max_bytes:
                raise ValueError(f"文件超过 {max_bytes // 1024 // 1024}MB 上限")
            doc = factory(SETTINGS).save_from_file(
                filename=file.filename,
                filepath=tmp.name,
                category=category,
                description=description,
                tmp_hours=hours,
            )
        finally:
            os.unlink(tmp.name)
        if expiry:
            doc = _attach_temp_link(doc, hours)
        cache["ts"] = 0.0  # 让首页计数下次重新取
        return jsonify({"ok": True, "document": doc})

    def link_view(doc_id: str):
        require_docstore()
        return jsonify({"ok": True, "url": factory(SETTINGS).presign(doc_id)})

    def tmp_link_view(doc_id: str):
        """列表里「临时链接」按钮用：把上传时存下的链接读回来，没有就明说。"""
        require_docstore()
        doc = factory(SETTINGS).get(doc_id)
        if not doc:
            raise KeyError("文档不存在")
        url = (doc.get("tmp_public_url") or "").strip()
        if not url:
            return jsonify({"ok": True, "url": "", "message": "无临时链接"})
        expire_ms = int(doc.get("tmp_expire_time") or 0)
        expires_at = _iso_ms(expire_ms)
        if expire_ms and expire_ms <= time.time() * 1000:
            # 过期的链接不清理（按需求保留在表里），但也不再把一个打不开的地址当成有效的发出去
            return jsonify(
                {
                    "ok": True,
                    "url": "",
                    "expired": True,
                    "expires_at": expires_at,
                    "message": "临时链接已过期",
                }
            )
        # expires_at 为 None：这一列加上之前写入的老数据，过期时间未知
        return jsonify({"ok": True, "url": url, "expired": False, "expires_at": expires_at})

    def delete_view(doc_id: str):
        require_docstore()
        deleted = factory(SETTINGS).delete(doc_id)
        cache["ts"] = 0.0
        if not deleted:
            raise KeyError("文档不存在")
        return jsonify({"ok": True, "id": doc_id})

    app.add_url_rule(f"/api/{name}", f"{name}_list", api_view(list_view), methods=["GET"])
    app.add_url_rule(
        f"/api/{name}/upload", f"{name}_upload", api_view(upload_view), methods=["POST"]
    )
    app.add_url_rule(
        f"/api/{name}/upload-url",
        f"{name}_upload_url",
        api_view(direct_init_view),
        methods=["POST"],
    )
    app.add_url_rule(
        f"/api/{name}/complete",
        f"{name}_complete",
        api_view(direct_complete_view),
        methods=["POST"],
    )
    app.add_url_rule(
        f"/api/{name}/<doc_id>/link", f"{name}_link", api_view(link_view), methods=["POST"]
    )
    if expiry:
        app.add_url_rule(
            f"/api/{name}/<doc_id>/tmp-link",
            f"{name}_tmp_link",
            api_view(tmp_link_view),
            methods=["GET"],
        )
    app.add_url_rule(
        f"/api/{name}/<doc_id>", f"{name}_delete", api_view(delete_view), methods=["DELETE"]
    )


register_doc_api(
    "docs",
    docs_service.documents,
    DOC_COUNT_CACHE,
    max_bytes=DOC_LIMIT_MB["documents"] * 1024 * 1024,
)
register_doc_api(
    "davine",
    docs_service.davine,
    DAVINE_COUNT_CACHE,
    DOC_PAGES["davine"]["categories"],
    require_description=DOC_PAGES["davine"]["desc_required"],
    max_bytes=DOC_LIMIT_MB["davine"] * 1024 * 1024,
    expiry=True,
)


@app.get("/api/plans")
@api_view
def api_plans_list():
    require_docstore()
    items, truncated = plans_service.scan_plans(SETTINGS)
    shown = items[:LIST_LIMIT]
    return jsonify(
        {"plans": shown, "count": len(shown), "total": len(items), "truncated": truncated}
    )


@app.post("/api/plans/upload")
@api_view
def api_plans_upload():
    require_docstore()
    # 在读 request.files 之前按 Content-Length 粗筛（原因同 upload_view）
    if request.content_length and request.content_length > PLAN_MAX_BYTES + MULTIPART_SLACK:
        raise ValueError(f"计划文件超过 {PLAN_MAX_BYTES // 1024 // 1024}MB 上限")
    file = request.files.get("file")
    plan_name = (request.form.get("plan_name") or "").strip()
    plan_info = (request.form.get("plan_info") or "").strip()
    trace_id = (request.form.get("trace_id") or "").strip()
    trace_info = (request.form.get("trace_info") or "").strip()
    github_name = (request.form.get("github_project_name") or "").strip()
    github_url = (request.form.get("github_project_url") or "").strip()
    github_branch = (request.form.get("github_project_branch") or "").strip()

    if file is None or not file.filename:
        raise ValueError("请选择要上传的 .md 文件")
    if not file.filename.lower().endswith(".md"):
        raise ValueError("计划只支持 .md 格式")
    if not github_name:
        raise ValueError("github_project_name 不能为空")
    if not github_branch:
        raise ValueError("github_project_branch 不能为空")
    github_url = plans_service.validate_repo_url(github_url)  # 拦 javascript: 之类的链接

    data = file.read(PLAN_MAX_BYTES + 1)  # 最多读上限 +1 字节，够判断是否超限，不会整块读进内存
    if not data:
        raise ValueError("文件是空的")
    if len(data) > PLAN_MAX_BYTES:
        raise ValueError(f"计划文件超过 {PLAN_MAX_BYTES // 1024 // 1024}MB 上限")

    plan = plans_service.save_plan(
        SETTINGS,
        filename=file.filename,
        data=data,
        plan_name=plan_name or file.filename,
        trace_id=trace_id,
        trace_info=trace_info,
        plan_info=plan_info,
        github_project_name=github_name,
        github_project_url=github_url,
        github_project_branch=github_branch,
    )
    PLAN_COUNT_CACHE["ts"] = 0.0
    return jsonify({"ok": True, "plan": plan})


@app.post("/api/plans/<plan_uuid>/<int:plan_id>/link")
@api_view
def api_plans_link(plan_uuid: str, plan_id: int):
    require_docstore()
    url = plans_service.presign_plan(SETTINGS, plan_uuid, plan_id)
    return jsonify({"ok": True, "url": url})


@app.delete("/api/plans/<plan_uuid>/<int:plan_id>")
@api_view
def api_plans_delete(plan_uuid: str, plan_id: int):
    require_docstore()
    deleted = plans_service.delete_plan(SETTINGS, plan_uuid, plan_id)
    PLAN_COUNT_CACHE["ts"] = 0.0
    if not deleted:
        raise KeyError("计划不存在")
    return jsonify({"ok": True})


@app.get("/api/status")
@api_view
def api_status():
    return jsonify({"redis": redis_status(), "instance": instance_summary()})


# --------------------------------------------------------------------------
# 静态参考数据（命令页）
# --------------------------------------------------------------------------
COMMAND_GROUPS = [
    ("String", "SET GET MSET MGET INCR INCRBY SETNX", "2.8+", "缓存、计数、分布式锁"),
    ("Hash", "HSET HGET HGETALL HINCRBY HDEL", "2.8+", "对象字段局部读写"),
    ("List", "LPUSH RPUSH LRANGE LTRIM LPOP", "2.8+", "队列、最新 N 条"),
    ("Set", "SADD SMEMBERS SINTER SINTERSTORE SCARD", "2.8+", "标签、去重、交集"),
    ("Sorted Set", "ZADD ZINCRBY ZREVRANGE ZRANK ZCOUNT", "2.8+", "排行榜、延迟队列"),
    ("Bitmap", "SETBIT GETBIT BITCOUNT BITOP BITFIELD", "2.8+", "签到、布尔标记"),
    ("HyperLogLog", "PFADD PFCOUNT PFMERGE", "2.8+", "UV 去重（固定约 12KB）"),
    ("Geospatial", "GEOADD GEODIST GEOPOS GEOSEARCH", "3.2+", "附近的人、距离"),
    ("Stream", "XADD XREAD XGROUP XACK XLEN", "5.0+", "消息队列、消费组"),
    ("Pub/Sub", "PUBLISH SUBSCRIBE PSUBSCRIBE", "2.8+", "广播（不持久化）"),
    ("Transactions", "MULTI EXEC DISCARD WATCH", "2.8+", "事务"),
    ("Scripting", "EVAL EVALSHA FCALL FUNCTION", "2.8+ / 7.0", "Lua 原子逻辑"),
    ("Connection", "AUTH PING CLIENT SETNAME CLIENT LIST", "2.8+", "连接管理"),
    ("Generic", "DEL EXISTS EXPIRE TTL SCAN TYPE", "2.8+", "Key 通用操作"),
    ("Server", "INFO CONFIG GET DBSIZE", "2.8+", "运维（部分受限）"),
    ("Cluster", "CLUSTER INFO NODES SLOTS KEYSLOT", "集群架构", "集群信息"),
]

VERSION_NOTES = [
    ("5.0", "Stream、ZPOPMIN / ZPOPMAX、BZPOPMIN", "消息队列从这一版开始有"),
    ("6.0", "BITFIELD_RO、CLIENT CACHING / TRACKING、LPOS", "客户端缓存相关能力"),
    (
        "7.0",
        "GETDEL、GETEX、LCS、COPY、SINTERCARD、GEOSEARCH、XAUTOCLAIM、FUNCTION",
        "新增命令最多的一版",
    ),
]

LIMITS = [
    ("MIGRATE", "所有版本都不支持"),
    ("CONFIG SET", "只返回 OK，不会真正修改参数，改参数用控制台"),
    ("ACL 系列", "绝大多数不支持，ACL WHOAMI 仅 7.0 支持"),
    ("SWAPDB", "仅兼容 Redis 7.0 的集群架构实例支持"),
    ("多 Key 命令", "集群架构要求所有 Key 在同一 Slot，用 Hash Tag {前缀} 解决"),
    ("SELECT", "集群架构只有 db 0，命令被接受但切不了库"),
]


if __name__ == "__main__":
    import os

    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8080"))
    print(f"看板已启动： http://{host}:{port}   （Ctrl-C 停止）")
    print(
        f"Redis: {SETTINGS.redis_host or '(未配置)'}:{SETTINGS.redis_port}  地域: {SETTINGS.region_id}"
    )
    app.run(host=host, port=port, debug=False, threaded=True)
