"""davine 上传后返回临时下载链接：有效期校验、两种上传方式都要带上链接。

python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

from _fakes import FakeOSS, FakeOTS, settings, use  # noqa: E402
from _stubs import install_stubs  # noqa: E402

install_stubs()

import guard  # noqa: E402

CSRF = {guard.CSRF_HEADER: guard.CSRF_VALUE}

try:
    import flask  # noqa: F401

    HAVE_FLASK = True
except Exception:  # noqa: BLE001
    HAVE_FLASK = False


class ExpiryParseTest(unittest.TestCase):
    """_expire_hours 的边界，不依赖 Flask。"""

    @classmethod
    def setUpClass(cls):
        import app as webapp

        cls.parse = staticmethod(webapp._expire_hours)

    def test_default_when_empty(self):
        import app as webapp

        self.assertEqual(self.parse(None), webapp.EXPIRY_DEFAULT_HOURS)
        self.assertEqual(self.parse(""), webapp.EXPIRY_DEFAULT_HOURS)

    def test_accepts_strings_from_form(self):
        self.assertEqual(self.parse("6"), 6.0)
        self.assertEqual(self.parse(168), 168.0)

    def test_rejects_out_of_range_and_garbage(self):
        for bad in (0, -1, 169, 200, "abc", "NaN"):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.parse(bad)


@unittest.skipUnless(HAVE_FLASK, "没有 flask，跳过看板集成测试")
class DavineLinkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as webapp

        cls.webapp = webapp
        cls.client = webapp.app.test_client()

    def setUp(self):
        self._saved = (self.webapp.SETTINGS, self.webapp.DIRECT_UPLOAD)
        self.webapp.SETTINGS = settings()
        self.webapp.DIRECT_UPLOAD = True
        self.ots, self.oss = FakeOTS(), FakeOSS(size=10)
        use(self.ots, self.oss)

    def tearDown(self):
        self.webapp.SETTINGS, self.webapp.DIRECT_UPLOAD = self._saved

    def _post(self, url, payload):
        return self.client.post(url, json=payload, headers=CSRF)

    # ---- 直传：complete 的响应里带链接
    def test_direct_upload_returns_link(self):
        meta = {
            "filename": "a.md",
            "size": 10,
            "category": "paper",
            "description": "d",
            "expire_hours": 72,
        }
        init = self._post("/api/davine/upload-url", meta).get_json()
        done = self._post("/api/davine/complete", {"upload_id": init["upload_id"]})
        self.assertEqual(done.status_code, 200)
        doc = done.get_json()["document"]
        self.assertIn("temporary_url", doc)
        self.assertIn("expires_at", doc)
        self.assertEqual(doc["expires_hours"], 72.0)
        self.assertIn("sig=1", doc["temporary_url"])

    # ---- 中转：multipart 上传也能拿到链接
    def test_relay_upload_returns_link(self):
        response = self.client.post(
            "/api/davine/upload",
            data={
                "file": (io.BytesIO(b"hello"), "b.md"),
                "category": "others",
                "description": "",
                "expire_hours": "1",
            },
            headers=CSRF,
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        doc = response.get_json()["document"]
        self.assertEqual(doc["expires_hours"], 1.0)
        self.assertTrue(doc["temporary_url"])

    # ---- 文档库没有这个功能，响应里不该多字段
    def test_documents_board_has_no_link(self):
        meta = {"filename": "a.md", "size": 10, "category": "c", "description": "d"}
        init = self._post("/api/docs/upload-url", meta).get_json()
        done = self._post("/api/docs/complete", {"upload_id": init["upload_id"]})
        self.assertNotIn("temporary_url", done.get_json()["document"])

    # ---- 过期时间不合法时，在签发地址之前就拒掉
    def test_invalid_expiry_rejected_before_signing(self):
        base = {"filename": "a.md", "size": 10, "category": "paper", "description": ""}
        for bad in (0, 200, "abc"):
            response = self._post("/api/davine/upload-url", {**base, "expire_hours": bad})
            self.assertEqual(response.status_code, 400, msg=str(bad))
        self.assertEqual(self.oss.uploaded, [])


if __name__ == "__main__":
    unittest.main()
