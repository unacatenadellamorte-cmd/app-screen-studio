#!/usr/bin/env python
"""アプリ画面編集室 (app-screen-studio)

ローカル(127.0.0.1)だけで動く小さなHTTPサーバー。アプリ画面をブラウザーに表示し、
文字・色・位置・大きさを直接編集して、変更内容をJSONとして成果物フォルダへ保存する。
製品ソースの自動書換えはしない(AIがJSONを読んで反映する流れ)。標準ライブラリのみ。

使い方:
  python studio.py --project NAME --dir PATH [--entry /index.html] [--port 4780]
  python studio.py --project NAME --url http://127.0.0.1:5173
  python studio.py            (同梱デモを表示)
"""
import argparse
import datetime as dt
import hashlib
import io
import hmac
import http.client
import importlib.util
import json
import math
import mimetypes
import os
import posixpath
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import orca_bridge

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
DEMO_DIR = os.path.join(HERE, "demo")
DEFAULT_OUTPUT_ROOT = os.path.abspath(os.environ.get("APP_SCREEN_STUDIO_OUTPUT_ROOT") or os.path.join(os.path.expanduser("~"), "AppScreenStudio", "成果物"))
PREFIX = "/__screen_studio__"
SCHEMA = "app-screen-studio/edit/v1"
SAVE_CATEGORY = "関連資料"
SAVE_SUBDIR = "画面編集"

MAX_BODY = 1024 * 1024          # 保存POST本文の上限
MAX_EDITS = 500
MAX_TEXT = 5000                 # テキスト1件の上限(文字)
MAX_PROXY_BODY = 50 * 1024 * 1024

# --dir 静的配信で出すWeb拡張子(これ以外は404)
SERVE_EXT = {
    ".html": "text/html", ".htm": "text/html", ".css": "text/css",
    ".js": "text/javascript", ".mjs": "text/javascript", ".json": "application/json",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".ico": "image/x-icon", ".avif": "image/avif", ".woff": "font/woff",
    ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
    ".webmanifest": "application/manifest+json", ".txt": "text/plain",
}
# 公開してはいけない名前(拡張子が許可でも拒否)
DENY_NAME_PARTS = ("secret", "credential", "password", "passwd", "keystore",
                   "private", "id_rsa", "token", ".env", "signature", ".sig")

# 編集で許可するスタイルキー(editor.js の対応表と同じ)
_SIDES = ("Top", "Right", "Bottom", "Left")
ALLOWED_STYLE_KEYS = frozenset(
    ["color", "backgroundColor", "fontSize", "fontFamily", "fontWeight", "fontStyle",
     "textAlign", "lineHeight", "letterSpacing", "opacity", "width", "height",
     "translate", "rotate", "position", "left", "top", "zIndex", "objectFit", "boxShadow", "backgroundImage", "backdropFilter", "textShadow", "fill", "stroke", "strokeWidth",
     "borderTopLeftRadius", "borderTopRightRadius", "borderBottomRightRadius",
     "borderBottomLeftRadius"]
    + [f"padding{s}" for s in _SIDES] + [f"margin{s}" for s in _SIDES]
    + [f"border{s}Width" for s in _SIDES] + [f"border{s}Color" for s in _SIDES]
    + [f"border{s}Style" for s in _SIDES])
ALLOWED_ATTR_KEYS = frozenset(["src", "alt"])
EDIT_OPS = ("modify", "add", "duplicate", "delete")
SHAPES = ("rect", "ellipse", "line", "text", "image")
ASSET_URL_PREFIX = PREFIX + "/assets/"
ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
ASSET_CATEGORY = "画像"
MAX_ASSET = 5 * 1024 * 1024
ASSET_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
# AIへの依頼に添える画像。名前の接頭辞で種類を区別する(スクショと貼り付けは別添付)
ATTACH_KINDS = {"shot": ("shot-", "スクリーンショット"), "paste": ("paste-", "貼り付け画像"),
                "file": ("file-", "添付ファイル")}
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_PROMPT = 8000
MAX_ATTACH = 8
REQUEST_SUBDIR = SAVE_SUBDIR + "/依頼"
_STYLE_VALUE_RE = re.compile(r"^[A-Za-z0-9_\s.,#%()+\-/\"'぀-ヿ一-鿿！-～]*$")
_SELECTOR_RE = re.compile(r"^[^\x00-\x1f<{};`]{1,500}$")
_IDENT_RE = re.compile(r"^[^\x00-\x1f<>]{1,200}$")
_SAFE_SRC_RE = re.compile(r"^(?:[A-Za-z0-9_\-./%~@+=?&#:,;!()\[\] ]|[^\x00-\x7f])*$")

ARTIFACT_MANAGER = os.path.join(HERE, "artifact_manager.py")


def load_artifact_manager():
    spec = importlib.util.spec_from_file_location("artifact_manager", ARTIFACT_MANAGER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class StudioError(Exception):
    def __init__(self, status, code, message, extra=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.extra = extra or {}


# ------------------------------------------------------------ 検査ユーティリティ
def is_loopback_host(host):
    return host in ("127.0.0.1", "localhost", "::1", "[::1]")


def parse_upstream(url):
    """--url を検査して (scheme, host, port) を返す。loopback 以外は拒否。"""
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https"):
        raise StudioError(400, "bad_url", "--url は http(s):// で始めてください")
    if p.username or p.password:
        raise StudioError(400, "bad_url", "--url にユーザー情報は含められません")
    host = p.hostname or ""
    if not is_loopback_host(host):
        raise StudioError(400, "bad_url", "--url は 127.0.0.1 / localhost のみ指定できます")
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        raise StudioError(400, "bad_url", "--url のポートが不正です")
    return p.scheme, host, port


def safe_static_path(root_real, url_path):
    """URLパス→ルート配下の実ファイルパス。脱出・リンク・禁止名は StudioError(404)。"""
    try:
        raw = urllib.parse.unquote(url_path, errors="strict")
    except UnicodeDecodeError:
        raise StudioError(404, "not_found", "not found")
    if "\x00" in raw or "\\" in raw or ":" in raw:
        raise StudioError(404, "not_found", "not found")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    for p in parts:
        if p == ".." or p.startswith(".") or p[-1] in " .":
            raise StudioError(404, "not_found", "not found")
    if not parts:
        raise StudioError(404, "not_found", "not found")
    lower = parts[-1].lower()
    ext = os.path.splitext(lower)[1]
    if ext not in SERVE_EXT or any(w in lower for w in DENY_NAME_PARTS):
        raise StudioError(404, "not_found", "not found")
    joined = os.path.join(root_real, *parts)
    if not os.path.isfile(joined):
        raise StudioError(404, "not_found", "not found")
    # リンク/ジャンクション経由は、実体パスと字面パスが食い違うことで検出して拒否
    real = os.path.realpath(joined)
    if os.path.normcase(real) != os.path.normcase(os.path.abspath(joined)):
        raise StudioError(404, "not_found", "not found")
    if os.path.commonpath([os.path.normcase(real), os.path.normcase(root_real)]) \
            != os.path.normcase(root_real):
        raise StudioError(404, "not_found", "not found")
    return real, SERVE_EXT[ext]


def screen_safe_name(path):
    """画面パス→保存ファイル名(拡張子なし)。Windowsで安全な文字だけにする。"""
    original = str(path or "")
    parsed = urllib.parse.urlsplit(original)
    s = re.sub(r"\.(html?|php)$", "", parsed.path, flags=re.I)
    if parsed.fragment:
        s += "/" + parsed.fragment.lstrip("/")
    s = s.strip("/").replace("/", "__")
    s = "".join(c if (c.isalnum() or c in "_-") else "_" for c in s)
    s = re.sub(r"_{3,}", "__", s).strip("_.")[:80].strip("_.")
    s = s or "index"
    if parsed.query or parsed.fragment:
        s = s[:66] + "__" + hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
    return s


def _chk(cond, msg):
    if not cond:
        raise StudioError(400, "bad_payload", msg)


def _is_str(v, maxlen):
    return isinstance(v, str) and len(v) <= maxlen


def _clean_style(d, label):
    _chk(isinstance(d, dict), f"{label}.style はオブジェクト")
    out = {}
    for k, v in d.items():
        _chk(k in ALLOWED_STYLE_KEYS, f"許可されないスタイルキー: {str(k)[:40]}")
        if k == "backgroundImage" and label == "before" and isinstance(v, str) and "url(" in v.lower():
            # 既存背景画像は照合用の変更前情報としてのみ保持する。変更後の外部参照は許可しない。
            refs = re.findall(r'''url\(\s*(?:"([^"]*)"|'([^']*)'|([^)]*))\s*\)''', v, re.I)
            _chk(_is_str(v, 2000) and refs, "変更前の背景画像が不正")
            for ref in refs:
                _clean_attrs({"src": next((part for part in ref if part), "").strip()}, label)
            remaining = re.sub(r'''url\(\s*(?:"[^"]*"|'[^']*'|[^)]*)\s*\)''', 'none', v, flags=re.I)
            _chk(_STYLE_VALUE_RE.match(remaining) and not re.search(r"url\s*\(|expression|javascript|/\*", remaining, re.I), "変更前の背景画像が不正")
            out[k] = v
            continue
        limit = 2000 if k in {"backgroundImage", "boxShadow", "textShadow"} else 300
        _chk(_is_str(v, limit) and _STYLE_VALUE_RE.match(v), f"スタイル値が不正: {k}")
        low = v.lower()
        _chk(not re.search(r"url\s*\(", low) and "expression" not in low and "javascript" not in low
             and "/*" not in v, f"スタイル値が不正: {k}")
        out[k] = v
    return out


def _clean_attrs(d, label):
    _chk(isinstance(d, dict), f"{label}.attrs はオブジェクト")
    out = {}
    for k, v in d.items():
        _chk(k in ALLOWED_ATTR_KEYS, f"許可されない属性: {str(k)[:40]}")
        _chk(_is_str(v, 2000), f"属性値が長すぎる: {k}")
        if k == "src" and v:
            low = v.strip().lower()
            _chk(_SAFE_SRC_RE.match(v) is not None, "画像URLに使えない文字がある")
            _chk(not re.match(r"^(javascript|vbscript|file|data|blob):", low),
                 "画像URLのスキームは http / https / 相対パスのみ")
            if v.startswith(ASSET_URL_PREFIX):
                _chk(ASSET_NAME_RE.match(v[len(ASSET_URL_PREFIX):]) is not None, "取込画像の名前が不正")
        out[k] = v
    return out


def _clean_side(d, label):
    _chk(isinstance(d, dict), f"{label} はオブジェクト")
    out = {}
    if "text" in d:
        _chk(d["text"] is None or _is_str(d["text"], MAX_TEXT), f"{label}.text が不正/長すぎる")
        out["text"] = d["text"]
    out["style"] = _clean_style(d.get("style", {}), label)
    out["attrs"] = _clean_attrs(d.get("attrs", {}), label)
    return out


def _clean_target(t, label):
    _chk(isinstance(t, dict), f"{label} が不正")
    out = {}
    for k in ("edit_id", "id"):
        if t.get(k):
            _chk(isinstance(t[k], str) and _IDENT_RE.match(t[k]), f"{label}.{k} が不正")
            out[k] = t[k]
    _chk(_is_str(t.get("selector"), 500) and t["selector"] and _SELECTOR_RE.match(t["selector"]),
         f"{label}.selector が不正")
    out["selector"] = t["selector"]
    _chk(isinstance(t.get("tag"), str) and re.match(r"^[a-z][a-z0-9-]{0,30}$", t["tag"]),
         f"{label}.tag が不正")
    out["tag"] = t["tag"]
    if "signature" in t:
        _chk(isinstance(t["signature"], str) and re.fullmatch(r"[0-9a-f]{8}", t["signature"]),
             f"{label}.signature が不正")
        out["signature"] = t["signature"]
    if t.get("text_hint"):
        _chk(_is_str(t["text_hint"], 100), f"{label}.text_hint が長すぎる")
        out["text_hint"] = t["text_hint"]
    return out


def validate_payload(data, project):
    """保存POSTの検査と正規化。許可した項目だけを作り直して返す(未知キーは捨てる)。"""
    _chk(isinstance(data, dict), "payload はオブジェクト")
    _chk(data.get("schema") == SCHEMA, "schema が不正")
    _chk(data.get("project") == project, "project がこのサーバーと一致しない")
    screen = data.get("screen")
    _chk(isinstance(screen, dict) and _is_str(screen.get("path"), 300)
         and screen["path"].startswith("/"), "screen.path が不正")
    title = screen.get("title", "")
    _chk(_is_str(title, 200), "screen.title が長すぎる")
    _chk(_is_str(data.get("timestamp"), 40), "timestamp が不正")
    vp = data.get("viewport")
    _chk(isinstance(vp, dict) and vp.get("mode") in ("phone", "tablet", "pc", "custom"),
         "viewport.mode が不正")
    for k in ("width", "height"):
        _chk(isinstance(vp.get(k), int) and not isinstance(vp.get(k), bool)
             and 0 < vp[k] <= 10000, f"viewport.{k} が不正")
    edits = data.get("edits")
    _chk(isinstance(edits, list) and len(edits) <= MAX_EDITS, "edits が不正/多すぎる")
    clean_edits = []
    for i, e in enumerate(edits):
        _chk(isinstance(e, dict), f"edits[{i}] が不正")
        op = e.get("op", "modify")
        _chk(op in EDIT_OPS, f"edits[{i}].op が不正")
        item = {"op": op, "target": _clean_target(e.get("target"), "target"),
                "before": _clean_side(e.get("before", {}), "before"),
                "after": _clean_side(e.get("after", {}), "after")}
        if op in ("add", "duplicate"):
            item["parent"] = _clean_target(e.get("parent"), "parent")
            idx = e.get("index")
            _chk(isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx <= 100000,
                 "index が不正")
            item["index"] = idx
        if op == "add":
            _chk(e.get("shape") in SHAPES, "shape が不正")
            item["shape"] = e["shape"]
        if op == "duplicate":
            item["clone_of"] = _clean_target(e.get("clone_of"), "clone_of")
        src = e.get("source")
        if src is not None:
            _chk(isinstance(src, dict) and len(src) <= 8, "source が不正")
            item["source"] = {}
            for k, v in src.items():
                _chk(isinstance(k, str) and re.match(r"^[a-z][a-z0-9_-]{0,40}$", k)
                     and _is_str(v, 300), "source の値が不正")
                item["source"][k] = v
        wid = e.get("viewport_width")
        if wid is not None:
            _chk(isinstance(wid, int) and not isinstance(wid, bool) and 0 < wid <= 10000,
                 "viewport_width が不正")
            item["viewport_width"] = wid
        clean_edits.append(item)
    return {
        "schema": SCHEMA, "project": project,
        "screen": {"path": screen["path"], "title": title},
        "timestamp": data["timestamp"],
        "viewport": {"mode": vp["mode"], "width": vp["width"], "height": vp["height"]},
        "edits": clean_edits,
    }


def sniff_image_ext(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def crop_png(png, rect, vp):
    """撮影した全体PNGを、iframe(画面)の範囲だけに切り抜く。
    CSS座標→画像ピクセルは 画像幅/ウィンドウ幅 で換算(ズーム・DPI・scaleを吸収)。"""
    try:
        from PIL import Image
    except ImportError:
        raise StudioError(501, "no_pillow", "Pillow が未導入です(pip install pillow)")
    try:
        img = Image.open(io.BytesIO(png))
        img.load()
    except Exception:
        raise StudioError(502, "shot_failed", "撮影画像を読めませんでした")
    W, H = img.size
    sx, sy = W / float(vp["w"]), H / float(vp["h"])
    left, top = max(0, math.floor(rect["x"] * sx)), max(0, math.floor(rect["y"] * sy))
    right = min(W, math.ceil((rect["x"] + rect["w"]) * sx))
    bottom = min(H, math.ceil((rect["y"] + rect["h"]) * sy))
    if right - left < 8 or bottom - top < 8:
        raise StudioError(409, "shot_outside", "画面が見える範囲に入っていません")
    want_w, want_h = rect["w"] * sx, rect["h"] * sy
    clipped = (right - left) < want_w - 2 or (bottom - top) < want_h - 2
    out = img.crop((left, top, right, bottom)).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue(), clipped, out.size


AI_NOTE = (
    "このJSONはブラウザー上の見た目の編集記録です。製品ソースには未反映です"
    "(applied_to_source=false)。AIは各editのtarget(edit_id > id > selector の順で要素を特定、"
    "tagとtext_hintで照合)とbefore/afterの差分を読み、元の画面ソース(source属性があれば"
    "それを優先)へ反映してください。before.styleは編集前の計算値、after.styleは指定した値です。"
    "opがaddは新しい図形・画像(parent内のindex位置、afterが最終状態)、duplicateはclone_ofの複製、"
    "deleteは削除です。assetsの画像は記載パスのファイルを製品の画像置き場へコピーして参照"
    "してください。一致しない要素は推測で当てず、報告してください。")


# ------------------------------------------------------------ サーバー
class StudioState:
    def __init__(self, project, mode, root=None, upstream=None, entry="/index.html",
                 output_root=DEFAULT_OUTPUT_ROOT, port=0, bridge=None):
        self.am = load_artifact_manager()
        self.am.check_name(project, "project", top=True)
        self.project = project
        self.mode = mode                      # "dir" | "url"
        self.root = os.path.realpath(root) if root else None
        self.upstream = upstream              # (scheme, host, port)
        self.entry = entry
        self.output_root = output_root
        self.token = secrets.token_urlsafe(32)
        self.port = port
        self.lock = threading.Lock()
        self.bridge = bridge                  # OrcaBridge | None(未設定なら送信・撮影は無効)
        self.ai_lock = threading.Lock()       # AIへの送信は同時に1件だけ(連打防止)
        self.shot_lock = threading.Lock()
        self.ai_results = {}                  # request_id -> 結果(同じidの再送を防ぐ)

    def allowed_origins(self):
        return {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}

    def allowed_hosts(self):
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def screen_file(self, screen_path):
        return screen_safe_name(screen_path) + ".json"

    def asset_dir(self):
        return os.path.join(self.output_root, self.project, ASSET_CATEGORY, SAVE_SUBDIR)

    def checked_output_file(self, path):
        """保存先の親も含め、リンク経由の外部ファイルを読み出さない。"""
        try:
            root, parts = self.am.resolve_under_root(self.output_root, path)
            self.am.check_root_path(root)
            if not self.am.walk_no_links(root, parts):
                raise StudioError(404, "not_found", "not found")
        except self.am.ToolError:
            raise StudioError(404, "not_found", "not found")
        return os.path.join(root, *parts)

    def save_asset(self, name, data, prefix=""):
        """取り込み画像を 成果物/<project>/画像/画面編集/ へ保存(publish経由)。"""
        ext = sniff_image_ext(data)
        if not ext:
            raise StudioError(415, "bad_image", "PNG / JPEG / GIF / WebP の画像だけ取り込めます")
        if not data or len(data) > MAX_ASSET:
            raise StudioError(413, "too_large", "画像は5MBまでです")
        stem = re.sub(r"[^A-Za-z0-9_-]", "_", os.path.splitext(os.path.basename(name or "image"))[0])
        stem = stem.strip("_-")[:40] or "image"
        fname = "%s%s-%s%s" % (prefix, hashlib.sha256(data).hexdigest()[:10], stem, ext)
        with self.lock, tempfile.TemporaryDirectory(prefix="screen-studio-") as tmp:
            src = os.path.join(tmp, fname)
            with open(src, "wb") as f:
                f.write(data)
            try:
                self.am.publish(self.output_root, src, self.project, ASSET_CATEGORY,
                                name=fname, subdir=SAVE_SUBDIR)
            except self.am.ToolError as ex:
                raise StudioError(500, ex.code, ex.message)
        return {"name": fname, "url": ASSET_URL_PREFIX + fname,
                "path": os.path.join(self.asset_dir(), fname)}

    def read_asset(self, name):
        if not ASSET_NAME_RE.match(name or ""):
            raise StudioError(404, "not_found", "not found")
        ext = os.path.splitext(name)[1].lower()
        if ext not in ASSET_TYPES:
            raise StudioError(404, "not_found", "not found")
        p = self.checked_output_file(os.path.join(self.asset_dir(), name))
        if not os.path.isfile(p) or os.path.getsize(p) > MAX_ASSET:
            raise StudioError(404, "not_found", "not found")
        with open(p, "rb") as f:
            return f.read(MAX_ASSET + 1), ASSET_TYPES[ext]

    def collect_assets(self, edits):
        """editsが参照する取込画像を、AIが追える形(名前・実パス・sha256)で一覧にする。"""
        seen, out = set(), []
        for e in edits:
            v = e["after"]["attrs"].get("src", "")
            if v.startswith(ASSET_URL_PREFIX) and v not in seen:
                seen.add(v)
                name = v[len(ASSET_URL_PREFIX):]
                p = os.path.join(self.asset_dir(), name)
                try:
                    data, _ = self.read_asset(name)
                except StudioError:
                    raise StudioError(400, "bad_payload", "取り込んだ画像が見つかりません")
                out.append({"url": v, "path": p, "sha256": hashlib.sha256(data).hexdigest(),
                            "bytes": len(data)})
        return out

    def save(self, payload):
        clean = validate_payload(payload, self.project)
        clean["assets"] = self.collect_assets(clean["edits"])
        name = self.screen_file(clean["screen"]["path"])
        clean["server_saved_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        clean["applied_to_source"] = False
        clean["ai_instructions"] = AI_NOTE
        text = json.dumps(clean, ensure_ascii=False, indent=2) + "\n"
        with self.lock, tempfile.TemporaryDirectory(prefix="screen-studio-") as tmp:
            src = os.path.join(tmp, name)
            with open(src, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            try:
                res = self.am.publish(self.output_root, src, self.project, SAVE_CATEGORY,
                                      name=name, subdir=SAVE_SUBDIR)
            except self.am.ToolError as ex:
                raise StudioError(500, ex.code, ex.message)
        res["path"] = os.path.join(self.output_root, self.project, SAVE_CATEGORY,
                                   SAVE_SUBDIR, name)
        return res

    # ---------------------------------------------------- AIへの依頼(送信登録)・スクショ
    def ai_summary(self):
        if not self.bridge:
            return {"configured": False, "missing": orca_bridge.OrcaBridge().missing(),
                    "worktree": "", "target": "", "tab": ""}
        return self.bridge.summary()

    def _require_bridge(self):
        if not self.bridge or not self.bridge.configured():
            miss = (self.bridge or orca_bridge.OrcaBridge()).missing()
            raise StudioError(409, "ai_disabled", "AI連携が設定されていません: " + " / ".join(miss),
                              {"missing": miss})
        return self.bridge

    def ai_status(self):
        out = self.ai_summary()
        out["verified"] = False
        out["reasons"] = []
        if out["configured"]:
            v = self.bridge.verify(self.port)
            out["verified"], out["reasons"] = v["ok"], v["reasons"]
        return out

    def screenshot(self, req):
        """固定した編集タブを撮り、iframeの画面範囲だけに切り抜いて保存する。"""
        b = self._require_bridge()
        if not isinstance(req, dict):
            raise StudioError(400, "bad_payload", "撮影の形式が不正です")
        rect, vp = req.get("rect"), req.get("viewport")
        _chk_num = lambda d, keys: isinstance(d, dict) and all(
            isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool)
            and math.isfinite(d[k]) and -50000 < d[k] < 50000 for k in keys)
        if not (_chk_num(rect, ("x", "y", "w", "h")) and _chk_num(vp, ("w", "h"))
                and rect["w"] > 8 and rect["h"] > 8 and vp["w"] > 8 and vp["h"] > 8):
            raise StudioError(400, "bad_payload", "撮影範囲が不正です")
        if not self.shot_lock.acquire(blocking=False):
            raise StudioError(429, "busy", "撮影中です")
        try:
            reasons = b.check_tab(self.port)
            if reasons:
                raise StudioError(409, "ai_unbound", " / ".join(reasons))
            try:
                png = b.capture()
            except orca_bridge.BridgeError as ex:
                raise StudioError(502, ex.code, ex.message)
            cropped, clipped, size = crop_png(png, rect, vp)
            full = req.get("full")
            if _chk_num(full, ("w", "h")):
                clipped = clipped or rect["w"] < full["w"] - 1 or rect["h"] < full["h"] - 1
            res = self.save_asset("%s.png" % time.strftime("%Y%m%d-%H%M%S"), cropped, prefix="shot-")
            res.update(kind="shot", label=ATTACH_KINDS["shot"][1], clipped=clipped,
                       width=size[0], height=size[1])
            return res
        finally:
            self.shot_lock.release()

    def attach(self, kind, name, data):
        if kind not in ("paste", "file"):
            raise StudioError(400, "bad_payload", "kind が不正です")
        res = self.save_asset(name, data, prefix=ATTACH_KINDS[kind][0])
        res.update(kind=kind, label=ATTACH_KINDS[kind][1])
        return res

    def _resolve_attachments(self, names):
        if not isinstance(names, list) or len(names) > MAX_ATTACH:
            raise StudioError(400, "bad_payload", "添付は%d枚までです" % MAX_ATTACH)
        out, seen = [], set()
        for n in names:
            if not isinstance(n, str) or n in seen:
                raise StudioError(400, "bad_payload", "添付の指定が不正です")
            seen.add(n)
            kind = next((k for k, (pre, _) in ATTACH_KINDS.items() if n.startswith(pre)), None)
            if kind is None:
                raise StudioError(400, "bad_payload", "添付の種類を判別できません")
            try:
                self.read_asset(n)
            except StudioError:
                raise StudioError(400, "bad_payload", "添付画像が見つかりません")
            out.append({"kind": kind, "label": ATTACH_KINDS[kind][1],
                        "path": os.path.join(self.asset_dir(), n)})
        return out

    def _write_request(self, screen, prompt, json_path, edit_count, attachments):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = "%s-%s-%s.md" % (screen_safe_name(screen["path"])[:40], stamp, secrets.token_hex(2))
        lines = ["# 画面編集室からの依頼", "",
                 "- project: " + self.project,
                 "- 画面: " + screen["path"] + (("(" + screen["title"] + ")") if screen.get("title") else ""),
                 "- 作成: " + dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "- 注意: 画面上の見た目の編集は **製品コードに未反映** です(applied_to_source=false)。", "",
                 "## 依頼内容(本人の入力そのまま)", "", prompt, "",
                 "## 編集JSON", ""]
        lines.append("- パス: " + json_path + "(編集 %d 件)" % edit_count if json_path else "- なし(今回は画面の編集なし)")
        lines += ["", "## 添付画像", ""]
        if attachments:
            lines += ["- [%s] %s" % (a["label"], a["path"]) for a in attachments]
            lines += ["", "画像は上のパスのファイルを画像として開いて確認してください。"]
        else:
            lines.append("- なし")
        lines += ["", "## 進め方", "",
                  "1. 編集JSONがあれば読み、各 edit の target(edit_id → id → selector)と before/after を元ソースへ反映する。",
                  "2. 一致しない要素は推測で当てず報告する。",
                  "3. 反映後は画面を再表示して確認し、結果を依頼元へ返す。", ""]
        text = "\n".join(lines)
        with self.lock, tempfile.TemporaryDirectory(prefix="screen-studio-") as tmp:
            src = os.path.join(tmp, name)
            with open(src, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            try:
                self.am.publish(self.output_root, src, self.project, SAVE_CATEGORY,
                                name=name, subdir=REQUEST_SUBDIR)
            except self.am.ToolError as ex:
                raise StudioError(500, ex.code, ex.message)
        return os.path.join(self.output_root, self.project, SAVE_CATEGORY, *REQUEST_SUBDIR.split("/"), name)

    def _remember(self, rid, result):
        self.ai_results[rid] = result
        while len(self.ai_results) > 200:
            self.ai_results.pop(next(iter(self.ai_results)))

    def export_request(self, req):
        """外部AIへ送らず、確認済みの編集・添付・依頼文をローカルへ保存する。"""
        if not isinstance(req, dict):
            raise StudioError(400, "bad_payload", "依頼の形式が不正です")
        prompt = req.get("prompt")
        if not (isinstance(prompt, str) and prompt.strip() and len(prompt) <= MAX_PROMPT):
            raise StudioError(400, "bad_payload", "依頼文は1〜%d文字で入力してください" % MAX_PROMPT)
        clean = validate_payload(req.get("payload"), self.project)
        atts = self._resolve_attachments(req.get("attachments", []))
        if not self.ai_lock.acquire(blocking=False):
            raise StudioError(429, "busy", "保存処理中です")
        try:
            path = self.save(req["payload"])["path"]
            request_file = self._write_request(clean["screen"], prompt, path, len(clean["edits"]), atts)
            return {"ok": True, "state": "exported", "saved_path": path, "request_file": request_file}
        finally:
            self.ai_lock.release()

    def ai_send(self, req):
        """編集JSONを保存し、依頼文ファイルを作って、固定した送信先へ『送信登録』する。
        相手の受領・完了は分からない。曖昧な結果は自動再送しない。"""
        b = self._require_bridge()
        if not isinstance(req, dict):
            raise StudioError(400, "bad_payload", "依頼の形式が不正です")
        rid, prompt = req.get("request_id"), req.get("prompt")
        if not (isinstance(rid, str) and REQUEST_ID_RE.match(rid)):
            raise StudioError(400, "bad_payload", "request_id が不正です")
        if not (isinstance(prompt, str) and prompt.strip() and len(prompt) <= MAX_PROMPT):
            raise StudioError(400, "bad_payload", "依頼文は1〜%d文字で入力してください" % MAX_PROMPT)
        prev = self.ai_results.get(rid)
        if prev is not None:      # 同じ送信の再実行は何もしない(二重送信防止)
            return dict(prev, duplicate=True)
        clean = validate_payload(req.get("payload"), self.project)
        atts = self._resolve_attachments(req.get("attachments", []))
        if not self.ai_lock.acquire(blocking=False):
            raise StudioError(429, "busy", "送信処理中です。終わるまでお待ちください")
        try:
            prev = self.ai_results.get(rid)
            if prev is not None:
                return dict(prev, duplicate=True)
            v = b.verify(self.port)
            if not v["ok"]:
                raise StudioError(409, "ai_unbound", " / ".join(v["reasons"]))
            # 編集が空でも「現在の状態」を保存する(元に戻した結果も正本にする。旧版は一時ゴミ箱へ隔離される)
            json_path = self.save(req["payload"])["path"]
            extra = {"saved_path": json_path}
            try:
                req_path = self._write_request(clean["screen"], prompt, json_path, len(clean["edits"]), atts)
            except StudioError as ex:
                ex.extra.update(extra)
                raise
            extra["request_file"] = req_path
            safe = screen_safe_name(clean["screen"]["path"])
            subject = "画面編集室の依頼: %s %s" % (self.project, safe)
            body = ("画面編集室からの依頼です。依頼文: %s / 編集JSON: %s / 添付画像: %d枚。"
                    "見た目の編集は製品コード未反映です。依頼文ファイルを読んで進めてください。"
                    % (req_path, json_path or "なし", len(atts)))
            payload = {"kind": "app-screen-studio-request", "project": self.project,
                       "screen": clean["screen"]["path"], "request_file": req_path,
                       "edit_json": json_path, "images": [{"kind": a["kind"], "path": a["path"]} for a in atts]}
            try:
                sent = b.send(subject, body, payload)
            except orca_bridge.BridgeError as ex:
                fail = dict(ok=False, code=ex.code, message=ex.message, ambiguous=ex.ambiguous, **extra)
                self._remember(rid, fail)
                raise StudioError(502, ex.code, ex.message, dict(ambiguous=ex.ambiguous, **extra))
            res = dict(ok=True, state="registered", message_id=sent["message_id"],
                       note="送信登録のみ。相手の受領・完了は未確認です。", **extra)
            self._remember(rid, res)
            return res
        finally:
            self.ai_lock.release()

    def load(self, screen_path):
        name = self.screen_file(screen_path)
        p = os.path.join(self.output_root, self.project, SAVE_CATEGORY, SAVE_SUBDIR, name)
        try:
            p = self.checked_output_file(p)
            if os.path.islink(p) or not os.path.isfile(p) or os.path.getsize(p) > 4 * MAX_BODY:
                return {"exists": False}
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            clean = validate_payload(data, self.project)
        except (OSError, ValueError, StudioError):
            return {"exists": False}
        return {"exists": True, "path": p, "data": clean}


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ScreenStudio"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):   # トークン等を含み得るクエリは出さない
            method = getattr(self, "command", "")
            path = getattr(self, "path", "")
            if method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS") or not path.startswith("/"):
                line = "不正なリクエストを拒否"
            else:
                line = method + " " + path.split("?")[0]
            sys.stderr.write("[studio] %s\n" % line)

        # ---- 共通
        def _host_ok(self):
            return (self.headers.get("Host") or "").lower() in state.allowed_hosts()

        def _send(self, status, body=b"", ctype="application/json; charset=utf-8",
                  extra=None, head=False):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if self.close_connection:
                self.send_header("Connection", "close")
            for k, v in (extra or []):
                self.send_header(k, v)
            self.end_headers()
            if not head and self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def _err(self, ex):
            # 拒否したPOST本文を次のHTTP要求として解釈・記録しない。
            self.close_connection = True
            if not getattr(self, "_body_read", False):
                try:
                    remaining = int(self.headers.get("Content-Length") or 0)
                    if 0 < remaining <= MAX_ASSET + 65536:
                        deadline = time.monotonic() + 0.5
                        previous_timeout = self.connection.gettimeout()
                        try:
                            while remaining and time.monotonic() < deadline:
                                self.connection.settimeout(max(0.01, deadline - time.monotonic()))
                                part = self.rfile.read1(min(65536, remaining))
                                if not part:
                                    break
                                remaining -= len(part)
                        finally:
                            self.connection.settimeout(previous_timeout)
                except (OSError, ValueError, AttributeError):
                    pass
            self._json(ex.status, dict(ex.extra, ok=False, code=ex.code, message=ex.message))

        def _json_body(self, limit):
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise StudioError(415, "bad_type", "Content-Type は application/json")
            try:
                n = int(self.headers.get("Content-Length") or "")
            except ValueError:
                raise StudioError(411, "length", "Content-Length が必要")
            if n < 0 or n > limit:
                raise StudioError(413, "too_large", "データが大きすぎます")
            raw = self._read_body(n)
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise StudioError(400, "bad_json", "JSONとして読めません")

        def _read_body(self, size):
            data = self.rfile.read(size)
            self._body_read = True
            return data

        # ---- ルーティング
        def do_HEAD(self):
            self._route()

        def do_GET(self):
            self._route()

        def do_POST(self):
            self._route()

        def do_PUT(self):
            self._route()

        def do_DELETE(self):
            self._route()

        def do_PATCH(self):
            self._route()

        def do_OPTIONS(self):
            self._send(405, b"", "text/plain")

        def _route(self):
            self._body_read = False
            try:
                if not self._host_ok():
                    raise StudioError(403, "bad_host", "Host が許可されていません")
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                if path.startswith(PREFIX + "/") or path == PREFIX:
                    return self._studio(path, parsed)
                if state.mode == "url":
                    return self._proxy()
                if self.command not in ("GET", "HEAD"):
                    raise StudioError(405, "method", "method not allowed")
                if path == "/":
                    return self._send(302, b"", "text/plain", [("Location", state.entry)])
                return self._static(path)
            except StudioError as ex:
                self._err(ex)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as ex:  # 想定外は詳細を出さない
                sys.stderr.write("[studio] internal error: %s\n" % ex.__class__.__name__)
                try:
                    self._json(500, {"ok": False, "code": "internal", "message": "内部エラー"})
                except Exception:
                    pass

        def _shell_url(self, path):
            return PREFIX + "/shell?path=" + urllib.parse.quote(path, safe="")

        # ---- 編集室自身のエンドポイント
        def _studio(self, path, parsed):
            sub = path[len(PREFIX):]
            if sub in ("/editor.js", "/editor.css", "/shell.js", "/shell.css"):
                if self.command not in ("GET", "HEAD"):
                    raise StudioError(405, "method", "method not allowed")
                fn = sub[1:]
                with open(os.path.join(STATIC_DIR, fn), "rb") as f:
                    body = f.read()
                ctype = "text/javascript; charset=utf-8" if fn.endswith(".js") \
                    else "text/css; charset=utf-8"
                return self._send(200, body, ctype)
            if sub == "/shell":
                if self.command not in ("GET", "HEAD"):
                    raise StudioError(405, "method", "method not allowed")
                with open(os.path.join(STATIC_DIR, "shell.html"), "rb") as f:
                    body = f.read()
                csp = ("default-src 'none'; script-src 'self'; style-src 'self'; "
                       "img-src 'self' data:; connect-src 'self'; frame-src 'self'; "
                       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
                return self._send(200, body, "text/html; charset=utf-8",
                                  [("Content-Security-Policy", csp)])
            if sub == "/api/session":
                self._check_fetch_site()
                return self._json(200, {"ok": True, "project": state.project,
                                        "token": state.token, "entry": state.entry,
                                        "mode": state.mode, "ai": state.ai_summary(),
                                        "capabilities": {"material_presets": True, "material_colors": True, "text_glow": True}})
            if sub == "/api/ai/status":
                self._check_fetch_site()
                self._check_token()
                return self._json(200, {"ok": True, "ai": state.ai_status()})
            if sub == "/api/ai/screenshot":
                if self.command != "POST":
                    raise StudioError(405, "method", "method not allowed")
                self._check_origin()
                self._check_token()
                return self._json(200, dict(ok=True, **state.screenshot(self._json_body(4096))))
            if sub == "/api/ai/attach":
                if self.command != "POST":
                    raise StudioError(405, "method", "method not allowed")
                self._check_origin()
                self._check_token()
                try:
                    n = int(self.headers.get("Content-Length") or "")
                except ValueError:
                    raise StudioError(411, "length", "Content-Length が必要")
                if n <= 0 or n > MAX_ASSET:
                    raise StudioError(413, "too_large", "画像は5MBまでです")
                q = urllib.parse.parse_qs(parsed.query)
                res = state.attach((q.get("kind") or [""])[0], (q.get("name") or ["image"])[0][:200],
                                   self._read_body(n))
                return self._json(200, dict(ok=True, **res))
            if sub == "/api/ai/export":
                if self.command != "POST":
                    raise StudioError(405, "method", "method not allowed")
                self._check_origin()
                self._check_token()
                return self._json(200, state.export_request(self._json_body(MAX_BODY + 65536)))
            if sub == "/api/ai/send":
                if self.command != "POST":
                    raise StudioError(405, "method", "method not allowed")
                self._check_origin()
                self._check_token()
                return self._json(200, state.ai_send(self._json_body(MAX_BODY + 65536)))
            if sub == "/api/load":
                self._check_fetch_site()
                self._check_token()
                q = urllib.parse.parse_qs(parsed.query)
                sp = (q.get("path") or [""])[0]
                if not sp.startswith("/") or len(sp) > 300:
                    raise StudioError(400, "bad_payload", "path が不正")
                return self._json(200, dict(ok=True, **state.load(sp)))
            if sub.startswith("/assets/"):
                if self.command not in ("GET", "HEAD"):
                    raise StudioError(405, "method", "method not allowed")
                data, ctype = state.read_asset(urllib.parse.unquote(sub[len("/assets/"):]))
                return self._send(200, data, ctype)
            if sub == "/api/asset":
                if self.command != "POST":
                    raise StudioError(405, "method", "method not allowed")
                self._check_origin()
                self._check_token()
                try:
                    n = int(self.headers.get("Content-Length") or "")
                except ValueError:
                    raise StudioError(411, "length", "Content-Length が必要")
                if n <= 0 or n > MAX_ASSET:
                    raise StudioError(413, "too_large", "画像は5MBまでです")
                name = urllib.parse.parse_qs(parsed.query).get("name", ["image"])[0][:200]
                res = state.save_asset(name, self._read_body(n))
                return self._json(200, dict(ok=True, **res))
            if sub == "/api/save":
                if self.command != "POST":
                    raise StudioError(405, "method", "method not allowed")
                self._check_origin()
                self._check_token()
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    raise StudioError(415, "bad_type", "Content-Type は application/json")
                try:
                    n = int(self.headers.get("Content-Length") or "")
                except ValueError:
                    raise StudioError(411, "length", "Content-Length が必要")
                if n < 0 or n > MAX_BODY:
                    raise StudioError(413, "too_large", "保存データが大きすぎます")
                raw = self._read_body(n)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise StudioError(400, "bad_json", "JSONとして読めません")
                res = state.save(payload)
                return self._json(200, {"ok": True, "status": res.get("status"),
                                        "path": res["path"],
                                        "destination": res.get("destination"),
                                        "trash_id": res.get("trash_id")})
            raise StudioError(404, "not_found", "not found")

        def _check_fetch_site(self):
            site = self.headers.get("Sec-Fetch-Site")
            if site is not None and site not in ("same-origin", "none"):
                raise StudioError(403, "cross_site", "別サイトからは使えません")
            origin = self.headers.get("Origin")
            if origin is not None and origin not in state.allowed_origins():
                raise StudioError(403, "bad_origin", "Origin が許可されていません")

        def _check_origin(self):
            origin = self.headers.get("Origin")
            if origin not in state.allowed_origins():   # 無い場合も拒否
                raise StudioError(403, "bad_origin", "Origin が許可されていません")
            self._check_fetch_site()

        def _check_token(self):
            got = self.headers.get("X-Studio-Token") or ""
            if not hmac.compare_digest(got.encode("utf-8"), state.token.encode("utf-8")):
                raise StudioError(403, "bad_token", "トークンが一致しません")

        # ---- 静的配信
        def _static(self, url_path):
            real, ctype = safe_static_path(state.root, url_path)
            try:
                with open(real, "rb") as f:
                    body = f.read()
            except OSError:
                raise StudioError(404, "not_found", "not found")
            if ctype == "text/html":
                body = inject(body)
            if ctype.startswith("text/") or ctype in ("application/json",
                                                       "application/manifest+json"):
                ctype += "; charset=utf-8"
            self._send(200, body, ctype)

        # ---- 既存devサーバーへのプロキシ
        def _proxy(self):
            scheme, host, port = state.upstream
            body = None
            if self.command in ("POST", "PUT", "PATCH", "DELETE"):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    raise StudioError(411, "length", "Content-Length が不正")
                if n < 0 or n > MAX_PROXY_BODY:
                    raise StudioError(413, "too_large", "too large")
                body = self._read_body(n) if n else None
            hdrs = {}
            for k, v in self.headers.items():
                if k.lower() in ("host", "connection", "accept-encoding", "content-length",
                                 "origin", "referer", "keep-alive", "upgrade",
                                 "proxy-connection", "te", "transfer-encoding"):
                    continue
                hdrs[k] = v
            hdrs["Accept-Encoding"] = "identity"
            hdrs["Host"] = f"{host}:{port}"
            cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            conn = cls(host, port, timeout=30)
            try:
                conn.request(self.command, self.path, body=body, headers=hdrs)
                resp = conn.getresponse()
                data = resp.read(MAX_PROXY_BODY + 1)
                if len(data) > MAX_PROXY_BODY:
                    raise StudioError(502, "too_large", "upstream response too large")
                ctype = resp.getheader("Content-Type") or "application/octet-stream"
                out_headers = []
                for k, v in resp.getheaders():
                    kl = k.lower()
                    if kl in ("content-length", "transfer-encoding", "connection", "keep-alive",
                              "content-encoding", "x-frame-options", "content-type",
                              "cache-control", "upgrade"):
                        continue
                    if kl == "content-security-policy":   # frame-ancestors だけ外す
                        v = re.sub(r"(^|;)\s*frame-ancestors[^;]*", r"\1", v).strip(" ;")
                        if not v:
                            continue
                    if kl == "location":
                        v = re.sub(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?", "", v)
                    out_headers.append((k, v))
                if ctype.lower().startswith("text/html"):
                    data = inject(data)
                self._send(resp.status, data, ctype, out_headers)
            except StudioError:
                raise
            except (OSError, http.client.HTTPException):
                raise StudioError(502, "upstream", "接続先のdevサーバーに届きません")
            finally:
                conn.close()

    return Handler


_HEAD_RE = re.compile(rb"</head\s*>", re.I)
_BODY_RE = re.compile(rb"</body\s*>", re.I)
LINK_TAG = ('<link rel="stylesheet" href="%s/editor.css">' % PREFIX).encode()
SCRIPT_TAG = ('<script src="%s/editor.js" defer></script>' % PREFIX).encode()


def inject(html):
    """外部 link / script だけを挿入する(inline は使わない)。"""
    if b"/__screen_studio__/editor.js" in html:
        return html
    m = _HEAD_RE.search(html)
    html = html[:m.start()] + LINK_TAG + html[m.start():] if m else LINK_TAG + html
    m = None
    for m in _BODY_RE.finditer(html):
        pass
    if m:
        return html[:m.start()] + SCRIPT_TAG + html[m.start():]
    return html + SCRIPT_TAG


def create_server(state, port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    srv.daemon_threads = True
    state.port = srv.server_address[1]
    return srv


def build_bridge(args):
    """Orca連携の起動設定。1つも指定が無ければ None(送信・撮影は無効)。"""
    names = ("orca_worktree", "orca_page", "orca_open", "ai_target", "ai_target_worktree", "ai_from")
    if not any(getattr(args, n, None) for n in names):
        return None
    g = lambda n: getattr(args, n, None)
    target = g("ai_target")
    if target == "self":      # 起動したAI自身(環境変数 ORCA_AGENT_SESSION_ID)
        sid = os.environ.get(orca_bridge.SESSION_ENV, "")
        target = "orca_session_id:" + sid if sid else None
        if not target:
            raise StudioError(400, "args", "--ai-target self には ORCA_AGENT_SESSION_ID が必要です")
    try:
        return orca_bridge.OrcaBridge(worktree=g("orca_worktree"), page=g("orca_page"),
                                      target=target, target_worktree=g("ai_target_worktree"),
                                      sender=g("ai_from"), open_tab=g("orca_open"))
    except orca_bridge.BridgeError as ex:
        raise StudioError(400, "args", ex.message)


def build_state(args):
    if args.dir and args.url:
        raise StudioError(400, "args", "--dir と --url は同時に指定できません")
    entry = args.entry or ("/" if args.url else "/index.html")
    if not entry.startswith("/") or ".." in entry.split("/") or "\\" in entry:
        raise StudioError(400, "args", "--entry は / で始まるURLパスにしてください")
    project = args.project or "demo"
    out = args.output_root or DEFAULT_OUTPUT_ROOT
    if args.url:
        return StudioState(project, "url", upstream=parse_upstream(args.url), entry=entry,
                           output_root=out, bridge=build_bridge(args))
    root = args.dir or DEMO_DIR
    if not os.path.isdir(root):
        raise StudioError(400, "args", "--dir がディレクトリではありません")
    if not args.dir:
        entry = "/index.html"
    return StudioState(project, "dir", root=root, entry=entry, output_root=out,
                       bridge=build_bridge(args))


def main(argv=None):
    ap = argparse.ArgumentParser(description="アプリ画面編集室(ローカル専用)")
    ap.add_argument("--project", help="プロジェクト名(保存先フォルダ名。デモは省略可)")
    ap.add_argument("--dir", help="表示する画面ソースのフォルダ(読むだけ)")
    ap.add_argument("--url", help="既存devサーバーのURL(127.0.0.1/localhostのみ)")
    ap.add_argument("--entry", help="最初に開く画面のパス(--dirの既定 /index.html、--urlの既定 /)")
    ap.add_argument("--port", type=int, default=4780)
    ap.add_argument("--output-root", help="成果物ルート(テスト用に変更可)")
    ap.add_argument("--orca-worktree", help="開発対象のOrca workspaceのパス(AI送信・撮影の固定先)")
    ap.add_argument("--orca-page", help="編集タブのpage id(orca tab list --json の browserPageId)")
    ap.add_argument("--orca-open", action="store_true",
                    help="起動時に --orca-worktree のworkspaceへ編集タブを作り、そのpage idを固定する")
    ap.add_argument("--ai-target", help="送信先session(orca_session_id:... または self=起動したAI自身)")
    ap.add_argument("--ai-target-worktree", help="送信先sessionがいるworkspace(既定は --orca-worktree)")
    ap.add_argument("--ai-from", help="送信元handle(省略時はOrca CLIの既定)")
    args = ap.parse_args(argv)
    try:
        if not (args.dir or args.url) and not args.project:
            args.project = "demo"
        if (args.dir or args.url) and not args.project:
            raise StudioError(400, "args", "--project が必要です")
        state = build_state(args)
        srv = create_server(state, args.port)
    except StudioError as ex:
        print("エラー: " + ex.message, file=sys.stderr)
        return 2
    except OSError as ex:
        print("エラー: ポートを開けません(%s)。--port を変えてください" % ex.__class__.__name__,
              file=sys.stderr)
        return 2
    except Exception as ex:
        if ex.__class__.__name__ == "ToolError":
            print("エラー: " + getattr(ex, "message", str(ex)), file=sys.stderr)
            return 2
        raise
    url = "http://127.0.0.1:%d%s/shell?path=%s" % (
        state.port, PREFIX, urllib.parse.quote(state.entry, safe=""))
    print("画面編集室を起動しました (終了: Ctrl+C)")
    print("  プロジェクト: " + state.project)
    print("  開くURL: " + url)
    print("  保存先: " + os.path.join(state.output_root, state.project, SAVE_CATEGORY, SAVE_SUBDIR))
    b = state.bridge
    if b is None or b.missing():
        miss = (b or orca_bridge.OrcaBridge()).missing()
        print("  AI送信・撮影: 無効(足りない設定: " + " / ".join(miss) + ")")
        print("    有効にする例: python studio.py ... --orca-worktree <workspaceのパス> --orca-open --ai-target self")
    else:
        s = b.summary()
        print("  AI送信・撮影: 有効 (workspace=%s, 送信先=%s, タブ=%s)" % (
            s["worktree"], s["target"], s["tab"] or "起動後に作成"))
    sys.stdout.flush()
    opener = None
    if b is not None and b.want_open and not b.page:
        def _open():
            try:
                b.open_tab(url)
                print("  編集タブを作成しました(…%s)" % b.page[-6:])
            except orca_bridge.BridgeError as ex:
                print("  編集タブを作れませんでした: " + ex.message)
            sys.stdout.flush()
        opener = threading.Timer(0.3, _open)     # 一度だけ実行(常駐しない)
        opener.start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")
    finally:
        srv.server_close()
        # 本人の入力や未保存編集を守るため、停止時も編集タブは残す。
        if b is not None and b.created_tab:
            print("  編集タブは保持しました。確認後に閉じられます")
    return 0


if __name__ == "__main__":
    sys.exit(main())
