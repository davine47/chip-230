#!/usr/bin/env python3
"""02_redis_commands.py —— 数据面：把官方「命令支持表」里的常用命令族跑一遍。

场景：一个商品页的典型缓存玩法
    String  商品快照缓存 + 库存计数
    Hash    商品字段（局部更新、HINCRBY 扣库存）
    List    最近浏览队列（LPUSH/LTRIM 控制长度）
    Set     商品标签、求交集（共同标签）
    ZSet    销量排行榜
    Bitmap  用户签到
    HLL     UV 去重计数
    TTL     过期与持久化
    SET NX  + Lua   分布式锁
    INCR    接口限流
    Pipeline / MULTI  批量写与事务
    SCAN    遍历 Key（生产上别用 KEYS）

用法：
    python 02_redis_commands.py          # 跑完自动清理演示数据
    python 02_redis_commands.py --keep   # 保留数据，方便用 redis-cli 对着看
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 让脚本能 import tair 包

from tair import config
from tair.console import line, section
from tair.redis_conn import build_client, server_summary

# 大括号是 Redis Cluster 的 Hash Tag：同一 Slot 的 Key 才能做多 Key 操作
# （SINTER / ZUNIONSTORE / 事务 在集群架构下要求所有 Key 同 Slot）。
PREFIX = "{tair:demo}"


def K(name: str) -> str:
    return f"{PREFIX}:{name}"


# --------------------------------------------------------------------------
# 1. 连接
# --------------------------------------------------------------------------
def demo_connection(r) -> None:
    section("0. 连接健康检查")
    line("PING", r.ping())
    for key, value in server_summary(r).items():
        line(key, value)
    line("DBSIZE（当前库 Key 数量）", r.dbsize())


# --------------------------------------------------------------------------
# 2. String：缓存 / 计数
# --------------------------------------------------------------------------
def demo_string(r) -> None:
    section("1. String —— 商品快照缓存 + 库存计数")

    product = K("product:1001")
    payload = {"id": 1001, "name": "机械键盘 K8", "price": 399, "stock": 100}
    # SET key value EX 600：带过期时间一起写，一条命令搞定，避免 SET + EXPIRE 中间断连
    r.set(product, json.dumps(payload, ensure_ascii=False), ex=600)
    line("SET product:1001 EX 600", "OK")
    line("GET product:1001", r.get(product))
    line("TTL product:1001", f"{r.ttl(product)} 秒后过期")

    # 整型才可以用 INCR/DECR，写进去的字符串不能自增
    stock = K("stock:1001")
    r.set(stock, 100)
    line("INCRBY stock:1001 +5", r.incrby(stock, 5))
    line("DECRBY stock:1001 -2", r.decrby(stock, 2))
    line("GET stock:1001", r.get(stock))

    # 常用组合：多个 Key 一次读写，减少往返
    r.mset({K("cfg:a"): "1", K("cfg:b"): "2"})
    line("MGET cfg:a cfg:b", r.mget(K("cfg:a"), K("cfg:b")))

    # SETNX：只在不存在时写，用于幂等/抢锁
    line("SET stock:1001 NX（已存在应返回 None）", r.set(stock, 0, nx=True))


# --------------------------------------------------------------------------
# 3. Hash：对象字段
# --------------------------------------------------------------------------
def demo_hash(r) -> None:
    section("2. Hash —— 商品明细字段，局部读写更省带宽")

    key = K("product:1001:detail")
    r.delete(key)
    # HSET 一次写多个字段（redis-py 的 mapping 写法）
    r.hset(key, mapping={"name": "机械键盘 K8", "price": "399", "stock": "100", "brand": "K8"})
    line("HSET detail name/price/stock/brand", "4 个字段")
    line("HGETALL", r.hgetall(key))
    line("HGET price", r.hget(key, "price"))
    line("HINCRBY stock -1（下单扣减）", r.hincrby(key, "stock", -1))
    line("HMGET name/stock", r.hmget(key, ["name", "stock"]))
    line("HLEN", r.hlen(key))
    line("HDEL brand", r.hdel(key, "brand"))


# --------------------------------------------------------------------------
# 4. List：队列 / 最新列表
# --------------------------------------------------------------------------
def demo_list(r) -> None:
    section("3. List —— 最近浏览队列，LTRIM 固定长度")

    key = K("user:42:recent")
    r.delete(key)
    line("RPUSH 1001..1004", r.rpush(key, "1001", "1002", "1003", "1004"))
    # LPUSH：新看的排最前
    line("LPUSH 1005（最新浏览）", r.lpush(key, "1005"))
    line("LRANGE 0 -1（全部）", r.lrange(key, 0, -1))
    line("LRANGE 0 2（前 3 条）", r.lrange(key, 0, 2))
    r.ltrim(key, 0, 2)
    line("LTRIM 0 2 -> LLEN", r.llen(key))
    line("LPOP", r.lpop(key))


# --------------------------------------------------------------------------
# 5. Set：标签 / 去重 / 交集
# --------------------------------------------------------------------------
def demo_set(r) -> None:
    section("4. Set —— 商品标签，SINTER 求交集")

    keyboard = K("tag:keyboard")
    gaming = K("tag:gaming")
    r.delete(keyboard, gaming)
    r.sadd(keyboard, "1001", "1002")
    r.sadd(gaming, "1002", "1003")
    line("SADD tag:keyboard", r.smembers(keyboard))
    line("SADD tag:gaming", r.smembers(gaming))
    line("SINTER（既是键盘又是电竞）", r.sinter(keyboard, gaming))
    line("SCARD tag:keyboard", r.scard(keyboard))
    line("SISMEMBER 1002 是否属于 keyboard", r.sismember(keyboard, "1002"))
    # 多 Key 运算在集群架构下要求同 Slot（这里靠 Hash Tag 保证）
    r.sinterstore(K("tag:both"), keyboard, gaming)
    line("SINTERSTORE -> tag:both", r.smembers(K("tag:both")))


# --------------------------------------------------------------------------
# 6. ZSet：排行榜
# --------------------------------------------------------------------------
def demo_zset(r) -> None:
    section("5. ZSet —— 销量排行榜（有序集合）")

    key = K("rank:sales")
    r.delete(key)
    for product_id, sales in (("1001", 3), ("1002", 5), ("1003", 1)):
        r.zincrby(key, sales, product_id)
    line("ZINCRBY 累加销量", r.zcard(key))
    line("ZREVRANGE 0 2 WITHSCORES（Top3）", r.zrevrange(key, 0, 2, withscores=True))
    line("ZSCORE 1002", r.zscore(key, "1002"))
    line("ZREVRANK 1002（从 0 开始的排名）", r.zrevrank(key, "1002"))
    line("ZCOUNT 3 ~ +inf", r.zcount(key, 3, "+inf"))


# --------------------------------------------------------------------------
# 7. Bitmap：签到
# --------------------------------------------------------------------------
def demo_bitmap(r) -> None:
    section("6. Bitmap —— 用户签到（一个 bit 一天）")

    key = K("sign:202611")
    r.delete(key)
    for day in (1, 3, 5):
        r.setbit(key, day - 1, 1)  # 第 day 天 -> 偏移 day-1
    line("SETBIT 1/3/5 号签到", "OK")
    line("GETBIT 第 2 天", r.getbit(key, 1))
    line("BITCOUNT（本月签到天数）", r.bitcount(key))
    line("BITPOS 0（第一个没签到的位置）", r.bitpos(key, 0))


# --------------------------------------------------------------------------
# 8. HyperLogLog：UV
# --------------------------------------------------------------------------
def demo_hll(r) -> None:
    section("7. HyperLogLog —— 页面 UV 去重计数（固定 ~12KB）")

    key = K("uv:20261101")
    r.delete(key)
    r.pfadd(key, "u1001", "u1002", "u1003", "u1002")  # 重复的 u1002 会被去重
    line("PFADD 4 个用户（含重复）", r.pfcount(key))
    r.pfadd(key, "u1004")
    line("PFCOUNT 再加 1 个", r.pfcount(key))


# --------------------------------------------------------------------------
# 9. 过期
# --------------------------------------------------------------------------
def demo_expire(r) -> None:
    section("8. 过期与持久化 —— 缓存必备")

    key = K("hot:list")
    r.set(key, "x")
    line("EXPIRE 120", r.expire(key, 120))
    line("TTL", r.ttl(key))
    line("PERSIST（去掉过期时间）", r.persist(key))
    line("TTL after PERSIST（-1 = 永不过期）", r.ttl(key))
    line("EXPIREAT（指定时间戳过期）", r.expireat(key, int(time.time()) + 60))


# --------------------------------------------------------------------------
# 10. 原子操作：分布式锁 + 限流
# --------------------------------------------------------------------------
RELEASE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


def demo_lock_and_limit(r) -> None:
    section("9. 原子操作 —— SET NX EX 分布式锁 + INCR 限流")

    lock_key = K("lock:order:9001")
    token = uuid.uuid4().hex
    r.delete(lock_key)

    # 一条命令同时做到「不存在才写」+「设置过期」，避免死锁
    line("SET lock NX EX 10（抢锁）", r.set(lock_key, token, nx=True, ex=10))
    line("再次抢同一把锁（应失败）", r.set(lock_key, uuid.uuid4().hex, nx=True, ex=10))

    # 释放锁必须校验 token，防止误删别人的锁 —— 用 Lua 保证原子性
    try:
        line("EVAL/Lua 释放自己的锁", r.eval(RELEASE_LOCK_LUA, 1, lock_key, token))
        line(
            "EVAL 释放别人的 token（应为 0）", r.eval(RELEASE_LOCK_LUA, 1, lock_key, "other-token")
        )
    except Exception as exc:  # noqa: BLE001 - 极少数实例/客户端会禁用脚本
        print(f"    [!] 跳过 Lua 部分: {exc}（改用 GET+DEL 两步删除）")
        r.delete(lock_key)

    # 固定窗口限流：每个 IP 每分钟最多 3 次
    limit_key = K("limit:ip:1.2.3.4:20261101205")
    r.delete(limit_key)
    for attempt in range(1, 5):
        current = r.incr(limit_key)
        if current == 1:
            r.expire(limit_key, 60)  # 第一次进来才设置窗口，保证窗口会过期
        allowed = current <= 3
        line(f"第 {attempt} 次请求 -> INCR = {current}", "放行" if allowed else "限流 429")


# --------------------------------------------------------------------------
# 11. 批量：Pipeline / 事务
# --------------------------------------------------------------------------
def demo_pipeline(r) -> None:
    section("10. Pipeline / MULTI —— 批量提交，减少网络往返")

    pipe = r.pipeline(transaction=False)  # 只打包，不保证原子性
    for i in range(1, 6):
        pipe.hset(K("batch"), f"f{i}", i)
    line("pipeline 5 条 HSET 一次发出", f"{len(pipe.execute())} 条命令已执行")
    line("HGETALL batch", r.hgetall(K("batch")))

    # transaction=True 走 MULTI/EXEC，命令不交错（集群架构要求同 Slot）
    tx = r.pipeline(transaction=True)
    tx.set(K("tx:a"), 10)
    tx.incr(K("tx:a"))
    line("MULTI/EXEC 结果", tx.execute())


# --------------------------------------------------------------------------
# 12. SCAN
# --------------------------------------------------------------------------
def demo_scan(r) -> None:
    section("11. SCAN —— 遍历 Key，生产环境请用 SCAN 而不是 KEYS")

    # KEYS 是 O(N) 且会阻塞；SCAN 是游标式增量遍历，不会长时间卡住实例
    found = []
    for key in r.scan_iter(match=f"{PREFIX}:*", count=100):
        found.append(key)
        if len(found) >= 5:  # 演示只取前 5 个
            break
    line("SCAN MATCH {tair:demo}:* 前 5 个", found)


# --------------------------------------------------------------------------
# 13. 清理
# --------------------------------------------------------------------------
def cleanup(r) -> None:
    section("12. 清理演示数据")
    # 先把 Key 收集起来再删：边 SCAN 边 DEL 可能漏掉一些 Key。
    # 真实业务里 Key 很多时要分批处理，别一次性 list() 全部。
    keys = list(r.scan_iter(match=f"{PREFIX}:*", count=500))
    deleted = sum(r.delete(key) for key in keys) if keys else 0
    line("DEL 匹配到的演示 Key", f"{deleted} 个")


def main() -> int:
    parser = argparse.ArgumentParser(description="redis-py 常用命令演示")
    parser.add_argument("--keep", action="store_true", help="保留演示数据不清理")
    args = parser.parse_args()

    settings = config.load_settings()
    if not settings.has_redis:
        print(config.MISSING_REDIS_HINT)
        return 1

    print(f"目标实例: {settings.redis_host}:{settings.redis_port}  db={settings.redis_db}")
    client = build_client(settings)
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001
        print(f"[x] 连接失败: {exc}")
        print(config.CONNECTION_HINT)
        return 1

    try:
        demo_connection(client)
        demo_string(client)
        demo_hash(client)
        demo_list(client)
        demo_set(client)
        demo_zset(client)
        demo_bitmap(client)
        demo_hll(client)
        demo_expire(client)
        demo_lock_and_limit(client)
        demo_pipeline(client)
        demo_scan(client)
        if args.keep:
            section("12. 保留演示数据（--keep）")
            print(f"    用 redis-cli 查看: SCAN 0 MATCH {PREFIX}:* COUNT 100")
        else:
            cleanup(client)
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        print(f"\n[x] 执行中断: {type(exc).__name__}: {message}")
        if "CROSSSLOT" in message or "cross slot" in message.lower():
            print("    这是集群架构的跨 Slot 报错：多 Key 命令要求所有 Key 落在同一个 Slot。")
            print("    解决：给 Key 加同样的 Hash Tag，例如 {tair:demo}:tag:a / {tair:demo}:tag:b")
        elif "NOAUTH" in message or "WRONGPASS" in message.lower():
            print("    认证失败：检查 REDIS_PASSWORD（自定义账号还要填 REDIS_USERNAME）。")
        return 1
    finally:
        client.close()

    print("\n全部命令执行完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
