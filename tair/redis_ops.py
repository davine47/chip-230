"""看板对 Redis 的写操作。单独放一个模块，方便不起 Flask 也能测试。"""

from __future__ import annotations

import redis

# 看板预览字符串的上限（按字节，和 STRLEN / GETRANGE 口径一致）
MAX_VALUE_CHARS = 100_000


def write_string_key(
    client: redis.Redis,
    name: str,
    value: str,
    ttl: int | None = None,
    create: bool = False,
    max_chars: int = MAX_VALUE_CHARS,
) -> None:
    """安全地写一个 String Key。

    以前的实现直接 SET，有三个坑，这里都堵上了：
      1. 值超过预览上限时，前端拿到的是截断内容，保存回去会把后半段永久覆盖掉 → 拒绝写入
      2. 同名 Key 是 hash/list 等其他类型时，SET 会把整个结构替换成字符串 → 拒绝写入
      3. SET 和 EXPIRE 分两步，中间有一个没有 TTL 的窗口 → 合成一条 SET ... EX

    检查和写入放在 WATCH 事务里：检查之后、写入之前如果 Key 被别人改了，整笔放弃。
    create=True 表示「新建」，Key 已存在时报错而不是悄悄覆盖。
    """
    if not isinstance(value, str):
        raise ValueError("value 必须是字符串")
    if ttl is not None and ttl <= 0:
        raise ValueError("TTL 必须是正整数，留空表示永不过期")

    with client.pipeline(transaction=True) as pipe:
        try:
            pipe.watch(name)
            kind = pipe.type(name)
            if kind != "none":
                if create:
                    raise ValueError(
                        f"Key「{name}」已存在，新建不会覆盖已有数据；要改请打开它再编辑"
                    )
                if kind != "string":
                    raise ValueError(f"Key「{name}」是 {kind} 类型，看板只能写 String，拒绝覆盖")
                if pipe.strlen(name) > max_chars:
                    raise ValueError(
                        f"Key「{name}」的值超过 {max_chars} 字节，看板只拿到了截断内容，"
                        "为防止覆盖丢数据，拒绝保存；请用 redis-cli 处理"
                    )
            pipe.multi()
            if ttl:
                pipe.set(name, value, ex=ttl)
            else:
                pipe.set(name, value)  # 不带 EX 的 SET 本身就会清掉旧 TTL，不用再 PERSIST
            pipe.execute()
        except redis.WatchError:
            raise ValueError("保存期间这个 Key 被别处修改了，请刷新后重试") from None
