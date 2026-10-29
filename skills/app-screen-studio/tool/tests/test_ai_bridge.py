"""AI送信・スクショ連携のテスト。Orca CLI は偽のrunnerに差し替える(実際のOrcaは呼ばない)。"""
import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import orca_bridge  # noqa: E402
import studio  # noqa: E402
from test_studio import PNG, Server, make_site, valid_payload  # noqa: E402

SELF = "claude_aaaa1111_bbbb_2222"
TARGET = "orca_session_id:" + SELF
PAGE = "11111111-2222-3333-4444-555555555555"


def make_png(size=(200, 100)):
    from PIL import Image
    img = Image.new("RGB", size, (255, 255, 255))
    for x in range(size[0] // 2):                 # 左半分は赤、右半分は青
        for y in range(size[1]):
            img.putpixel((x, y), (255, 0, 0))
    for x in range(size[0] // 2, size[0]):
        for y in range(size[1]):
            img.putpixel((x, y), (0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeOrca:
    """Orca CLI の代わり。呼ばれた引数を記録し、コマンドごとの返答を返す。"""

    def __init__(self, wt, shot=None, port=0):
        self.wt, self.port, self.calls = wt, port, []
        self.shot = shot
        self.tab_worktree_id = "folder:uuid-1"
        self.tab_url = None
        self.agents_in = wt            # 送信先sessionがいるworkspace
        self.send_to = None            # None=宛先どおり / 文字列=ずらして返す
        self.send_error = None         # "fail" | "timeout" | "garbage"
        self.pane = "structured-agent-session-%s:abc" % SELF

    def __call__(self, cli, args, timeout):
        self.calls.append(list(args))
        head = tuple(args[:2]) if args[0] in ("orchestration", "worktree", "tab") else (args[0],)
        ok = lambda r: (0, json.dumps({"ok": True, "result": r}))
        if head == ("worktree", "ps"):
            ws = [{"worktreeId": "folder:uuid-1", "path": self.wt, "agents": []},
                  {"worktreeId": "folder:uuid-9", "path": "C:/other/ws", "agents": []}]
            for w in ws:
                if w["path"] == self.agents_in:
                    w["agents"] = [{"paneKey": self.pane, "prompt": "SECRET-PROMPT"},
                                   {"paneKey": "structured-agent-session-%sX:zzz" % SELF}]
            return ok({"worktrees": ws})
        if head == ("tab", "show"):
            return ok({"tab": {"worktreeId": self.tab_worktree_id,
                               "url": self.tab_url or "http://127.0.0.1:%d/__screen_studio__/shell?path=%%2F" % self.port}})
        if head == ("tab", "create"):
            return ok({"tab": {"browserPageId": PAGE}})
        if head == ("tab", "close"):
            return ok({"closed": True})
        if head == ("screenshot",):
            return ok({"data": base64.b64encode(self.shot or PNG).decode(), "format": "png"})
        if head == ("orchestration", "send"):
            if self.send_error == "timeout":
                raise subprocess.TimeoutExpired("orca", timeout)
            if self.send_error == "garbage":
                return 0, "not json"
            if self.send_error == "fail":
                return 1, json.dumps({"ok": False, "error": {"code": "boom", "message": "SECRET-PROMPT"}})
            to = args[args.index("--to") + 1]
            return ok({"message": {"id": "msg_test1", "to_handle": self.send_to or to, "delivered_at": None}})
        return 1, ""

    def sent(self):
        return [c for c in self.calls if c[:2] == ["orchestration", "send"]]

    def shots(self):
        return [c for c in self.calls if c[0] == "screenshot"]


def bridge_for(wt, fake, **kw):
    cfg = dict(worktree=wt, page=PAGE, target=TARGET, cli="orca.exe", runner=fake)
    cfg.update(kw)
    return orca_bridge.OrcaBridge(**cfg)


class BridgeUnitTests(unittest.TestCase):
    def setUp(self):
        self.wt = tempfile.mkdtemp(prefix="ss-wt-").replace("\\", "/")

    def tearDown(self):
        shutil.rmtree(self.wt, ignore_errors=True)

    def test_config_is_validated(self):
        for kw in ({"page": "../x"}, {"target": "claude_x"}, {"target": "orca_session_id:a b"},
                   {"sender": "x;y"}, {"worktree": self.wt + "/nonexistent"}):
            with self.assertRaises(orca_bridge.BridgeError, msg=str(kw)):
                bridge_for(self.wt, FakeOrca(self.wt), **kw)
        with self.assertRaises(orca_bridge.BridgeError):
            bridge_for(self.wt, FakeOrca(self.wt), open_tab=True)      # page と open は排他

    def test_missing_lists_what_to_set(self):
        b = orca_bridge.OrcaBridge(cli="")
        text = " ".join(b.missing())
        for need in ("orca.exe", "--orca-worktree", "--orca-page", "--ai-target"):
            self.assertIn(need, text)
        self.assertFalse(b.configured())
        ok = bridge_for(self.wt, FakeOrca(self.wt))
        self.assertEqual(ok.missing(), [])
        self.assertEqual(orca_bridge.OrcaBridge(worktree=self.wt, open_tab=True, target=TARGET, cli="x").missing(), [])

    def test_cli_must_be_exe_not_cmd(self):
        d = tempfile.mkdtemp()
        try:
            cmd = os.path.join(d, "orca.cmd")
            open(cmd, "w").close()
            exe = os.path.join(d, "orca.exe")
            open(exe, "w").close()
            self.assertIsNone(orca_bridge.find_cli({orca_bridge.CLI_ENV: cmd}) if not shutil.which("orca.exe") else None)
            self.assertEqual(orca_bridge.find_cli({orca_bridge.CLI_ENV: exe}), exe)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_default_runner_uses_arg_list_without_shell(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=b"{}")
            orca_bridge.default_runner("C:/orca.exe", ["orchestration", "send", "--body", "a & b; $(x)"], 5)
        args, kw = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertIs(kw["shell"], False)
        self.assertEqual(args[0][-1], "a & b; $(x)")      # 文字列はそのまま1引数(解釈されない)

    def test_verify_ok_with_folder_workspace_id(self):
        f = FakeOrca(self.wt, port=4780)
        b = bridge_for(self.wt, f)
        self.assertEqual(b.verify(4780), {"ok": True, "reasons": [], "missing": []})

    def test_verify_rejects_wrong_workspace_tab_and_url(self):
        f = FakeOrca(self.wt, port=4780)
        f.tab_worktree_id = "folder:uuid-9"                 # 別workspaceのタブ
        self.assertIn("編集タブが指定のworkspaceにありません", bridge_for(self.wt, f).verify(4780)["reasons"])
        f = FakeOrca(self.wt, port=4780)
        f.tab_url = "https://evil.example/__screen_studio__/shell"
        self.assertIn("編集タブがこの編集室を開いていません", bridge_for(self.wt, f).verify(4780)["reasons"])
        f = FakeOrca(self.wt, port=4781)                    # 別ポートの編集室
        self.assertFalse(bridge_for(self.wt, f).verify(4780)["ok"])

    def test_verify_rejects_shell_prefix_and_credentials(self):
        for url in ("http://127.0.0.1:4780/__screen_studio__/shell-other", "http://user@127.0.0.1:4780/__screen_studio__/shell"):
            f = FakeOrca(self.wt, port=4780)
            f.tab_url = url
            self.assertFalse(bridge_for(self.wt, f).verify(4780)["ok"])

    def test_verify_target_session_exact_prefix_and_workspace(self):
        f = FakeOrca(self.wt, port=4780)
        f.pane = "structured-agent-session-%sX:zzz" % SELF      # idが前方一致するだけの別session
        r = bridge_for(self.wt, f).verify(4780)
        self.assertFalse(r["ok"])
        self.assertIn("送信先sessionが見つかりません(終了済みの可能性)", r["reasons"])
        f = FakeOrca(self.wt, port=4780)
        f.agents_in = "C:/other/ws"                              # 別workspaceにいる
        self.assertFalse(bridge_for(self.wt, f).verify(4780)["ok"])
        # 送信先を別workspaceに明示した場合は、そこにいれば通る
        b = bridge_for(self.wt, f, target_worktree="C:/other/ws")
        self.assertTrue(b.verify(4780)["ok"])

    def test_send_requires_matching_to_handle(self):
        f = FakeOrca(self.wt)
        f.send_to = "run:other"
        with self.assertRaises(orca_bridge.BridgeError) as cm:
            bridge_for(self.wt, f).send("s", "b", {})
        self.assertEqual(cm.exception.code, "misrouted")
        self.assertTrue(cm.exception.ambiguous)

    def test_send_errors_are_classified(self):
        for mode, ambiguous in (("timeout", True), ("garbage", True), ("fail", False)):
            f = FakeOrca(self.wt)
            f.send_error = mode
            with self.assertRaises(orca_bridge.BridgeError, msg=mode) as cm:
                bridge_for(self.wt, f).send("s", "b", {})
            self.assertEqual(cm.exception.ambiguous, ambiguous, mode)
            self.assertNotIn("SECRET-PROMPT", cm.exception.message)

    def test_open_and_close_own_tab_only(self):
        f = FakeOrca(self.wt)
        b = orca_bridge.OrcaBridge(worktree=self.wt, target=TARGET, open_tab=True, cli="orca.exe", runner=f)
        self.assertEqual(b.open_tab("http://127.0.0.1:1/__screen_studio__/shell"), PAGE)
        create = [c for c in f.calls if c[:2] == ["tab", "create"]][0]
        self.assertIn("path:" + self.wt, create)             # 指定workspaceへ作る
        self.assertTrue(b.close_tab())
        self.assertEqual([c for c in f.calls if c[:2] == ["tab", "close"]][0][3], PAGE)
        # 利用者が渡したタブは閉じない
        f2 = FakeOrca(self.wt)
        b2 = bridge_for(self.wt, f2)
        self.assertFalse(b2.close_tab())
        self.assertFalse([c for c in f2.calls if c[:2] == ["tab", "close"]])


class AiServerBase(unittest.TestCase):
    def setUp(self):
        self.site_base = tempfile.mkdtemp(prefix="ss-site-ai-")
        self.wt = tempfile.mkdtemp(prefix="ss-wt-").replace("\\", "/")
        self.s = Server(root=make_site(self.site_base))
        self.fake = FakeOrca(self.wt, shot=make_png(), port=self.s.port)
        self.s.state.bridge = bridge_for(self.wt, self.fake)

    def tearDown(self):
        self.s.close()
        shutil.rmtree(self.site_base, ignore_errors=True)
        shutil.rmtree(self.wt, ignore_errors=True)

    def post(self, path, body, token="default", origin="default", ctype="application/json"):
        h = {"Content-Type": ctype}
        o = self.s.origin() if origin == "default" else origin
        if o:
            h["Origin"] = o
        t = self.s.token() if token == "default" else token
        if t:
            h["X-Studio-Token"] = t
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        st, _, raw = self.s.req("POST", studio.PREFIX + path, data, h)
        return st, json.loads(raw)

    def img_dir(self):
        return os.path.join(self.s.out, "tproj", "画像", "画面編集")

    def send_req(self, **over):
        req = {"request_id": "rid-%s" % os.urandom(4).hex(), "prompt": "このボタンを大きくして",
               "attachments": [], "payload": valid_payload()}
        req.update(over)
        return req


class ScreenshotTests(AiServerBase):
    VP = {"w": 100, "h": 50}      # 画像は200x100 = 2倍(devicePixelRatio 2 相当)

    def test_crop_converts_css_px_to_image_px(self):
        from PIL import Image
        st, r = self.post("/api/ai/screenshot", {"rect": {"x": 10, "y": 5, "w": 80, "h": 40}, "viewport": self.VP})
        self.assertEqual(st, 200, r)
        self.assertEqual((r["width"], r["height"]), (160, 80))
        self.assertFalse(r["clipped"])
        self.assertTrue(r["name"].startswith("shot-") and r["name"].endswith(".png"))
        path = os.path.join(self.img_dir(), r["name"])
        img = Image.open(path)
        self.assertEqual(img.size, (160, 80))
        self.assertEqual(img.getpixel((2, 2)), (255, 0, 0))          # 左端は赤
        self.assertEqual(img.getpixel((157, 2)), (0, 0, 255))        # 右端は青
        self.assertEqual(len(self.fake.shots()), 1)
        shot = self.fake.shots()[0]
        self.assertEqual(shot[shot.index("--page") + 1], PAGE)       # 固定したタブだけ
        self.assertEqual(shot[shot.index("--worktree") + 1], "path:" + self.wt)

    def test_clipped_when_stage_hides_part_of_frame(self):
        st, r = self.post("/api/ai/screenshot", {"rect": {"x": 0, "y": 0, "w": 100, "h": 30},
            "viewport": self.VP, "full": {"w": 100, "h": 80}})
        self.assertEqual(st, 200)
        self.assertTrue(r["clipped"])
        self.assertEqual(r["height"], 60)

    def test_clipped_when_frame_goes_off_screen(self):
        st, r = self.post("/api/ai/screenshot", {"rect": {"x": 50, "y": 0, "w": 100, "h": 50}, "viewport": self.VP})
        self.assertEqual(st, 200)
        self.assertTrue(r["clipped"])
        self.assertEqual(r["width"], 100)

    def test_outside_and_invalid_rect_rejected(self):
        for rect in ({"x": 500, "y": 500, "w": 50, "h": 50}, {"x": 0, "y": 0, "w": 0, "h": 5},
                     {"x": "a", "y": 0, "w": 50, "h": 50}, {"x": float("nan"), "y": 0, "w": 50, "h": 50}, None):
            st, r = self.post("/api/ai/screenshot", {"rect": rect, "viewport": self.VP})
            self.assertIn(st, (400, 409), str(rect))
        self.assertFalse(os.path.isdir(self.img_dir()) and os.listdir(self.img_dir()))

    def test_request_cannot_choose_page_or_workspace(self):
        st, r = self.post("/api/ai/screenshot", {"rect": {"x": 0, "y": 0, "w": 50, "h": 50}, "viewport": self.VP,
                                                 "page": "evil-page-id-0000", "worktree": "path:C:/x"})
        self.assertEqual(st, 200)
        self.assertEqual(self.fake.shots()[0][self.fake.shots()[0].index("--page") + 1], PAGE)

    def test_unbound_tab_is_not_captured(self):
        self.fake.tab_worktree_id = "folder:uuid-9"
        st, r = self.post("/api/ai/screenshot", {"rect": {"x": 0, "y": 0, "w": 50, "h": 50}, "viewport": self.VP})
        self.assertEqual(st, 409)
        self.assertEqual(self.fake.shots(), [])

    def test_disabled_without_config(self):
        self.s.state.bridge = None
        st, r = self.post("/api/ai/screenshot", {"rect": {"x": 0, "y": 0, "w": 50, "h": 50}, "viewport": self.VP})
        self.assertEqual(st, 409)
        self.assertEqual(r["code"], "ai_disabled")
        self.assertTrue(r["missing"])

    def test_auth_required(self):
        body = {"rect": {"x": 0, "y": 0, "w": 50, "h": 50}, "viewport": self.VP}
        self.assertEqual(self.post("/api/ai/screenshot", body, token="bad")[0], 403)
        self.assertEqual(self.post("/api/ai/screenshot", body, origin="http://evil.example")[0], 403)
        self.assertEqual(self.post("/api/ai/screenshot", body, origin="")[0], 403)
        self.assertEqual(self.fake.shots(), [])

    def test_busy_lock(self):
        self.s.state.shot_lock.acquire()
        try:
            st, r = self.post("/api/ai/screenshot", {"rect": {"x": 0, "y": 0, "w": 50, "h": 50}, "viewport": self.VP})
            self.assertEqual(st, 429)
        finally:
            self.s.state.shot_lock.release()


class AttachTests(AiServerBase):
    def attach(self, kind, data, name="a.png", **kw):
        return self.post("/api/ai/attach?kind=%s&name=%s" % (kind, name), data, ctype="application/octet-stream", **kw)

    def test_paste_and_file_are_separate_kinds(self):
        st, a = self.attach("paste", PNG, "clip.png")
        st2, b = self.attach("file", PNG + b"1", "pic.png")
        self.assertEqual((st, st2), (200, 200))
        self.assertTrue(a["name"].startswith("paste-") and b["name"].startswith("file-"))
        self.assertEqual((a["label"], b["label"]), ("貼り付け画像", "添付ファイル"))
        self.assertTrue(os.path.isfile(os.path.join(self.img_dir(), a["name"])))

    def test_rejections(self):
        self.assertEqual(self.attach("shot", PNG)[0], 400)             # スクショはサーバー撮影のみ
        self.assertEqual(self.attach("paste", b"<svg onload=1>")[0], 415)
        self.assertEqual(self.attach("paste", PNG, token="bad")[0], 403)
        self.assertEqual(self.attach("paste", PNG, origin="http://evil.example")[0], 403)
        self.assertEqual(self.attach("paste", PNG, "../../x.png")[0], 200)
        names = os.listdir(self.img_dir())
        self.assertTrue(all(".." not in n and "/" not in n for n in names))


class SendTests(AiServerBase):
    def shot(self):
        return self.post("/api/ai/screenshot", {"rect": {"x": 0, "y": 0, "w": 50, "h": 25}, "viewport": ScreenshotTests.VP})[1]

    def test_send_registers_one_message_with_files_not_prompt(self):
        shot = self.shot()
        paste = self.post("/api/ai/attach?kind=paste&name=c.png", PNG, ctype="application/octet-stream")[1]
        req = self.send_req(prompt="このボタンを大きくして\n(改行あり) & $(whoami) \"引用\"",
                            attachments=[shot["name"], paste["name"]])
        st, r = self.post("/api/ai/send", req)
        self.assertEqual(st, 200, r)
        self.assertTrue(r["ok"])
        self.assertEqual((r["state"], r["message_id"]), ("registered", "msg_test1"))
        self.assertIn("未確認", r["note"])
        self.assertNotIn("delivered", r)
        sent = self.fake.sent()
        self.assertEqual(len(sent), 1)
        args = sent[0]
        self.assertEqual(args[args.index("--to") + 1], TARGET)         # 固定した宛先
        self.assertNotIn("--run", args)
        body = args[args.index("--body") + 1]
        self.assertNotIn("このボタン", body)                            # 本文(引数)にユーザー入力は載せない
        self.assertNotIn("このボタン", " ".join(args))
        payload = json.loads(args[args.index("--payload") + 1])
        self.assertEqual(payload["kind"], "app-screen-studio-request")
        self.assertEqual(payload["screen"], "/mobile/web/index.html")
        self.assertEqual({i["kind"] for i in payload["images"]}, {"shot", "paste"})   # 別添付
        # 依頼文ファイル: 入力そのまま・画像パス(種類つき)・編集JSONパス
        text = open(r["request_file"], encoding="utf-8").read()
        self.assertIn("(改行あり) & $(whoami) \"引用\"", text)
        self.assertIn("[スクリーンショット] " + os.path.join(self.img_dir(), shot["name"]), text)
        self.assertIn("[貼り付け画像] " + os.path.join(self.img_dir(), paste["name"]), text)
        self.assertIn("製品コードに未反映", text)
        # 送信前に編集JSONが保存されている
        saved = json.load(open(r["saved_path"], encoding="utf-8"))
        self.assertEqual(saved["edits"][0]["target"]["edit_id"], "demo-title")
        self.assertEqual(os.path.dirname(r["request_file"]),
                         os.path.join(self.s.out, "tproj", "関連資料", "画面編集", "依頼"))

    def test_same_request_id_is_never_sent_twice(self):
        req = self.send_req()
        self.assertEqual(self.post("/api/ai/send", req)[0], 200)
        st, r = self.post("/api/ai/send", req)
        self.assertEqual(st, 200)
        self.assertTrue(r["duplicate"])
        self.assertEqual(len(self.fake.sent()), 1)

    def test_double_click_while_busy_is_refused(self):
        self.s.state.ai_lock.acquire()
        try:
            st, r = self.post("/api/ai/send", self.send_req())
            self.assertEqual((st, r["code"]), (429, "busy"))
        finally:
            self.s.state.ai_lock.release()
        self.assertEqual(self.fake.sent(), [])

    def test_failure_keeps_edits_saved_and_blocks_auto_resend(self):
        self.fake.send_error = "fail"
        req = self.send_req()
        st, r = self.post("/api/ai/send", req)
        self.assertEqual((st, r["ok"], r["ambiguous"]), (502, False, False))
        self.assertTrue(os.path.isfile(r["saved_path"]))               # JSONは保存済み(次の手動送信に使える)
        self.assertNotIn("SECRET-PROMPT", json.dumps(r))
        self.fake.send_error = None
        st2, r2 = self.post("/api/ai/send", req)                       # 同じidでは自動再送しない
        self.assertTrue(r2.get("duplicate"))
        self.assertEqual(len(self.fake.sent()), 1)
        st3, r3 = self.post("/api/ai/send", self.send_req())            # 利用者が押し直した新しい送信は通る
        self.assertEqual((st3, r3["ok"]), (200, True))

    def test_timeout_is_ambiguous_not_success(self):
        self.fake.send_error = "timeout"
        st, r = self.post("/api/ai/send", self.send_req())
        self.assertEqual(st, 502)
        self.assertTrue(r["ambiguous"])
        self.assertNotIn("state", r)

    def test_misrouted_registration_is_not_success(self):
        self.fake.send_to = "run:someone-else"
        st, r = self.post("/api/ai/send", self.send_req())
        self.assertEqual((st, r["ok"], r["code"]), (502, False, "misrouted"))

    def test_unbound_blocks_before_saving_or_sending(self):
        for tweak in (lambda: setattr(self.fake, "tab_worktree_id", "folder:uuid-9"),
                      lambda: setattr(self.fake, "agents_in", "C:/other/ws"),
                      lambda: setattr(self.fake, "pane", "structured-agent-session-other:1")):
            self.fake.tab_worktree_id, self.fake.agents_in = "folder:uuid-1", self.wt
            self.fake.pane = "structured-agent-session-%s:abc" % SELF
            tweak()
            st, r = self.post("/api/ai/send", self.send_req())
            self.assertEqual((st, r["code"]), (409, "ai_unbound"))
        self.assertEqual(self.fake.sent(), [])
        self.assertFalse(os.path.isdir(os.path.join(self.s.out, "tproj", "関連資料")))

    def test_disabled_when_not_configured(self):
        self.s.state.bridge = orca_bridge.OrcaBridge(cli="")
        st, r = self.post("/api/ai/send", self.send_req())
        self.assertEqual((st, r["code"]), (409, "ai_disabled"))
        self.assertTrue(r["missing"])

    def test_input_validation(self):
        bad = [self.send_req(prompt=""), self.send_req(prompt="   "), self.send_req(prompt="あ" * (studio.MAX_PROMPT + 1)),
               self.send_req(request_id="x"), self.send_req(request_id="a/b/c/d/e/f"),
               self.send_req(attachments=["../../x.png"]), self.send_req(attachments=["unknown-aaaa.png"]),
               self.send_req(attachments=["paste-0000000000-none.png"]), self.send_req(attachments="x"),
               self.send_req(attachments=["shot-a.png"] * 2),
               self.send_req(payload=valid_payload(project="other")),
               self.send_req(payload={"schema": "x"})]
        for req in bad:
            st, r = self.post("/api/ai/send", req)
            self.assertEqual(st, 400, json.dumps(req)[:120])
        self.assertEqual(self.fake.sent(), [])

    def test_auth_required(self):
        self.assertEqual(self.post("/api/ai/send", self.send_req(), token="bad")[0], 403)
        self.assertEqual(self.post("/api/ai/send", self.send_req(), origin="http://evil.example")[0], 403)
        self.assertEqual(self.post("/api/ai/send", self.send_req(), origin="")[0], 403)
        self.assertEqual(self.post("/api/ai/send", self.send_req(), ctype="text/plain")[0], 415)
        self.assertEqual(self.fake.sent(), [])

    def test_no_edits_still_saves_current_json_and_quarantines_old(self):
        self.assertEqual(self.post("/api/ai/send", self.send_req())[0], 200)
        jpath = os.path.join(self.s.out, "tproj", "関連資料", "画面編集", "mobile__web__index.json")
        empty = valid_payload()
        empty["edits"] = []
        st, r = self.post("/api/ai/send", self.send_req(payload=empty, prompt="全部戻したので確認して"))
        self.assertEqual(st, 200)
        with open(jpath, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["edits"], [])                      # 現在の状態(空)が正本
        self.assertEqual(os.path.normcase(r["saved_path"]), os.path.normcase(jpath))
        manifest = os.path.join(self.s.out, "一時ゴミ箱", "manifest.json")
        with open(manifest, encoding="utf-8") as f:
            self.assertIn("mobile__web__index.json", f.read())               # 旧版は隔離されている

    def test_logs_do_not_contain_prompt_images_or_tokens(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.post("/api/ai/send", self.send_req(prompt="ひみつのプロンプト-XYZ"))
            self.post("/api/ai/send", self.send_req(prompt="ひみつ2"), token="leaked-token-value")
            self.post("/api/ai/attach?kind=paste&name=secret-name.png", PNG, ctype="application/octet-stream")
        log = buf.getvalue()
        for secret in ("ひみつ", self.s.token(), "leaked-token-value", "secret-name", "iVBOR"):
            self.assertNotIn(secret, log)

    def test_session_endpoint_shows_summary_only(self):
        st, _, raw = self.s.req("GET", "/__screen_studio__/api/session")
        ai = json.loads(raw)["ai"]
        self.assertTrue(ai["configured"])
        self.assertEqual(ai["target"], "…" + TARGET[-8:])
        self.assertNotIn(SELF, raw.decode("utf-8"))
        self.assertNotIn(PAGE, raw.decode("utf-8"))
        self.s.state.bridge = None
        ai = json.loads(self.s.req("GET", "/__screen_studio__/api/session")[2])["ai"]
        self.assertFalse(ai["configured"])
        self.assertIn("--ai-target", " ".join(ai["missing"]))

    def test_status_endpoint_runs_verification(self):
        st, _, raw = self.s.req("GET", "/__screen_studio__/api/ai/status", headers={"X-Studio-Token": self.s.token()})
        self.assertTrue(json.loads(raw)["ai"]["verified"])
        self.fake.agents_in = "C:/other/ws"
        raw = self.s.req("GET", "/__screen_studio__/api/ai/status", headers={"X-Studio-Token": self.s.token()})[2]
        self.assertFalse(json.loads(raw)["ai"]["verified"])
        self.assertEqual(self.s.req("GET", "/__screen_studio__/api/ai/status")[0], 403)


class ArgsTests(unittest.TestCase):
    def ns(self, **kw):
        base = dict(project="p", dir=studio.DEMO_DIR, url=None, entry=None, output_root="x",
                    orca_worktree=None, orca_page=None, orca_open=False, ai_target=None,
                    ai_target_worktree=None, ai_from=None)
        base.update(kw)
        return type("A", (), base)()

    def test_no_orca_args_means_disabled(self):
        self.assertIsNone(studio.build_state(self.ns()).bridge)

    def test_self_target_uses_env_only(self):
        wt = tempfile.mkdtemp()
        try:
            with mock.patch.dict(os.environ, {orca_bridge.SESSION_ENV: SELF}):
                st = studio.build_state(self.ns(orca_worktree=wt, orca_open=True, ai_target="self"))
            self.assertEqual(st.bridge.target, TARGET)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(orca_bridge.SESSION_ENV, None)
                with self.assertRaises(studio.StudioError):
                    studio.build_state(self.ns(orca_worktree=wt, ai_target="self"))
            with self.assertRaises(studio.StudioError):
                studio.build_state(self.ns(orca_worktree=wt, ai_target="not-a-handle"))
        finally:
            shutil.rmtree(wt, ignore_errors=True)

    def test_main_reports_bad_orca_args(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(studio.main(["--orca-worktree", "C:/definitely/not/here", "--ai-target", TARGET]), 2)
        self.assertIn("エラー", err.getvalue())


class UiSourceTests(unittest.TestCase):
    def read(self, f):
        with open(os.path.join(studio.STATIC_DIR, f), encoding="utf-8") as fh:
            return fh.read()

    def test_enter_does_not_send(self):
        js = self.read("shell.js")
        self.assertNotRegex(js, r"aiPrompt'\)\.addEventListener\('key")
        self.assertNotRegex(js, r"aiPrompt'\)\.addEventListener\('submit")
        html = self.read("shell.html")
        self.assertIn('id="aiPrompt"', html)
        self.assertNotIn("<form", html)                 # formのEnter送信を持たない

    def test_ui_has_required_controls(self):
        html = self.read("shell.html")
        for el in ("aiPrompt", "aiSendBtn", "shotBtn", "attachBtn", "aiThumbs"):
            self.assertIn('id="%s"' % el, html)
        js = self.read("shell.js")
        self.assertIn("送信登録", js)
        self.assertIn("受領・完了は未確認", js)
        self.assertIn("自動では再送しません", js)

    def test_screenshot_hides_ui_and_uses_frame_rect(self):
        js = self.read("shell.js")
        self.assertIn("frame.getBoundingClientRect()", js)
        self.assertIn("capturing", js)
        self.assertIn("ss: 'overlay', visible: false", js)
        self.assertIn("window.innerWidth", js)
        self.assertIn("$('stage').getBoundingClientRect()", js)          # iframe ∩ stage ∩ viewport
        self.assertIn("Math.max(r.left, st.left, 0)", js)
        self.assertIn("Math.min(r.right, st.right, window.innerWidth)", js)


if __name__ == "__main__":
    unittest.main()
