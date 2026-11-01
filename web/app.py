#!/usr/bin/env python3
"""web/app.py —— chip-230 看板：Redis Key 增删改查 + 实例概览。

用法：
    ./.venv/bin/python web/app.py                   # http://127.0.0.1:8080
    WEB_PORT=8090 ./.venv/bin/python web/app.py     # 换端口

注意：这个服务能直接改线上实例的数据，默认只监听 127.0.0.1，
      不要把 WEB_HOST 改成 0.0.0.0 暴露到公网。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

import redis
from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR.parent))

import config  # noqa: E402  （要在 sys.path 之后导入）
import redis_conn  # noqa: E402
from openapi_client import describe_instances_overview  # noqa: E402

SETTINGS = config.load_settings()

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)

# 改模板/静态文件后不用重启（Flask 默认只在 debug 下自动重载模板）
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
app.json.ensure_ascii = False  # 中文直接输出，别转成 \uXXXX

# 连接与实例信息都做轻量缓存，避免每刷新一次页面就打一次 OpenAPI
_client: redis.Redis | None = None
_instance_cache: dict[str, object] = {"ts": 0.0, "data": None}
INSTANCE_TTL = 300  # 秒
MAX_KEYS = 200
MAX_VALUE_CHARS = 100_000


def get_client(refresh: bool = False) -> redis.Redis:
    global _client
    if _client is None or refresh:
        _client = redis_conn.build_client(SETTINGS)
    return _client


def reset_client() -> None:
    global _client
    _client = None


# --------------------------------------------------------------------------
# 数据采集
# --------------------------------------------------------------------------
def redis_status() -> dict:
    """PING + INFO + DBSIZE，页面顶部的状态胶囊和总览卡片都用它。"""
    data = {
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
        return data

    started = time.perf_counter()
    try:
        client = get_client()
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
        data["error"] = str(exc)
        reset_client()
    return data


def instance_summary(force: bool = False) -> dict:
    """调 OpenAPI 拿实例概览，缓存 5 分钟。"""
    now = time.time()
    cached = _instance_cache["data"]
    if not force and cached and now - float(_instance_cache["ts"]) < INSTANCE_TTL:
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
        _instance_cache.update(ts=now, data=summary)
    except Exception as exc:  # noqa: BLE001
        summary["error"] = str(exc)[:200]
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


def api_view(fn):
    """把 Redis / 参数异常统一转成 JSON 错误，前端好统一 toast。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KeyError as exc:
            return jsonify({"error": str(exc).strip("'")}), 404
        except redis.RedisError as exc:
            reset_client()
            return jsonify({"error": f"Redis 错误：{exc}"}), 502
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 500

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
    return render_template("index.html", instance=instance_summary())


@app.get("/redis")
def redis_page():
    return render_template("redis.html", instance=instance_summary())


@app.get("/instance")
def instance_page():
    return render_template("instance.html", instance=instance_summary(force=True))


@app.get("/commands")
def commands_page():
    return render_template(
        "commands.html", groups=COMMAND_GROUPS, versions=VERSION_NOTES, limits=LIMITS
    )


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
    for key in client.scan_iter(match=pattern, count=300):
        keys.append(key)
        if len(keys) >= limit:
            break

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

    if not name:
        raise ValueError("Key 不能为空")
    if len(name) > 512:
        raise ValueError("Key 太长（上限 512 字节）")

    client = get_client()
    client.set(name, value)
    if ttl_raw in (None, "", 0, "0"):
        client.persist(name)
    else:
        seconds = int(ttl_raw)
        if seconds <= 0:
            raise ValueError("TTL 必须是正整数，留空表示永不过期")
        client.expire(name, seconds)
    return jsonify({"ok": True, "key": name, "ttl": client.ttl(name)})


@app.delete("/api/key")
@api_view
def api_delete_key():
    name = request.args.get("name", "")
    if not name:
        raise ValueError("缺少 name 参数")
    deleted = get_client().delete(name)
    return jsonify({"ok": True, "deleted": deleted, "key": name})


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
