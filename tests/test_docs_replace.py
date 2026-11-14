"""同名文件更新：先写新索引行，再删旧行和旧 OSS 对象。"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

from _fakes import FakeOSS, FakeOTS, settings, use  # noqa: E402
from _stubs import install_stubs  # noqa: E402

install_stubs()

from tair import docs  # noqa: E402
from tair import tablestore as ts_helper  # noqa: E402
from tair.oss import key_from_url  # noqa: E402

try:
    import flask  # noqa: F401

    HAVE_FLASK = True
except Exception:  # noqa: BLE001
    HAVE_FLASK = False


class ReplaceSameNameTest(unittest.TestCase):
    def setUp(self):
        self.settings = settings()
        self.ots = FakeOTS()
        self.oss = FakeOSS()
        use(self.ots, self.oss)
        self.store = docs.documents(self.settings)

    def _record_old(self, name="a.md", key="agents/docs/a.md", description="old"):
        self.oss.existing.add(key)
        return self.store._record(name, f"https://b.oss/{key}", "env", description, 1)

    def _upload(self, replace):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".md")
        tmp.write(b"new content")
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return self.store.save_from_file(
            filename="a.md",
            filepath=tmp.name,
            category="env",
            description="new",
            replace=replace,
        )

    def test_replace_writes_new_row_then_deletes_old_row_and_object(self):
        old = self._record_old()
        new = self._upload(replace=True)

        self.assertEqual(new["name"], "a.md")
        self.assertEqual(new["replaced_count"], 1)
        self.assertEqual([dict(row.primary_key)["id"] for row in self.ots.rows], [new["id"]])
        self.assertNotEqual(new["url"], old["url"])
        self.assertIn("agents/docs/a.md", self.oss.deleted)

    def test_no_replace_keeps_old_row(self):
        old = self._record_old()
        new = self._upload(replace=False)

        ids = {dict(row.primary_key)["id"] for row in self.ots.rows}
        self.assertEqual(ids, {old["id"], new["id"]})
        self.assertNotIn("replaced_count", new)
        self.assertEqual(self.oss.deleted, [])

    def test_same_key_does_not_delete_new_object(self):
        old = self._record_old()
        self.oss.existing.clear()  # 旧对象缺失：新上传会复用原 key
        new = self._upload(replace=True)

        self.assertEqual(key_from_url(new["url"]), key_from_url(old["url"]))
        self.assertEqual(new["replaced_count"], 1)
        self.assertEqual([dict(row.primary_key)["id"] for row in self.ots.rows], [new["id"]])
        self.assertEqual(self.oss.deleted, [])

    def test_multiple_same_name_rows_are_all_removed(self):
        first = self._record_old(key="agents/docs/a.md")
        second = self._record_old(key="agents/docs/a-old.md")
        new = self._upload(replace=True)

        self.assertEqual(new["replaced_count"], 2)
        ids = {dict(row.primary_key)["id"] for row in self.ots.rows}
        self.assertEqual(ids, {new["id"]})
        self.assertIn("agents/docs/a.md", self.oss.deleted)
        self.assertIn("agents/docs/a-old.md", self.oss.deleted)
        self.assertNotIn(first["id"], ids)
        self.assertNotIn(second["id"], ids)


@unittest.skipUnless(HAVE_FLASK, "没有 flask，跳过上传接口测试")
class ReplaceApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as webapp

        cls.webapp = webapp
        cls.client = webapp.app.test_client()

    def setUp(self):
        self._saved_settings = self.webapp.SETTINGS
        self._saved_direct = self.webapp.DIRECT_UPLOAD
        self.webapp.SETTINGS = settings()
        self.webapp.DIRECT_UPLOAD = True
        self.ots, self.oss = FakeOTS(), FakeOSS(size=10)
        use(self.ots, self.oss)
        self.store = docs.documents(self.webapp.SETTINGS)
        self.old = self.store._record("a.md", "https://b.oss/agents/docs/a.md", "env", "old", 1)
        self.oss.existing.add("agents/docs/a.md")

    def tearDown(self):
        self.webapp.SETTINGS = self._saved_settings
        self.webapp.DIRECT_UPLOAD = self._saved_direct

    def _post(self, url, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        headers["X-Requested-With"] = "chip-230"
        return self.client.post(url, headers=headers, **kwargs)

    def test_direct_upload_replace_true(self):
        meta = {
            "filename": "a.md",
            "size": 10,
            "category": "env",
            "description": "new",
            "replace": True,
        }
        init = self._post("/api/docs/upload-url", json=meta).get_json()
        self.assertEqual(init["mode"], "direct")

        done = self._post("/api/docs/complete", json={"upload_id": init["upload_id"]})
        self.assertEqual(done.status_code, 200)
        doc = done.get_json()["document"]
        self.assertEqual(doc["replaced_count"], 1)
        self.assertEqual([dict(row.primary_key)["id"] for row in self.ots.rows], [doc["id"]])
        self.assertIn("agents/docs/a.md", self.oss.deleted)

    def test_relay_upload_replace_true(self):
        response = self._post(
            "/api/docs/upload",
            data={
                "file": (io.BytesIO(b"new content"), "a.md"),
                "category": "env",
                "description": "new",
                "replace": "1",
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        doc = response.get_json()["document"]
        self.assertEqual(doc["replaced_count"], 1)
        self.assertEqual([dict(row.primary_key)["id"] for row in self.ots.rows], [doc["id"]])
        self.assertIn("agents/docs/a.md", self.oss.deleted)


class FullScanTest(unittest.TestCase):
    """同名查找必须扫完整张表，不受列表扫描上限影响，且只扫当前看板自己的表。"""

    class PagedOTS(FakeOTS):
        """按主键排序分页返回（每页最多 3 行），并记下每次扫的是哪张表。"""

        def __init__(self):
            super().__init__()
            self.tables: list[str] = []

        def get_range(self, table, direction, start, end, columns_to_get=None, limit=None):
            self.tables.append(table)
            _, _, everything, _ = super().get_range(table, direction, start, end, columns_to_get)
            everything.sort(key=lambda r: dict(r.primary_key)["id"])
            begin = dict(start)["id"]
            if begin != docs.INF_MIN:
                everything = [r for r in everything if dict(r.primary_key)["id"] >= begin]
            n = min(3, limit or 3)
            page, rest = everything[:n], everything[n:]
            return None, (rest[0].primary_key if rest else None), page, None

    def setUp(self):
        self.ots, self.oss = self.PagedOTS(), FakeOSS()
        use(self.ots, self.oss)
        self.store = docs.davine(settings())
        for i in range(10):
            self._row(f"{i:02d}", f"other{i}.md")
        self._row("99", "notes.md")  # 主键最大，落在最后一页

    def _row(self, doc_id, name):
        self.ots.rows.append(
            type(
                "R",
                (),
                {
                    "primary_key": [("id", doc_id)],
                    "attribute_columns": [
                        ("name", name),
                        ("url", f"https://b.oss/davine_exchange/{doc_id}"),
                    ],
                },
            )()
        )

    def test_capped_scan_would_miss_it(self):
        # 对照组：按上限扫（这里取 5 行）会被截断，而且看不到排在后面的 notes.md
        rows, truncated = ts_helper.scan_all(
            self.ots, self.store.table, [("id", docs.INF_MIN)], [("id", docs.INF_MAX)], max_rows=5
        )
        self.assertTrue(truncated)
        self.assertNotIn("99", [dict(r.primary_key)["id"] for r in rows])

    def test_same_name_scans_whole_table(self):
        found = self.store._same_name("notes.md")
        self.assertEqual([d["id"] for d in found], ["99"])
        self.assertGreaterEqual(len(self.ots.tables), 4)  # 11 行、每页 3 行：一直翻到最后一页
        self.assertEqual(set(self.ots.tables), {self.store.table})  # 只扫 davine 自己的表


if __name__ == "__main__":
    unittest.main()
