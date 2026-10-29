"""呼出元の自動検出とfolderページ照合の回帰確認。実際の送信は行わない。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import prepare_launch
import orca_bridge


class AutoConnectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name
        self.caller = {"orcaSessionId": "orca_session_id:codex_here", "live": True}
        self.tabs = [{"browserPageId": "page-here-123", "worktreeId": "folder:here",
                      "url": "http://127.0.0.1:4793/__screen_studio__/shell?path=%2Fappearance.html"}]
        self.calls = []

    def runner(self, cli, args, timeout):
        self.calls.append(args)
        if args[0] == "status":
            result = {"caller": self.caller}
        elif args[:2] == ["worktree", "ps"]:
            result = {"worktrees": [{"path": self.path, "worktreeId": "folder:here",
                       "agents": [{"paneKey": "structured-agent-session-codex_here:pane"}]}]}
        else:
            result = {"tabs": self.tabs}
        return 0, json.dumps({"ok": True, "result": result})

    def test_detects_caller_and_reuses_same_folder_tab(self):
        result = prepare_launch.prepare("MonoA", entry="/appearance.html", port=4793, cli="orca.exe", runner=self.runner)
        self.assertEqual(result["target"], self.caller["orcaSessionId"])
        self.assertEqual(result["page"], "page-here-123")
        self.assertIn("--ai-target", result["argv"])
        self.assertFalse(any("send" in args for args in self.calls))

    def test_other_project_tab_is_never_reused(self):
        self.tabs[0]["worktreeId"] = "folder:elsewhere"
        result = prepare_launch.prepare("MonoA", port=4793, cli="orca.exe", runner=self.runner)
        self.assertIsNone(result["page"])
        self.assertIn("--orca-open", result["argv"])

    def test_missing_caller_is_not_guessed(self):
        self.caller = {}
        with self.assertRaises(orca_bridge.BridgeError):
            prepare_launch.prepare("MonoA", cli="orca.exe", runner=self.runner)

    def test_ambiguous_tabs_preserve_user_choice(self):
        self.tabs.append(dict(self.tabs[0], browserPageId="page-two-123"))
        with self.assertRaises(orca_bridge.BridgeError):
            prepare_launch.prepare("MonoA", port=4793, cli="orca.exe", runner=self.runner)

    def test_folder_selector_fallback_keeps_exact_page(self):
        def runner(cli, args, timeout):
            self.calls.append(args)
            if "--worktree" in args:
                return 1, json.dumps({"ok": False, "error": {"code": "selector_not_found"}})
            return 0, json.dumps({"ok": True, "result": {"tab": {"worktreeId": "folder:here"}}})
        bridge = orca_bridge.OrcaBridge(cli="orca.exe", runner=runner)
        result = bridge._page_json(["tab", "show", "--page", "page-here-123", "--worktree", "path:" + self.path, "--json"])
        self.assertEqual(result["tab"]["worktreeId"], "folder:here")
        self.assertEqual(self.calls[-1], ["tab", "show", "--page", "page-here-123", "--json"])


if __name__ == "__main__":
    unittest.main()
