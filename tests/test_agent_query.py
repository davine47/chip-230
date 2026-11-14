"""无服务 CLI（infra/agent_query.py）的回归测试。"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from _fakes import settings  # noqa: E402
from _stubs import install_stubs  # noqa: E402

install_stubs()

from infra import agent_core, agent_query  # noqa: E402


class AgentQueryCliTest(unittest.TestCase):
    def setUp(self):
        self.settings = settings()
        self.patcher = mock.patch.object(
            agent_query.config, "load_settings", return_value=self.settings
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _run(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = agent_query.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_tables(self):
        code, out, err = self._run(["tables", "--compact"])
        self.assertEqual(code, 0, err)
        names = {item["name"] for item in json.loads(out)["tables"]}
        self.assertEqual(names, {"oss_documents", "davine_oss_documents", "plans"})

    def test_documents_tag(self):
        payload = {
            "table": "oss_documents",
            "count": 1,
            "total": 1,
            "rows": [{"id": "d1", "name": "a.md", "category": "env"}],
        }
        with mock.patch.object(agent_core, "query_table", return_value=payload) as query:
            code, out, err = self._run(
                ["documents", "--tag", "env", "--tag-mode", "all", "--compact"]
            )
        self.assertEqual(code, 0, err)
        body = json.loads(out)
        self.assertEqual(body["documents"][0]["id"], "d1")
        self.assertEqual(query.call_args.kwargs["tags"], ["env"])
        self.assertEqual(query.call_args.kwargs["tag_mode"], "all")

    def test_unknown_table_is_exit_4(self):
        with mock.patch.object(agent_core, "query_table", side_effect=KeyError("未知表")):
            code, out, err = self._run(["rows", "nope", "--compact"])
        self.assertEqual(code, 4)
        self.assertEqual(out, "")
        self.assertFalse(json.loads(err)["ok"])

    def test_health_reports_docstore(self):
        code, out, err = self._run(["health", "--compact"])
        self.assertEqual(code, 0, err)
        body = json.loads(out)
        self.assertTrue(body["docstore"])
        self.assertEqual(body["missing"], [])


if __name__ == "__main__":
    unittest.main()
