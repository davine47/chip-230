"""P0 修复的回归测试：不连云、不装 SDK 也能跑。

    python -m unittest discover -s tests -v

阿里云 SDK（tablestore / alibabacloud_oss_v2）和 redis 用桩模块代替，只验证我们自己的逻辑：
  1. 列表必须扫完全表再排序，最新的一条不能因为 uuid 排在后面就漏掉；计数不能封顶
  2. 写索引失败要回滚 OSS 对象；删除要先删 OSS 再删索引
  3. Redis 写入：截断值 / 非 String / 新建撞名都要拒绝，TTL 和写入一步完成
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _stubs import install_stubs  # noqa: E402

install_stubs()

from tair import docs, plans, redis_ops  # noqa: E402
from tair import tablestore as ts_helper  # noqa: E402
from tair.config import load_settings  # noqa: E402


# ------------------------------------------------------------------ 假客户端
class FakeRow:
    def __init__(self, pk: list, attrs: dict):
        self.primary_key = pk
        self.attribute_columns = [(k, v, 0) for k, v in attrs.items()]


class FakeOTS:
    """按主键排序、分页返回，行为对齐 get_range。"""

    def __init__(self, rows: list[FakeRow], page: int = 3):
        self.rows = sorted(rows, key=lambda r: [v for _, v in r.primary_key])
        self.page = page
        self.calls: list[str] = []
        self.fail_put = False

    def get_range(self, table, direction, start, end, columns_to_get=None, limit=None):
        start_vals = [v for _, v in start]
        todo = [r for r in self.rows if [v for _, v in r.primary_key] >= start_vals]
        n = min(self.page, limit or self.page)
        got, rest = todo[:n], todo[n:]
        return None, (rest[0].primary_key if rest else None), got, None

    def put_row(self, table, row, condition, return_type=None):
        self.calls.append("ts.put_row")
        if self.fail_put:
            raise RuntimeError("OTS 写入失败")
        return None, types.SimpleNamespace(primary_key=[("uuid", "u"), ("id", 1)])

    def get_row(self, table, pk, columns_to_get=None):
        for r in self.rows:
            if r.primary_key == pk:
                return None, r, None
        return None, None, None

    def delete_row(self, table, pk, condition):
        self.calls.append("ts.delete_row")


class FakeOSS:
    def __init__(self, calls: list[str]):
        self.calls = calls
        self.deleted: list[str] = []
        self.fail_delete = False

    def is_object_exist(self, bucket, key):
        return False

    def uploader(self):
        calls = self.calls

        class U:
            def upload_file(self, req, path):
                calls.append("oss.upload")
                return types.SimpleNamespace(etag="e")

        return U()

    def put_object(self, req):
        self.calls.append("oss.put")
        return types.SimpleNamespace(etag="e", request_id="r")

    def delete_object(self, req):
        self.calls.append("oss.delete")
        if self.fail_delete:
            raise RuntimeError("OSS 删除失败")
        self.deleted.append(req.key)


def _settings():
    s = load_settings()
    return s.__class__(
        **{
            **s.__dict__,
            "oss_bucket": "b",
            "ts_instance": "i",
            "access_key_id": "a",
            "access_key_secret": "s",
        }
    )


def _patch_clients(ots, oss_client):
    ts_helper._SHARED[:] = [ots]
    from tair import oss as oss_helper

    oss_helper._SHARED[:] = [oss_client]


# ------------------------------------------------------------------ 1. 列表 / 计数
class ListingTest(unittest.TestCase):
    def test_newest_doc_is_found_even_if_its_uuid_sorts_last(self):
        # 最新的一条 uuid 是 ff...，按主键排在最后；以前只取第一页就会漏掉它
        rows = [
            FakeRow([("id", f"{i:02x}")], {"name": f"d{i}", "create_time": i}) for i in range(10)
        ]
        rows.append(FakeRow([("id", "ff")], {"name": "newest", "create_time": 999}))
        ots = FakeOTS(rows, page=3)
        _patch_clients(ots, FakeOSS(ots.calls))
        store = docs.documents(_settings())

        top = store.list(limit=2)
        self.assertEqual(top[0]["name"], "newest")
        self.assertEqual(store.count(), 11)

    def test_scan_reports_truncation(self):
        rows = [FakeRow([("id", f"{i:03d}")], {"create_time": i}) for i in range(12)]
        rows_out, truncated = ts_helper.scan_all(
            FakeOTS(rows, page=5), "t", [("id", "")], [], max_rows=10
        )
        self.assertEqual(len(rows_out), 10)
        self.assertTrue(truncated)
        rows_out, truncated = ts_helper.scan_all(
            FakeOTS(rows, page=5), "t", [("id", "")], [], max_rows=12
        )
        self.assertEqual(len(rows_out), 12)
        self.assertFalse(truncated)

    def test_plans_sorted_by_auto_id_across_pages(self):
        rows = [
            FakeRow([("uuid", f"{i:02x}"), ("id", 100 - i)], {"plan_name": f"p{i}"})
            for i in range(8)
        ]
        ots = FakeOTS(rows, page=2)
        _patch_clients(ots, FakeOSS(ots.calls))
        got = plans.list_plans(_settings(), limit=3)
        self.assertEqual([p["id"] for p in got], [100, 99, 98])
        self.assertEqual(plans.count_plans(_settings()), 8)


# ------------------------------------------------------------------ 2. 补偿 / 删除顺序
class ConsistencyTest(unittest.TestCase):
    def test_upload_rolls_back_object_when_index_write_fails(self):
        ots = FakeOTS([])
        ots.fail_put = True
        o = FakeOSS(ots.calls)
        _patch_clients(ots, o)
        store = docs.documents(_settings())
        with self.assertRaises(RuntimeError):
            store.save_from_file("a.md", __file__, "c", "d")
        self.assertEqual(ots.calls, ["oss.upload", "ts.put_row", "oss.delete"])
        self.assertEqual(o.deleted, ["agents/docs/a.md"])

    def test_plan_upload_rolls_back_object_when_index_write_fails(self):
        ots = FakeOTS([])
        ots.fail_put = True
        o = FakeOSS(ots.calls)
        _patch_clients(ots, o)
        with self.assertRaises(RuntimeError):
            plans.save_plan(_settings(), "p.md", b"# x", "p", "t", "ti", "")
        self.assertEqual(ots.calls, ["oss.put", "ts.put_row", "oss.delete"])

    def test_delete_removes_object_before_index(self):
        url = "https://b.oss-cn-hangzhou.aliyuncs.com/agents/docs/a.md"
        ots = FakeOTS([FakeRow([("id", "x")], {"url": url})])
        _patch_clients(ots, FakeOSS(ots.calls))
        self.assertTrue(docs.documents(_settings()).delete("x"))
        self.assertEqual(ots.calls, ["oss.delete", "ts.delete_row"])

    def test_index_kept_when_object_delete_fails(self):
        url = "https://b.oss-cn-hangzhou.aliyuncs.com/agents/docs/a.md"
        ots = FakeOTS([FakeRow([("id", "x")], {"url": url})])
        o = FakeOSS(ots.calls)
        o.fail_delete = True
        _patch_clients(ots, o)
        with self.assertRaises(RuntimeError):
            docs.documents(_settings()).delete("x")
        self.assertNotIn("ts.delete_row", ots.calls)  # 索引还在，可以重试


# ------------------------------------------------------------------ 3. Redis 写入保护
class FakePipe:
    def __init__(self, store: dict, ttls: dict, race: bool = False):
        self.store, self.ttls, self.race, self.ops = store, ttls, race, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def watch(self, name):
        pass

    def type(self, name):
        v = self.store.get(name)
        return "none" if v is None else ("string" if isinstance(v, str) else "hash")

    def strlen(self, name):
        return len(self.store[name].encode())

    def multi(self):
        pass

    def set(self, name, value, ex=None):
        self.ops.append((name, value, ex))

    def execute(self):
        if self.race:
            raise sys.modules["redis"].WatchError()
        for name, value, ex in self.ops:
            self.store[name] = value
            self.ttls[name] = ex


class FakeRedis:
    def __init__(self, store, race=False):
        self.store, self.ttls, self.race = store, {}, race

    def pipeline(self, transaction=True):
        return FakePipe(self.store, self.ttls, self.race)


class RedisWriteTest(unittest.TestCase):
    def test_refuses_to_overwrite_truncated_value(self):
        big = "x" * (redis_ops.MAX_VALUE_CHARS + 1)
        r = FakeRedis({"k": big})
        with self.assertRaises(ValueError):
            redis_ops.write_string_key(r, "k", "x" * 10)
        self.assertEqual(r.store["k"], big)

    def test_refuses_to_replace_non_string(self):
        r = FakeRedis({"h": {"f": "v"}})
        with self.assertRaises(ValueError):
            redis_ops.write_string_key(r, "h", "v")
        self.assertEqual(r.store["h"], {"f": "v"})

    def test_create_does_not_overwrite_existing(self):
        r = FakeRedis({"k": "old"})
        with self.assertRaises(ValueError):
            redis_ops.write_string_key(r, "k", "new", create=True)
        self.assertEqual(r.store["k"], "old")

    def test_set_with_ttl_in_one_step(self):
        r = FakeRedis({"k": "old"})
        redis_ops.write_string_key(r, "k", "new", ttl=60)
        self.assertEqual((r.store["k"], r.ttls["k"]), ("new", 60))
        redis_ops.write_string_key(r, "n", "v", create=True)
        self.assertEqual((r.store["n"], r.ttls["n"]), ("v", None))

    def test_concurrent_change_is_reported(self):
        r = FakeRedis({"k": "old"}, race=True)
        with self.assertRaises(ValueError):
            redis_ops.write_string_key(r, "k", "new")
        self.assertEqual(r.store["k"], "old")

    def test_bad_ttl(self):
        with self.assertRaises(ValueError):
            redis_ops.write_string_key(FakeRedis({}), "k", "v", ttl=-1)


if __name__ == "__main__":
    unittest.main()
