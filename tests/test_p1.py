"""P1 修复的回归测试：防跨站 / Host 白名单、外链校验、错误信息不外泄、白名单脚本。

    python -m unittest discover -s tests -v

guard / validate_repo_url / allow_my_ip 都是纯函数，任何环境都能跑；
Flask 集成测试需要能 import flask（项目 .venv 里有），没有就自动跳过。
"""

from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

from _stubs import install_stubs  # noqa: E402

install_stubs()

import guard  # noqa: E402

from tair import plans  # noqa: E402


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


allow_my_ip = _load_script("allow_my_ip")

LOCAL = guard.allowed_hostnames({})
HDR = guard.CSRF_VALUE


# ------------------------------------------------------------------ P1-4 Host / CSRF
class GuardTest(unittest.TestCase):
    def test_hostname_parsing(self):
        self.assertEqual(guard.hostname_of("localhost:8080"), "localhost")
        self.assertEqual(guard.hostname_of("[::1]:8080"), "::1")
        self.assertEqual(guard.hostname_of("127.0.0.1"), "127.0.0.1")
        self.assertEqual(guard.hostname_of("EVIL.com:8080"), "evil.com")
        self.assertEqual(guard.hostname_of(""), "")

    def test_allowed_hosts_from_env(self):
        self.assertEqual(LOCAL, guard.LOCAL_HOSTS)
        self.assertNotIn("0.0.0.0", guard.allowed_hostnames({"WEB_HOST": "0.0.0.0"}))
        env = {"WEB_HOST": "10.0.0.5", "WEB_ALLOWED_HOSTS": "dev.lan, Board.local"}
        self.assertTrue({"10.0.0.5", "dev.lan", "board.local"} <= guard.allowed_hostnames(env))

    def test_dns_rebinding_host_rejected_even_for_reads(self):
        self.assertIsNotNone(guard.check_request("GET", "evil.com:8080", None, None, LOCAL))

    def test_local_reads_pass_without_header(self):
        self.assertIsNone(guard.check_request("GET", "127.0.0.1:8080", None, None, LOCAL))

    def test_ssh_forwarded_port_is_fine(self):
        self.assertIsNone(
            guard.check_request("POST", "localhost:9000", "http://localhost:9000", HDR, LOCAL)
        )

    def test_writes_need_csrf_header(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertIsNotNone(guard.check_request(method, "localhost:8080", None, None, LOCAL))
            self.assertIsNotNone(guard.check_request(method, "localhost:8080", None, "x", LOCAL))
            self.assertIsNone(guard.check_request(method, "localhost:8080", None, HDR, LOCAL))

    def test_cross_origin_write_rejected(self):
        for origin in ("http://evil.com", "null", "http://localhost:9999"):
            self.assertIsNotNone(guard.check_request("POST", "localhost:8080", origin, HDR, LOCAL))
        self.assertIsNone(
            guard.check_request("POST", "localhost:8080", "http://localhost:8080", HDR, LOCAL)
        )


# ------------------------------------------------------------------ P1-5 外链校验
class RepoUrlTest(unittest.TestCase):
    def test_accepts_https_and_empty(self):
        self.assertEqual(plans.validate_repo_url(""), "")
        self.assertEqual(
            plans.validate_repo_url("  https://github.com/o/r "), "https://github.com/o/r"
        )

    def test_rejects_scripts_and_non_https(self):
        bad = [
            "javascript:alert(1)",
            " JaVaScRiPt:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "http://github.com/o/r",
            "https://",
            'https://github.com/o/r"onmouseover="x',
            "//github.com/o/r",
            "https://" + "a" * 600,
        ]
        for url in bad:
            with self.assertRaises(ValueError, msg=url):
                plans.validate_repo_url(url)


# ------------------------------------------------------------------ 6b 白名单脚本
class AllowMyIpTest(unittest.TestCase):
    def _fetch(self, mapping):
        return lambda url: mapping.get(url, "")

    def test_two_agreeing_sources_accepted(self):
        eps = ["a", "b", "c"]
        ip = allow_my_ip.detect_ip(self._fetch({"a": "8.8.8.8\n", "b": "8.8.8.8", "c": ""}), eps)
        self.assertEqual(ip, "8.8.8.8")

    def test_disagreement_aborts(self):
        with self.assertRaises(SystemExit):
            allow_my_ip.detect_ip(self._fetch({"a": "8.8.8.8", "b": "1.1.1.1"}), ["a", "b"])

    def test_single_source_is_not_enough(self):
        with self.assertRaises(SystemExit):
            allow_my_ip.detect_ip(self._fetch({"a": "8.8.8.8"}), ["a", "b"])

    def test_private_and_garbage_answers_ignored(self):
        fetch = self._fetch({"a": "192.168.1.2", "b": "<html>", "c": "8.8.8.8", "d": "8.8.8.8"})
        self.assertEqual(allow_my_ip.detect_ip(fetch, ["a", "b", "c", "d"]), "8.8.8.8")

    def test_only_https_endpoints(self):
        self.assertTrue(all(u.startswith("https://") for u in allow_my_ip.IP_ENDPOINTS))

    def test_manual_ip(self):
        self.assertEqual(allow_my_ip.parse_manual_ip("1.2.3.4"), "1.2.3.4")
        self.assertEqual(allow_my_ip.parse_manual_ip("1.2.3.9/24"), "1.2.3.0/24")
        for bad in ("0.0.0.0/0", "10.0.0.0/8", "abc", "1.2.3.4.5"):
            with self.assertRaises(SystemExit, msg=bad):
                allow_my_ip.parse_manual_ip(bad)

    def test_group_rules(self):
        allow_my_ip.check_group("dev_auto", "Cover")
        allow_my_ip.check_group("default", "Append")  # 追加到 default 允许
        for group, mode in (
            ("default", "Cover"),
            ("ali_dms_group", "Cover"),
            ("Dev-Mac", "Cover"),
            ("x", "Cover"),
        ):
            with self.assertRaises(SystemExit, msg=group):
                allow_my_ip.check_group(group, mode)


# ------------------------------------------------------------------ Flask 集成
try:
    import flask  # noqa: F401

    HAVE_FLASK = True
except Exception:  # noqa: BLE001
    HAVE_FLASK = False


@unittest.skipUnless(HAVE_FLASK, "没有 flask，跳过看板集成测试")
class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as webapp

        cls.webapp = webapp
        cls.client = webapp.app.test_client()

    def _put_key(self, body, **headers):
        return self.client.put("/api/key", json=body, headers=headers)

    def test_foreign_host_blocked(self):
        resp = self.client.get("/api/status", headers={"Host": "evil.com:8080"})
        self.assertEqual(resp.status_code, 403)

    def test_write_without_header_blocked(self):
        self.assertEqual(self._put_key({"key": "k", "value": "v"}).status_code, 403)

    def test_write_with_header_reaches_handler(self):
        resp = self._put_key({"key": "", "value": "v"}, **{guard.CSRF_HEADER: HDR})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Key 不能为空", resp.get_json()["error"])

    def test_cross_origin_write_blocked(self):
        resp = self._put_key({"key": "k"}, **{guard.CSRF_HEADER: HDR, "Origin": "http://evil.com"})
        self.assertEqual(resp.status_code, 403)

    def test_upload_form_post_from_other_site_blocked(self):
        # 别的网页能发的就是这种：multipart 表单、不带自定义头
        data = {"file": (io.BytesIO(b"x"), "a.md"), "category": "c", "description": "d"}
        resp = self.client.post("/api/docs/upload", data=data, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 403)

    def test_plan_with_javascript_url_rejected(self):
        data = {
            "file": (io.BytesIO(b"# plan"), "p.md"),
            "github_project_name": "r",
            "github_project_branch": "main",
            "github_project_url": "javascript:alert(document.cookie)",
        }
        resp = self.client.post(
            "/api/plans/upload",
            data=data,
            content_type="multipart/form-data",
            headers={guard.CSRF_HEADER: HDR},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("https://", resp.get_json()["error"])

    def test_internal_error_details_not_leaked(self):
        original = self.webapp.plans_service.scan_plans

        def boom(settings):
            raise RuntimeError("secret-endpoint.oss-internal RequestId=XYZ")

        self.webapp.plans_service.scan_plans = boom
        try:
            with self.assertLogs(self.webapp.app.logger, level="ERROR") as logs:
                resp = self.client.get("/api/plans")
        finally:
            self.webapp.plans_service.scan_plans = original
        self.assertEqual(resp.status_code, 500)
        body = resp.get_json()["error"]
        self.assertNotIn("secret", body)
        self.assertIn("错误号", body)
        self.assertTrue(any("secret-endpoint" in line for line in logs.output))  # 详情进了日志

    def test_redis_server_reply_still_shown(self):
        redis_mod = sys.modules["redis"]

        class Client:
            def scan_iter(self, **kw):
                raise redis_mod.ResponseError("ERR unknown command 'SCAN'")

        original = self.webapp.get_client
        self.webapp.get_client = lambda refresh=False: Client()
        try:
            resp = self.client.get("/api/keys")
        finally:
            self.webapp.get_client = original
        self.assertEqual(resp.status_code, 502)
        self.assertIn("unknown command", resp.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
