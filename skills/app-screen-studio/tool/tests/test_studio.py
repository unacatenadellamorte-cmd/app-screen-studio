"""app-screen-studio のテスト。成果物は --output-root 相当のTemporaryDirectoryにだけ書く。

実行: python -X utf8 -m unittest discover -s tests -v   (app-screen-studio フォルダで)
"""
import contextlib
import http.client
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import studio  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def valid_payload(project="tproj", **over):
    p = {
        "schema": studio.SCHEMA, "project": project,
        "screen": {"path": "/mobile/web/index.html", "title": "テスト"},
        "timestamp": "2026-10-28T10:00:00Z",
        "viewport": {"mode": "phone", "width": 390, "height": 844},
        "edits": [{
            "op": "modify",
            "target": {"edit_id": "demo-title", "selector": "[data-edit-id=\"demo-title\"]",
                       "tag": "h1", "text_hint": "家計かんたんメモ"},
            "before": {"text": "家計かんたんメモ", "style": {"color": "rgb(255, 255, 255)"}, "attrs": {}},
            "after": {"text": "家計メモ", "style": {"color": "#ff0000", "fontSize": "24px"}, "attrs": {}},
            "source": {"source": "demo/index.html"}, "viewport_width": 390,
        }],
    }
    p.update(over)
    return p


class Server:
    """テスト用サーバー一式(127.0.0.1の空きポート、成果物は一時フォルダ)。"""

    def __init__(self, root=None, upstream=None, project="tproj"):
        self.tmp = tempfile.mkdtemp(prefix="ss-test-")
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out)
        if upstream:
            self.state = studio.StudioState(project, "url", upstream=upstream, entry="/",
                                            output_root=self.out)
        else:
            self.root = root or os.path.join(self.tmp, "site")
            self.state = studio.StudioState(project, "dir", root=self.root, entry="/index.html",
                                            output_root=self.out)
        self.srv = studio.create_server(self.state, 0)
        self.port = self.state.port
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or "127.0.0.1:%d" % self.port}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict((k.lower(), v) for k, v in r.getheaders()), data

    def token(self):
        return self.state.token

    def origin(self):
        return "http://127.0.0.1:%d" % self.port

    def save(self, payload, origin="default", token="default", ctype="application/json"):
        h = {"Content-Type": ctype}
        o = self.origin() if origin == "default" else origin
        if o is not None:
            h["Origin"] = o
        t = self.token() if token == "default" else token
        if t is not None:
            h["X-Studio-Token"] = t
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return self.req("POST", studio.PREFIX + "/api/save", body, h)


def make_site(base):
    site = os.path.join(base, "site")
    os.makedirs(os.path.join(site, "mobile", "web"))
    os.makedirs(os.path.join(site, ".git"))
    def w(p, t):
        with open(os.path.join(site, *p.split("/")), "w", encoding="utf-8") as f:
            f.write(t)
    w("index.html", "<!doctype html><html><head><title>t</title></head><body><h1>こんにちは</h1></body></html>")
    w("mobile/web/index.html", "<html><head></head><body><p>m</p></body></html>")
    w("style.css", "body{color:red}")
    w("app.js", "var a=1;")
    w(".env", "SECRET=1")
    w(".git/config", "[core]")
    w("server.py", "print(1)")
    w("secret-notes.txt", "x")
    w("api-token.json", "{}")
    w("key.pem", "x")
    return site


class StaticTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ss-site-")
        self.site = make_site(self.base)
        self.s = Server(root=self.site)

    def tearDown(self):
        self.s.close()
        shutil.rmtree(self.base, ignore_errors=True)

    def test_html_gets_external_injection_only(self):
        st, h, body = self.s.req("GET", "/index.html")
        t = body.decode("utf-8")
        self.assertEqual(st, 200)
        self.assertIn('<link rel="stylesheet" href="/__screen_studio__/editor.css"></head>', t)
        self.assertIn('<script src="/__screen_studio__/editor.js" defer></script></body>', t)
        self.assertNotIn("onclick", t)
        self.assertEqual(t.count("<script"), 1)       # inline scriptは入れない
        self.assertEqual(h["cache-control"], "no-store")
        self.assertNotIn("content-security-policy", h)  # CSPを勝手に外す/足すことはしない

    def test_injection_is_not_duplicated(self):
        out = studio.inject(studio.inject(b"<head></head><body></body>"))
        self.assertEqual(out.count(b"editor.js"), 1)

    def test_assets_and_mime(self):
        st, h, _ = self.s.req("GET", "/style.css")
        self.assertEqual((st, h["content-type"]), (200, "text/css; charset=utf-8"))
        st, h, _ = self.s.req("GET", "/app.js")
        self.assertEqual(st, 200)
        self.assertTrue(h["content-type"].startswith("text/javascript"))

    def test_nested_html_and_root_redirect(self):
        self.assertEqual(self.s.req("GET", "/mobile/web/index.html")[0], 200)
        st, h, _ = self.s.req("GET", "/")
        self.assertEqual((st, h["location"]), (302, "/index.html"))

    def test_denied_files(self):
        for p in ("/.env", "/.git/config", "/server.py", "/secret-notes.txt", "/api-token.json",
                  "/key.pem", "/nothing.html"):
            self.assertEqual(self.s.req("GET", p)[0], 404, p)

    def test_no_directory_listing(self):
        for p in ("/mobile", "/mobile/", "/mobile/web/", "/.git/"):
            self.assertEqual(self.s.req("GET", p)[0], 404, p)

    def test_path_traversal_rejected(self):
        secret = os.path.join(self.base, "outside.html")
        with open(secret, "w") as f:
            f.write("<p>outside</p>")
        for p in ("/../outside.html", "/%2e%2e/outside.html", "/..%2foutside.html",
                  "/mobile/../../outside.html", "/mobile/%2e%2e/%2e%2e/outside.html",
                  "/..\\outside.html", "/%5c..%5coutside.html", "/index.html%00.png",
                  "/C:/Windows/win.ini", "//outside.html"):
            st, _, body = self.s.req("GET", p)
            self.assertIn(st, (400, 404), p)
            self.assertNotIn(b"outside", body, p)

    def test_link_escape_rejected(self):
        outside = os.path.join(self.base, "outside_dir")
        os.makedirs(outside)
        with open(os.path.join(outside, "leak.html"), "w") as f:
            f.write("<p>LEAK</p>")
        link = os.path.join(self.site, "link")
        made = False
        try:
            os.symlink(outside, link, target_is_directory=True)
            made = True
        except (OSError, NotImplementedError):
            if os.name == "nt":      # 権限不要のジャンクションで代用
                made = subprocess.run(["cmd", "/c", "mklink", "/J", link, outside],
                                      capture_output=True).returncode == 0
        if not made:
            self.skipTest("リンクを作れない環境")
        try:
            st, _, body = self.s.req("GET", "/link/leak.html")
            self.assertEqual(st, 404)
            self.assertNotIn(b"LEAK", body)
        finally:
            if os.path.isdir(link):
                os.rmdir(link) if os.name == "nt" else os.unlink(link)

    def test_bad_host_rejected(self):
        self.assertEqual(self.s.req("GET", "/index.html", host="evil.example:80")[0], 403)
        self.assertEqual(self.s.req("GET", "/index.html", host="127.0.0.1:1")[0], 403)
        self.assertEqual(self.s.req("GET", "/__screen_studio__/api/session",
                                    host="attacker.test")[0], 403)

    def test_static_mode_rejects_writes(self):
        self.assertEqual(self.s.req("POST", "/index.html", b"x")[0], 405)

    def test_shell_and_editor_files(self):
        st, h, body = self.s.req("GET", "/__screen_studio__/shell?path=%2Findex.html")
        self.assertEqual(st, 200)
        self.assertIn("script-src 'self'", h["content-security-policy"])
        self.assertNotIn(b"<script>", body)
        for f in ("editor.js", "editor.css", "shell.js", "shell.css"):
            self.assertEqual(self.s.req("GET", "/__screen_studio__/" + f)[0], 200)
        self.assertEqual(self.s.req("GET", "/__screen_studio__/../studio.py")[0], 404)
        self.assertEqual(self.s.req("GET", "/__screen_studio__/studio.py")[0], 404)

    def test_session_endpoint_blocks_cross_site(self):
        st, _, body = self.s.req("GET", "/__screen_studio__/api/session")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["project"], "tproj")
        self.assertEqual(self.s.req("GET", "/__screen_studio__/api/session",
                                    headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.s.req("GET", "/__screen_studio__/api/session",
                                    headers={"Origin": "http://evil.example"})[0], 403)


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.s = Server(root=make_site(tempfile.mkdtemp(prefix="ss-site2-")))
        self.dir = os.path.join(self.s.out, "tproj", "関連資料", "画面編集")

    def tearDown(self):
        shutil.rmtree(os.path.dirname(self.s.root), ignore_errors=True)
        self.s.close()

    def test_save_writes_ai_readable_json(self):
        st, _, body = self.s.save(valid_payload())
        self.assertEqual(st, 200, body)
        res = json.loads(body)
        self.assertTrue(res["ok"])
        path = os.path.join(self.dir, "mobile__web__index.json")
        self.assertEqual(os.path.normcase(res["path"]), os.path.normcase(path))
        data = json.load(open(path, encoding="utf-8"))
        self.assertEqual(data["schema"], studio.SCHEMA)
        self.assertEqual(data["project"], "tproj")
        self.assertEqual(data["screen"]["path"], "/mobile/web/index.html")
        self.assertEqual(data["viewport"]["mode"], "phone")
        self.assertFalse(data["applied_to_source"])
        self.assertIn("製品ソースには未反映", data["ai_instructions"])
        e = data["edits"][0]
        self.assertEqual(e["op"], "modify")
        self.assertEqual(e["target"]["edit_id"], "demo-title")
        self.assertEqual(e["target"]["tag"], "h1")
        self.assertEqual(e["before"]["text"], "家計かんたんメモ")
        self.assertEqual(e["after"]["style"]["fontSize"], "24px")
        self.assertEqual(e["source"]["source"], "demo/index.html")
        self.assertIn("timestamp", data)
        self.assertIn("server_saved_at", data)

    def test_rejected_body_is_not_logged_or_reparsed(self):
        marker = b"REJECTED_PRIVATE_BODY_SENTINEL\r\n"
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            status, headers, _ = self.s.save(marker, token="incorrect")
        self.assertEqual(status, 403)
        self.assertEqual(headers.get("connection"), "close")
        self.assertNotIn(marker.decode().strip(), captured.getvalue())

    def test_layer_and_signature_survive_save(self):
        payload = valid_payload()
        payload["edits"][0]["target"]["signature"] = "12abcdef"
        payload["edits"][0]["after"]["style"]["zIndex"] = "4"
        self.assertEqual(self.s.save(payload)[0], 200)
        data = self.s.state.load(payload["screen"]["path"])["data"]
        self.assertEqual(data["edits"][0]["target"]["signature"], "12abcdef")
        self.assertEqual(data["edits"][0]["after"]["style"]["zIndex"], "4")

    def test_resave_quarantines_old_version(self):
        self.assertEqual(self.s.save(valid_payload())[0], 200)
        p2 = valid_payload()
        p2["edits"][0]["after"]["text"] = "二回目"
        st, _, body = self.s.save(p2)
        res = json.loads(body)
        self.assertEqual(st, 200)
        self.assertEqual(res["status"], "replaced")
        self.assertTrue(res["trash_id"])
        trash = os.path.join(self.s.out, "一時ゴミ箱")
        self.assertTrue(os.path.isdir(trash))
        manifest = json.load(open(os.path.join(trash, "manifest.json"), encoding="utf-8"))
        text = json.dumps(manifest, ensure_ascii=False)
        self.assertIn("mobile__web__index.json", text)
        saved = json.load(open(os.path.join(self.dir, "mobile__web__index.json"), encoding="utf-8"))
        self.assertEqual(saved["edits"][0]["after"]["text"], "二回目")
        # 旧版は一時ゴミ箱内に残っている(直接削除していない)
        found = [f for r, _, fs in os.walk(trash) for f in fs if f.endswith(".json") and f != "manifest.json"]
        self.assertTrue(found)

    def test_load_roundtrip(self):
        self.s.save(valid_payload())
        st, _, body = self.s.req("GET", "/__screen_studio__/api/load?path=%2Fmobile%2Fweb%2Findex.html",
                                 headers={"X-Studio-Token": self.s.token()})
        r = json.loads(body)
        self.assertTrue(r["exists"])
        self.assertEqual(r["data"]["edits"][0]["after"]["style"]["fontSize"], "24px")
        st, _, body = self.s.req("GET", "/__screen_studio__/api/load?path=%2Fnone.html",
                                 headers={"X-Studio-Token": self.s.token()})
        self.assertFalse(json.loads(body)["exists"])
        self.assertEqual(self.s.req("GET", "/__screen_studio__/api/load?path=%2Fx")[0], 403)  # token無し

    def test_origin_checks(self):
        self.assertEqual(self.s.save(valid_payload(), origin=None)[0], 403)
        self.assertEqual(self.s.save(valid_payload(), origin="http://evil.example")[0], 403)
        self.assertEqual(self.s.save(valid_payload(), origin="http://127.0.0.1:1")[0], 403)
        self.assertEqual(self.s.save(valid_payload(), origin="null")[0], 403)
        self.assertEqual(self.s.save(valid_payload(), origin="http://localhost:%d" % self.s.port)[0], 200)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "x.json")))

    def test_material_and_glow_save_load(self):
        # 実際の保存・再読込で材質と発光のCSS値が落ちないことを確認する。
        p = valid_payload()
        style = {'backgroundImage': 'linear-gradient(135deg,#fff,#a0aab8)',
                 'backdropFilter': 'blur(12px)',
                 'textShadow': '0 0 5px rgba(85,204,255,0.5),0 0 20px rgba(85,204,255,0.4)'}
        p['edits'][0]['after']['style'].update(style)
        self.assertEqual(self.s.save(p)[0], 200)
        st, _, body = self.s.req('GET', '/__screen_studio__/api/load?path=%2Fmobile%2Fweb%2Findex.html',
                                 headers={'X-Studio-Token': self.s.token()})
        loaded = json.loads(body)['data']['edits'][0]['after']['style']
        for key, value in style.items():
            self.assertEqual(loaded[key], value)

    def test_material_rejects_external_css_urls(self):
        # 材質追加で外部参照やスクリプト用の抜け道を増やさない。
        for value in ['url(https://example.com/a.png)', 'URL (https://example.com/a.png)',
                      'linear-gradient(#fff,#000);color:red', 'expression(alert(1))']:
            p = valid_payload()
            p['edits'][0]['after']['style']['backgroundImage'] = value
            with self.subTest(value=value):
                self.assertEqual(self.s.save(p)[0], 400)

    def test_material_preserves_existing_background_reference(self):
        # 既存画像を材質へ置き換える場合も、元の背景を照合情報として保存できる。
        p = valid_payload()
        p['edits'][0]['before']['style']['backgroundImage'] = 'url("http://127.0.0.1:4798/images/bg.png")'
        p['edits'][0]['after']['style']['backgroundImage'] = 'linear-gradient(#fff,#aaa)'
        self.assertEqual(self.s.save(p)[0], 200)

    def test_detailed_material_layers_roundtrip_and_limit(self):
        p = valid_payload()
        layers = ['linear-gradient(135deg,rgba(255,255,255,0.25),transparent 65%)'] * 12
        value = ','.join(layers)
        self.assertGreater(len(value), 300)
        p['edits'][0]['after']['style']['backgroundImage'] = value
        self.assertEqual(self.s.save(p)[0], 200)
        p['edits'][0]['after']['style']['backgroundImage'] = value * 4
        self.assertGreater(len(value * 4), 2000)
        self.assertEqual(self.s.save(p)[0], 400)

    def test_token_checks(self):
        self.assertEqual(self.s.save(valid_payload(), token=None)[0], 403)
        self.assertEqual(self.s.save(valid_payload(), token="wrong")[0], 403)
        self.assertEqual(self.s.save(valid_payload(), token="")[0], 403)
        self.assertFalse(os.path.exists(self.dir))     # 拒否時は何も書かれない

    def test_content_type_and_size_limits(self):
        self.assertEqual(self.s.save(valid_payload(), ctype="text/plain")[0], 415)
        big = valid_payload()
        big["edits"][0]["after"]["text"] = "あ" * 6000
        self.assertEqual(self.s.save(big)[0], 400)          # テキスト長上限
        huge = b"{" + b" " * (studio.MAX_BODY + 10) + b"}"
        self.assertEqual(self.s.save(huge)[0], 413)         # 本文上限
        self.assertEqual(self.s.save(b"not json")[0], 400)
        self.assertEqual(self.s.save(b"[1,2]")[0], 400)
        self.assertFalse(os.path.exists(self.dir))

    def test_invalid_payloads_rejected(self):
        def mod(fn):
            p = valid_payload()
            fn(p)
            return p
        cases = {
            "schema": mod(lambda p: p.update(schema="x")),
            "project一致": mod(lambda p: p.update(project="other")),
            "project型": mod(lambda p: p.update(project=["tproj"])),
            "screen.path": mod(lambda p: p["screen"].update(path="relative")),
            "viewport.mode": mod(lambda p: p["viewport"].update(mode="watch")),
            "viewport.width": mod(lambda p: p["viewport"].update(width=-1)),
            "edits型": mod(lambda p: p.update(edits="x")),
            "edits多すぎ": mod(lambda p: p.update(edits=[valid_payload()["edits"][0]] * (studio.MAX_EDITS + 1))),
            "op": mod(lambda p: p["edits"][0].update(op="eval")),
            "style key": mod(lambda p: p["edits"][0]["after"]["style"].update(behavior="x")),
            "style key2": mod(lambda p: p["edits"][0]["after"]["style"].update(__proto__="x")),
            "style url": mod(lambda p: p["edits"][0]["after"]["style"].update(backgroundColor="url(http://x)")),
            "style 括弧注入": mod(lambda p: p["edits"][0]["after"]["style"].update(color="red;}</style><script>")),
            "style 型": mod(lambda p: p["edits"][0]["after"]["style"].update(color=5)),
            "attr key": mod(lambda p: p["edits"][0]["after"]["attrs"].update(onerror="x")),
            "src js": mod(lambda p: p["edits"][0]["after"]["attrs"].update(src="javascript:alert(1)")),
            "src data": mod(lambda p: p["edits"][0]["after"]["attrs"].update(src="data:image/png;base64,AA")),
            "selector制御文字": mod(lambda p: p["edits"][0]["target"].update(selector="a\x00b")),
            "selector <": mod(lambda p: p["edits"][0]["target"].update(selector="<script>")),
            "selector 空": mod(lambda p: p["edits"][0]["target"].update(selector="")),
            "tag": mod(lambda p: p["edits"][0]["target"].update(tag="H1 onclick")),
            "target欠落": mod(lambda p: p["edits"][0].pop("target")),
            "text型": mod(lambda p: p["edits"][0]["after"].update(text=["x"])),
            "add parent無し": mod(lambda p: p["edits"][0].update(op="add", shape="rect", index=0)),
            "add shape不正": mod(lambda p: p["edits"][0].update(
                op="add", shape="iframe", index=0, parent=p["edits"][0]["target"])),
            "add index不正": mod(lambda p: p["edits"][0].update(
                op="add", shape="rect", index=-1, parent=p["edits"][0]["target"])),
        }
        for name, payload in cases.items():
            st, _, body = self.s.save(payload)
            self.assertEqual(st, 400, "%s -> %s %s" % (name, st, body))
        self.assertFalse(os.path.exists(self.dir), "不正payloadでは何も書かれない")

    def test_unknown_keys_are_dropped(self):
        p = valid_payload()
        p["evil"] = "x"
        p["edits"][0]["extra"] = {"a": 1}
        self.assertEqual(self.s.save(p)[0], 200)
        data = json.load(open(os.path.join(self.dir, "mobile__web__index.json"), encoding="utf-8"))
        self.assertNotIn("evil", data)
        self.assertNotIn("extra", data["edits"][0])

    def test_structural_ops_are_accepted(self):
        t = {"edit_id": "ss-new-1", "selector": "[data-edit-id=\"ss-new-1\"]", "tag": "div"}
        parent = {"selector": "body", "tag": "body"}
        p = valid_payload()
        p["edits"] = [
            {"op": "add", "shape": "rect", "target": t, "parent": parent, "index": 3,
             "before": {"style": {}, "attrs": {}},
             "after": {"style": {"position": "absolute", "left": "60px", "top": "60px",
                                 "width": "140px", "height": "90px", "rotate": "15deg",
                                 "backgroundColor": "#3d6b5e"}, "attrs": {}}},
            {"op": "duplicate", "target": dict(t, edit_id="ss-new-2"), "parent": parent, "index": 4,
             "clone_of": {"selector": "ul > li:nth-of-type(1)", "tag": "li", "text_hint": "食費"},
             "before": {}, "after": {"style": {"translate": "10px 0px"}, "attrs": {}, "text": "食費"}},
            {"op": "delete", "target": {"selector": "ul > li:nth-of-type(3)", "tag": "li", "text_hint": "日用品"},
             "before": {}, "after": {}},
        ]
        st, _, body = self.s.save(p)
        self.assertEqual(st, 200, body)
        data = json.load(open(os.path.join(self.dir, "mobile__web__index.json"), encoding="utf-8"))
        self.assertEqual([e["op"] for e in data["edits"]], ["add", "duplicate", "delete"])
        self.assertEqual(data["edits"][0]["shape"], "rect")
        self.assertEqual(data["edits"][0]["parent"]["tag"], "body")
        self.assertEqual(data["edits"][1]["clone_of"]["text_hint"], "食費")

    def test_token_not_logged(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.s.save(valid_payload())
            self.s.save(valid_payload(), token="wrong-secret-token")
            self.s.req("GET", "/__screen_studio__/api/load?path=%2Fx", headers={"X-Studio-Token": self.s.token()})
        self.assertNotIn(self.s.token(), buf.getvalue())
        self.assertNotIn("wrong-secret-token", buf.getvalue())

    def test_project_name_validated(self):
        for bad in ("..", "a/b", "con", "x:y", ".hidden", "一時ゴミ箱", ""):
            with self.assertRaises(Exception):
                studio.StudioState(bad, "dir", root=self.s.root, output_root=self.s.out)


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.s = Server(root=make_site(tempfile.mkdtemp(prefix="ss-site3-")))

    def tearDown(self):
        shutil.rmtree(os.path.dirname(self.s.root), ignore_errors=True)
        self.s.close()

    def upload(self, data, name="logo.png", token="default", origin="default"):
        h = {"Content-Type": "application/octet-stream"}
        o = self.s.origin() if origin == "default" else origin
        if o:
            h["Origin"] = o
        t = self.s.token() if token == "default" else token
        if t:
            h["X-Studio-Token"] = t
        return self.s.req("POST", studio.PREFIX + "/api/asset?name=" + name, data, h)

    def test_upload_and_serve(self):
        st, _, body = self.upload(PNG)
        self.assertEqual(st, 200, body)
        r = json.loads(body)
        self.assertTrue(r["url"].startswith("/__screen_studio__/assets/"))
        self.assertTrue(os.path.isfile(r["path"]))
        self.assertIn(os.path.join("tproj", "画像", "画面編集"), r["path"])
        st, h, data = self.s.req("GET", r["url"])
        self.assertEqual((st, h["content-type"], data), (200, "image/png", PNG))
        self.assertEqual(h["x-content-type-options"], "nosniff")

    def test_upload_rejections(self):
        self.assertEqual(self.upload(b"<svg onload=alert(1)>")[0], 415)
        self.assertEqual(self.upload(b"GIF89a" + b"\0" * 10, name="../../evil.php")[0], 200)  # 名前は無害化される
        self.assertEqual(self.upload(PNG, token="bad")[0], 403)
        self.assertEqual(self.upload(PNG, origin="http://evil.example")[0], 403)
        self.assertEqual(self.upload(PNG, origin="")[0], 403)
        self.assertEqual(self.upload(b"")[0], 413)
        self.assertEqual(self.upload(b"\x89PNG\r\n\x1a\n" + b"0" * (studio.MAX_ASSET + 1))[0], 413)
        names = os.listdir(os.path.join(self.s.out, "tproj", "画像", "画面編集"))
        self.assertTrue(all(n.endswith((".gif", ".png")) and ".." not in n and "/" not in n for n in names))

    def test_asset_get_rejects_traversal(self):
        for p in ("../x.png", "..%2fx.png", "%2e%2e/x.png", "a.php", ".hidden.png", "nope.png"):
            self.assertEqual(self.s.req("GET", "/__screen_studio__/assets/" + p)[0], 404, p)

    def test_save_lists_assets(self):
        url = json.loads(self.upload(PNG)[2])["url"]
        t = {"edit_id": "ss-new-1", "selector": "[data-edit-id=\"ss-new-1\"]", "tag": "img"}
        p = valid_payload()
        p["edits"] = [{"op": "add", "shape": "image", "target": t,
                       "parent": {"selector": "body", "tag": "body"}, "index": 1,
                       "before": {}, "after": {"style": {"width": "160px"}, "attrs": {"src": url}}}]
        st, _, body = self.s.save(p)
        self.assertEqual(st, 200, body)
        data = json.load(open(os.path.join(self.s.out, "tproj", "関連資料", "画面編集",
                                           "mobile__web__index.json"), encoding="utf-8"))
        self.assertEqual(len(data["assets"]), 1)
        a = data["assets"][0]
        self.assertEqual(a["url"], url)
        self.assertTrue(os.path.isfile(a["path"]))
        self.assertEqual(len(a["sha256"]), 64)

    def test_save_rejects_missing_asset(self):
        p = valid_payload()
        p["edits"][0]["after"]["attrs"] = {"src": "/__screen_studio__/assets/ffffffffff-none.png"}
        self.assertEqual(self.s.save(p)[0], 400)


class UnitTests(unittest.TestCase):
    def test_screen_safe_name(self):
        f = studio.screen_safe_name
        self.assertEqual(f("/mobile/web/index.html"), "mobile__web__index")
        self.assertEqual(f("/"), "index")
        self.assertTrue(f("/a.html?x=1").startswith("a__"))
        self.assertNotEqual(f("/a.html?x=1"), f("/a.html?x=2"))
        self.assertTrue(f("/app#/settings/user").startswith("app__settings__user__"))
        self.assertNotEqual(f("/index.html?dilScreenPreview=1#connect"), f("/index.html?dilScreenPreview=1#voice"))
        self.assertNotEqual(f("/index.html#connect"), f("/index.html?dilScreenPreview=1#connect"))
        self.assertEqual(f("/../../etc/passwd"), "etc__passwd")
        self.assertEqual(f("/画面/一覧.html"), "画面__一覧")
        n = f("/con/nul/aux")
        self.assertNotIn("/", n)
        self.assertNotIn("\\", n)
        self.assertLessEqual(len(f("/" + "a" * 500)), 80)

    def test_parse_upstream(self):
        self.assertEqual(studio.parse_upstream("http://127.0.0.1:5173"), ("http", "127.0.0.1", 5173))
        self.assertEqual(studio.parse_upstream("http://localhost:3000/x"), ("http", "localhost", 3000))
        for bad in ("http://example.com", "http://192.168.0.5:5173", "http://0.0.0.0:5173",
                    "https://evil.test:443", "ftp://127.0.0.1", "file:///etc/passwd",
                    "http://user:pw@127.0.0.1:5173", "http://127.0.0.1.evil.com:80",
                    "http://localhost.evil.com", "//127.0.0.1", "127.0.0.1:5173"):
            with self.assertRaises(studio.StudioError, msg=bad):
                studio.parse_upstream(bad)

    def test_dir_and_url_are_exclusive(self):
        ns = type("A", (), dict(project="p", dir=HERE, url="http://127.0.0.1:1", entry=None,
                                output_root=None))()
        with self.assertRaises(studio.StudioError):
            studio.build_state(ns)

    def test_bad_entry_rejected(self):
        for e in ("..", "/../x", "x.html", "/a\\b"):
            ns = type("A", (), dict(project="p", dir=HERE, url=None, entry=e, output_root="x"))()
            with self.assertRaises(studio.StudioError, msg=e):
                studio.build_state(ns)

    def test_main_rejects_external_url_and_both(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(studio.main(["--project", "p", "--url", "http://example.com"]), 2)
            self.assertEqual(studio.main(["--project", "p", "--dir", HERE, "--url", "http://127.0.0.1:1"]), 2)
            self.assertEqual(studio.main(["--dir", HERE]), 2)    # project必須
        self.assertIn("エラー", err.getvalue())

    def test_demo_builds_without_dir(self):
        ns = type("A", (), dict(project=None, dir=None, url=None, entry=None, output_root="x"))()
        st = studio.build_state(type("A", (), dict(project="demo", dir=None, url=None, entry=None,
                                                   output_root="x"))())
        self.assertEqual((st.mode, st.entry), ("dir", "/index.html"))
        self.assertTrue(os.path.isfile(os.path.join(studio.DEMO_DIR, "index.html")))

    def test_demo_has_no_inline_script_or_style(self):
        with open(os.path.join(studio.DEMO_DIR, "index.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)")
        self.assertNotIn("<style", html)
        self.assertNotRegex(html, r"\sstyle=|\sonclick=|\sonload=")


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Up(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:%d/page" % cls.up_port)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/api/data":
                    body, ctype = b'{"ok":true}', "application/json"
                else:
                    body = b"<html><head></head><body><h1>up</h1></body></html>"
                    ctype = "text/html"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(n)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        cls.up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Up)
        cls.up_port = cls.up.server_address[1]
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.s = Server(upstream=("http", "127.0.0.1", cls.up_port))

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()
        cls.up.server_close()
        cls.s.close()

    def test_html_is_proxied_and_injected(self):
        st, h, body = self.s.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"/__screen_studio__/editor.js", body)
        self.assertIn(b"<h1>up</h1>", body)
        self.assertNotIn("x-frame-options", h)
        csp = h["content-security-policy"]
        self.assertIn("default-src 'self'", csp)       # CSP全体は残す
        self.assertNotIn("frame-ancestors", csp)       # iframe表示に要る部分だけ外す

    def test_non_html_passthrough_and_post(self):
        st, _, body = self.s.req("GET", "/api/data")
        self.assertEqual((st, body), (200, b'{"ok":true}'))
        st, _, body = self.s.req("POST", "/echo", b"hello", {"Content-Type": "text/plain"})
        self.assertEqual((st, body), (200, b"hello"))

    def test_redirect_stays_on_proxy(self):
        st, h, _ = self.s.req("GET", "/redirect")
        self.assertEqual((st, h["location"]), (302, "/page"))

    def test_studio_paths_not_forwarded(self):
        st, _, body = self.s.req("GET", "/__screen_studio__/nothing")
        self.assertEqual(st, 404)

    def test_upstream_down_gives_502(self):
        dead = Server(upstream=("http", "127.0.0.1", 1))
        try:
            self.assertEqual(dead.req("GET", "/")[0], 502)
        finally:
            dead.close()

    def test_proxy_checks_host(self):
        self.assertEqual(self.s.req("GET", "/", host="evil.example")[0], 403)


class JsTests(unittest.TestCase):
    def test_js_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node が無い")
        for f in ("editor.js", "shell.js", os.path.join("..", "demo", "demo.js")):
            r = subprocess.run([node, "--check", os.path.join(studio.STATIC_DIR, f)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f + ": " + r.stderr)

    def test_no_eval_or_inline_handlers_in_ui(self):
        for f in ("editor.js", "shell.js"):
            with open(os.path.join(studio.STATIC_DIR, f), encoding="utf-8") as fh:
                t = fh.read()
            for bad in ("eval(", "new Function", "innerHTML", "document.write", "setAttribute('style'",
                        "setAttribute(\"style\""):
                self.assertNotIn(bad, t, f + " に " + bad)
        with open(os.path.join(studio.STATIC_DIR, "shell.html"), encoding="utf-8") as fh:
            t = fh.read()
        self.assertNotRegex(t, r"<script(?![^>]*\bsrc=)")
        self.assertNotRegex(t, r"\son[a-z]+=|\sstyle=")


if __name__ == "__main__":
    unittest.main()
