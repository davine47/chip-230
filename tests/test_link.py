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

    # ---- 上传时把链接落库，之后用 /tmp-link 查回来
    def test_tmp_link_is_stored_and_queryable(self):
        response = self.client.post(
            "/api/davine/upload",
            data={
                "file": (io.BytesIO(b"hello"), "c.md"),
                "category": "others",
                "description": "",
                "expire_hours": "6",
            },
            headers=CSRF,
            content_type="multipart/form-data",
        )
        doc_id = response.get_json()["document"]["id"]

        got = self.client.get(f"/api/davine/{doc_id}/tmp-link").get_json()
        self.assertTrue(got["url"].startswith("https://"))
        self.assertNotIn("message", got)

        # 表里确实多了一列
        store = self.webapp.docs_service.davine(self.webapp.SETTINGS)
        self.assertTrue(store.get(doc_id)["tmp_public_url"])

    def test_tmp_link_reports_missing(self):
        # 没有 tmp_public_url 的老数据
        self.ots.rows.append(
            type(
                "R", (), {"primary_key": [("id", "old")], "attribute_columns": [("name", "x.md")]}
            )()
        )
        got = self.client.get("/api/davine/old/tmp-link").get_json()
        self.assertEqual(got["url"], "")
        self.assertEqual(got["message"], "无临时链接")

    # ---- 第 1 档修复：列表不带链接、过期时间同源、过期如实告知
    def _relay_upload(self, name="d.md", hours="6"):
        response = self.client.post(
            "/api/davine/upload",
            data={
                "file": (io.BytesIO(b"hello"), name),
                "category": "others",
                "expire_hours": hours,
            },
            headers=CSRF,
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["document"]

    def test_list_does_not_leak_signed_links(self):
        self._relay_upload()
        docs = self.client.get("/api/davine").get_json()["documents"]
        self.assertEqual(len(docs), 1)
        self.assertNotIn("tmp_public_url", docs[0])
        self.assertNotIn("tmp_expire_time", docs[0])

    def test_expiry_matches_signature(self):
        doc = self._relay_upload(hours="6")
        signed_at = self.oss.last_expiration  # 交给 SDK 签名用的过期时刻
        self.assertIsNotNone(signed_at)
        self.assertEqual(doc["expires_at"], signed_at.isoformat(timespec="seconds"))
        got = self.client.get(f"/api/davine/{doc['id']}/tmp-link").get_json()
        self.assertEqual(got["expires_at"], doc["expires_at"])
        self.assertFalse(got["expired"])

    def test_expired_link_is_reported_not_returned(self):
        past_ms = 1_000_000  # 1970 年，早就过期
        self.ots.rows.append(
            type(
                "R",
                (),
                {
                    "primary_key": [("id", "old2")],
                    "attribute_columns": [
                        ("name", "x.md"),
                        ("tmp_public_url", "https://b.oss/x.md?sig=old"),
                        ("tmp_expire_time", past_ms),
                    ],
                },
            )()
        )
        got = self.client.get("/api/davine/old2/tmp-link").get_json()
        self.assertEqual(got["url"], "")
        self.assertTrue(got["expired"])
        self.assertEqual(got["message"], "临时链接已过期")
        self.assertTrue(got["expires_at"].startswith("1970-01-01"))
        # 按需求不清理：表里那条过期链接还在
        self.assertTrue(
            any(dict(r.attribute_columns).get("tmp_expire_time") == past_ms for r in self.ots.rows)
        )

    def test_legacy_link_without_expiry_still_returned(self):
        self.ots.rows.append(
            type(
                "R",
                (),
                {
                    "primary_key": [("id", "old3")],
                    "attribute_columns": [
                        ("name", "y.md"),
                        ("tmp_public_url", "https://b.oss/y.md?sig=1"),
                    ],
                },
            )()
        )
        got = self.client.get("/api/davine/old3/tmp-link").get_json()
        self.assertTrue(got["url"])
        self.assertIsNone(got["expires_at"])

    def test_tmp_link_route_only_on_davine(self):
        self.assertEqual(self.client.get("/api/docs/whatever/tmp-link").status_code, 404)

    def test_tmp_link_unknown_doc_is_404(self):
        self.assertEqual(self.client.get("/api/davine/nope/tmp-link").status_code, 404)

    # ---- 过期时间不合法时，在签发地址之前就拒掉
    def test_invalid_expiry_rejected_before_signing(self):
        base = {"filename": "a.md", "size": 10, "category": "paper", "description": ""}
        for bad in (0, 200, "abc"):
            response = self._post("/api/davine/upload-url", {**base, "expire_hours": bad})
            self.assertEqual(response.status_code, 400, msg=str(bad))
        self.assertEqual(self.oss.uploaded, [])


if __name__ == "__main__":
    unittest.main()
