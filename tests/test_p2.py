"""P2 修复的回归测试：浏览器直传（7）、Plan 上限（8）、文件名截断（10）、SCAN 预算（12）、配置检查（13）。

python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

from _stubs import install_stubs  # noqa: E402

install_stubs()

import guard  # noqa: E402

from tair import docs  # noqa: E402
from tair import oss as oss_helper  # noqa: E402
from tair import tablestore as ts_helper  # noqa: E402
from tair.config import load_settings  # noqa: E402

CSRF = {guard.CSRF_HEADER: guard.CSRF_VALUE}


def _settings(**over):
    s = load_settings()
    base = {"oss_bucket": "b", "ts_instance": "i", "access_key_id": "a", "access_key_secret": "s"}
    return s.__class__(**{**s.__dict__, **base, **over})


class FakeOTS:
    def __init__(self):
        self.rows = []
        self.fail_put = False

    def put_row(self, table, row, condition, return_type=None):
        if self.fail_put:
            raise RuntimeError("OTS 写入失败")
        self.rows.append(row)
        return None, None

    def get_range(self, *a, **k):
        return None, None, [], None


class FakeOSS:
    def __init__(self, size=10, missing=False):
        self.size, self.missing = size, missing
        self.deleted: list[str] = []

    def is_object_exist(self, bucket, key):
        return False

    def presign(self, req, expires=None):
        return types.SimpleNamespace(
            url=f"https://{req.bucket}.oss/{req.key}?sig=1",
            method="PUT",
            signed_headers={"Host": "b.oss", "Content-Type": req.content_type},
        )

    def head_object(self, req):
        if self.missing:
            raise RuntimeError("StatusCode: 404, ErrorCode: NoSuchKey")
        return types.SimpleNamespace(content_length=self.size, etag='"abc"')

    def delete_object(self, req):
        self.deleted.append(req.key)


def _use(ots, oss_client):
    ts_helper._SHARED[:] = [ots]
    oss_helper._SHARED[:] = [oss_client]


# ------------------------------------------------------------------ 10 文件名截断
class SafeNameTest(unittest.TestCase):
    def test_long_name_keeps_extension(self):
        name = docs.safe_name("报告" * 100 + ".pdf")
        self.assertTrue(name.endswith(".pdf"))
        self.assertEqual(len(name), docs.MAX_NAME_CHARS)
        self.assertEqual(docs.guess_content_type(name), "application/pdf")

    def test_short_name_untouched_and_path_stripped(self):
        self.assertEqual(docs.safe_name("../a/b:c.md"), "b_c.md")
        self.assertEqual(docs.safe_name(""), "unnamed")

    def test_bogus_long_suffix_not_preserved(self):
        name = docs.safe_name("a" * 50 + "." + "x" * 200)
        self.assertEqual(len(name), docs.MAX_NAME_CHARS)


# ------------------------------------------------------------------ 13 配置检查
class ConfigTest(unittest.TestCase):
    def test_missing_docstore(self):
        self.assertEqual(_settings().missing_docstore, [])
        missing = _settings(oss_bucket="", ts_instance="").missing_docstore
        self.assertEqual(missing, ["OSS_BUCKET", "OTS_INSTANCE"])
        self.assertFalse(_settings(oss_bucket="").has_docstore)
        self.assertTrue(_settings(oss_bucket="").has_oss)  # 只看凭据，04 演示脚本靠它自动挑 bucket


# ------------------------------------------------------------------ 7 直传（存储层）
class DirectUploadStoreTest(unittest.TestCase):
    def setUp(self):
        self.ots, self.oss = FakeOTS(), FakeOSS(size=10)
        _use(self.ots, self.oss)
        self.store = docs.documents(_settings())

    def test_prepare_signs_content_type_and_hides_host(self):
        info = self.store.prepare_direct_upload("data.bin")
        self.assertEqual(info["key"], "agents/docs/data.bin")
        self.assertEqual(info["headers"], {"Content-Type": "application/octet-stream"})
        self.assertEqual(info["method"], "PUT")

    def test_complete_writes_index(self):
        doc = self.store.complete_direct_upload("a.md", "agents/docs/a.md", "c", "d", 100)
        self.assertEqual((doc["size"], doc["etag"]), (10, "abc"))
        self.assertEqual(len(self.ots.rows), 1)
        self.assertEqual(self.oss.deleted, [])

    def test_missing_object(self):
        self.oss.missing = True
        with self.assertRaises(ValueError):
            self.store.complete_direct_upload("a.md", "agents/docs/a.md", "c", "d", 100)

    def test_oversize_and_empty_objects_are_deleted(self):
        for size in (0, 101):
            self.oss.size, self.oss.deleted = size, []
            with self.assertRaises(ValueError):
                self.store.complete_direct_upload("a.md", "agents/docs/a.md", "c", "d", 100)
            self.assertEqual(self.oss.deleted, ["agents/docs/a.md"])
        self.assertEqual(self.ots.rows, [])

    def test_index_failure_rolls_back(self):
        self.ots.fail_put = True
        with self.assertRaises(RuntimeError):
            self.store.complete_direct_upload("a.md", "agents/docs/a.md", "c", "d", 100)
        self.assertEqual(self.oss.deleted, ["agents/docs/a.md"])


# ------------------------------------------------------------------ Flask 集成
try:
    import flask  # noqa: F401

    HAVE_FLASK = True
except Exception:  # noqa: BLE001
    HAVE_FLASK = False


@unittest.skipUnless(HAVE_FLASK, "没有 flask，跳过看板集成测试")
class AppP2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as webapp

        cls.webapp = webapp
        cls.client = webapp.app.test_client()

    def setUp(self):
        self._saved = (self.webapp.SETTINGS, self.webapp.DIRECT_UPLOAD)
        self.webapp.SETTINGS = _settings()
        self.ots, self.oss = FakeOTS(), FakeOSS(size=10)
        _use(self.ots, self.oss)

    def tearDown(self):
        self.webapp.SETTINGS, self.webapp.DIRECT_UPLOAD = self._saved

    def _post(self, url, payload):
        return self.client.post(url, json=payload, headers=CSRF)

    # ---- 7
    def test_direct_upload_round_trip(self):
        meta = {"filename": "a.md", "size": 10, "category": "c", "description": "d"}
        init = self._post("/api/docs/upload-url", meta).get_json()
        self.assertEqual(init["mode"], "direct")
        self.assertTrue(init["url"].startswith("https://"))
        self.assertNotIn("Host", init["headers"])

        done = self._post("/api/docs/complete", {"upload_id": init["upload_id"]})
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.get_json()["document"]["name"], "a.md")
        self.assertEqual(len(self.ots.rows), 1)

        again = self._post("/api/docs/complete", {"upload_id": init["upload_id"]})
        self.assertEqual(again.status_code, 404)  # 凭证一次性

    def test_upload_id_is_bound_to_board(self):
        meta = {"filename": "a.md", "size": 10, "category": "c", "description": "d"}
        init = self._post("/api/docs/upload-url", meta).get_json()
        resp = self._post("/api/davine/complete", {"upload_id": init["upload_id"]})
        self.assertEqual(resp.status_code, 404)

    def test_forged_upload_id_rejected(self):
        self.assertEqual(self._post("/api/docs/complete", {"upload_id": "x"}).status_code, 404)

    def test_direct_init_validates_before_signing(self):
        too_big = {
            "filename": "a.md",
            "size": 26 * 1024 * 1024,
            "category": "c",
            "description": "d",
        }
        self.assertEqual(self._post("/api/docs/upload-url", too_big).status_code, 400)
        no_desc = {"filename": "a.md", "size": 1, "category": "c"}
        self.assertEqual(self._post("/api/docs/upload-url", no_desc).status_code, 400)
        bad_cat = {"filename": "a.md", "size": 1, "category": "nope"}
        self.assertEqual(self._post("/api/davine/upload-url", bad_cat).status_code, 400)

    def test_relay_mode_when_disabled(self):
        self.webapp.DIRECT_UPLOAD = False
        meta = {"filename": "a.md", "size": 1, "category": "c", "description": "d"}
        self.assertEqual(self._post("/api/docs/upload-url", meta).get_json(), {"mode": "relay"})

    # ---- 8
    def test_plan_size_limit(self):
        big = b"#" * (self.webapp.PLAN_MAX_BYTES + 100)
        data = {
            "file": (io.BytesIO(big), "p.md"),
            "github_project_name": "r",
            "github_project_branch": "main",
        }
        resp = self.client.post(
            "/api/plans/upload", data=data, content_type="multipart/form-data", headers=CSRF
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("5MB", resp.get_json()["error"])

    # ---- 13
    def test_missing_bucket_is_explained(self):
        self.webapp.SETTINGS = _settings(oss_bucket="")
        for method, url in (("get", "/api/docs"), ("get", "/api/plans")):
            resp = getattr(self.client, method)(url)
            self.assertEqual(resp.status_code, 400)
            self.assertIn("OSS_BUCKET", resp.get_json()["error"])

    # ---- 12
    def test_scan_stops_after_budget(self):
        class Sparse:
            calls = 0

            def scan(self, cursor=0, match=None, count=None):
                Sparse.calls += 1
                return cursor + 1, []  # 永远扫不完、也匹配不到

            def dbsize(self):
                return 10**7

        original = self.webapp.get_client
        self.webapp.get_client = lambda refresh=False: Sparse()
        try:
            data = self.client.get("/api/keys?pattern=nope*").get_json()
        finally:
            self.webapp.get_client = original
        self.assertEqual(Sparse.calls, self.webapp.SCAN_MAX_ROUNDS)
        self.assertFalse(data["complete"])
        self.assertNotEqual(data["next_cursor"], 0)

    def test_scan_complete_flag(self):
        class Small:
            def scan(self, cursor=0, match=None, count=None):
                return 0, ["k1", "k2"]

            def pipeline(self, transaction=False):
                class P:
                    def __init__(self):
                        self.n = 0

                    def type(self, k):
                        self.n += 1

                    def ttl(self, k):
                        self.n += 1

                    def execute(self):
                        return ["string", -1] * (self.n // 2)

                return P()

            def dbsize(self):
                return 2

        original = self.webapp.get_client
        self.webapp.get_client = lambda refresh=False: Small()
        try:
            data = self.client.get("/api/keys").get_json()
        finally:
            self.webapp.get_client = original
        self.assertTrue(data["complete"])
        self.assertEqual([r["key"] for r in data["keys"]], ["k1", "k2"])


if __name__ == "__main__":
    unittest.main()
