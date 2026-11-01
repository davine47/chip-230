"""数据面：用 redis-py 连阿里云 Tair（兼容 Redis）实例。

阿里云只提供「标准 Redis 协议」的接入地址，所以本地开发直接用 redis-py 即可，
命令的支持范围以官方《命令支持范围与使用限制》为准。
"""

from __future__ import annotations

import redis

from config import Settings


def build_client(
    settings: Settings,
    host: str | None = None,
    port: int | None = None,
    use_ssl: bool | None = None,
    connect_timeout: float = 5,
    socket_timeout: float = 5,
) -> redis.Redis:
    """建一个带连接池的客户端（连接池会自动复用 TCP 连接，别每次 new 一个）。"""
    kwargs: dict[str, object] = {
        "host": host or settings.redis_host,
        "port": int(port or settings.redis_port),
        "db": settings.redis_db,  # 集群架构只支持 db 0
        "decode_responses": True,  # 返回 str 而不是 bytes，日志更好看
        "socket_timeout": socket_timeout,
        "socket_connect_timeout": connect_timeout,
        "socket_keepalive": True,
        "health_check_interval": 30,  # 空闲连接复用前先发个 PING，避开断连
    }
    # 自定义账号（6.0 及以上支持）才需要 username，默认账号只填 password
    if settings.redis_username:
        kwargs["username"] = settings.redis_username
    if settings.redis_password:
        kwargs["password"] = settings.redis_password

    ssl_on = settings.redis_ssl if use_ssl is None else use_ssl
    if ssl_on:
        kwargs.update(ssl=True)
        # 阿里云 TLS 用的是服务端证书；演示环境先关校验，生产建议配置 CA。
        kwargs.update(ssl_cert_reqs="none")

    return redis.Redis(**kwargs)


def server_summary(client: redis.Redis) -> dict[str, str]:
    """取几项基础信息，用来确认连上的是哪个实例、什么版本。

    INFO 属于运维类命令，个别代理/受限账号可能禁用，所以失败了也不影响主流程。
    """
    try:
        info = client.info("server")
    except Exception:  # noqa: BLE001 - 拿不到就算了，不阻断业务
        return {"redis_version": "未知（INFO 不可用）", "role": "-", "tcp_port": "-"}
    return {
        "redis_version": str(info.get("redis_version", "?")),
        "role": str(info.get("role", "?")),
        "tcp_port": str(info.get("tcp_port", "?")),
    }
