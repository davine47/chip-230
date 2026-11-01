"""配置中心：控制面（AccessKey）与数据面（实例地址/密码）统一从这里读取。

取值优先级：环境变量 > 项目根目录下的 .env > 代码里的默认值。
所以你把 key 写进 .env 或 export 到终端，都可以，代码不用改。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent  # 仓库根目录（.env 在这里）

# 没配 AccessKey 时给出的提示，01/03/00 三个脚本共用
MISSING_AK_HINT = """
[!] 还没有配置 AccessKey（OpenAPI 调用需要）。任选一种方式：

    方式一（推荐）：cp .env.template .env，再编辑 .env 填入
                    ALIBABA_CLOUD_ACCESS_KEY_ID=...
                    ALIBABA_CLOUD_ACCESS_KEY_SECRET=...

    方式二：当前终端里 export
                    export ALIBABA_CLOUD_ACCESS_KEY_ID=<AccessKey ID>
                    export ALIBABA_CLOUD_ACCESS_KEY_SECRET=<AccessKey Secret>

    前提：用的是 RAM 子账号，且已附加 AliyunKvstoreReadOnlyAccess 策略。
""".strip()

# 没配实例地址时的提示
MISSING_REDIS_HINT = """
[!] 还没有配置实例连接信息（数据面读写需要）。请在 .env 里填：

    REDIS_HOST=r-xxxxxxxx.redis.rds.aliyuncs.com   # 控制台「连接信息」里的连接地址
    REDIS_PORT=6379
    REDIS_PASSWORD=<实例密码>

    注意：默认只有 VPC 内网地址，本机调试需要先在控制台申请「公网连接地址」，
          并把本机出口 IP 加进实例白名单。
""".strip()

CONNECTION_HINT = """
[!] 连接失败的常见原因，逐条排查：

    1. 地址/端口不对  —— 用控制台「连接信息」里的地址，端口默认 6379
    2. 密码或账号不对 —— 实例密码，或自定义账号需要同时填 REDIS_USERNAME
    3. 白名单没放行   —— 控制台「白名单设置」加上你的出口 IP（默认只放行 127.0.0.1）
    4. 网络不通       —— 内网地址只能从同 VPC 的 ECS 访问；本机请用公网地址
    5. 开了 TLS       —— 需要 REDIS_SSL=true
""".strip()


def _load_dotenv() -> None:
    """装了 python-dotenv 就自动加载 .env；没装也能靠系统环境变量跑。"""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    env_file = ROOT_DIR / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"[x] 环境变量 {name}={raw!r} 不是合法整数") from None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    """一份配置对象，控制面和数据面都在里面。"""

    # ---------- 控制面：阿里云 AccessKey，给 OpenAPI 用 ----------
    access_key_id: str
    access_key_secret: str
    region_id: str
    endpoint: str  # 留空则由 SDK 按 RegionId 自动解析（一般不需要填）
    instance_id: str  # 想固定操作的实例 ID，可留空
    # ---------- 数据面：Redis 协议连接信息 ----------
    redis_host: str
    redis_port: int
    redis_username: str
    redis_password: str
    redis_db: int
    redis_ssl: bool

    @property
    def has_access_key(self) -> bool:
        return bool(self.access_key_id and self.access_key_secret)

    @property
    def has_redis(self) -> bool:
        return bool(self.redis_host)

    def masked_ak(self) -> str:
        """打印日志用的脱敏 AccessKey，永远不要把完整 AK 打进日志。"""
        ak = self.access_key_id
        if not ak:
            return "(空)"
        return f"{ak[:6]}****{ak[-4:]}" if len(ak) > 12 else "****"


def load_settings() -> Settings:
    return Settings(
        access_key_id=_env("ALIBABA_CLOUD_ACCESS_KEY_ID"),
        access_key_secret=_env("ALIBABA_CLOUD_ACCESS_KEY_SECRET"),
        region_id=_env("ALIBABA_CLOUD_REGION_ID", "cn-hangzhou"),
        endpoint=_env("ALIBABA_CLOUD_ENDPOINT"),
        instance_id=_env("TAIR_INSTANCE_ID"),
        redis_host=_env("REDIS_HOST"),
        redis_port=_env_int("REDIS_PORT", 6379),
        redis_username=_env("REDIS_USERNAME"),
        redis_password=_env("REDIS_PASSWORD"),
        redis_db=_env_int("REDIS_DB", 0),
        redis_ssl=_env_bool("REDIS_SSL", False),
    )
