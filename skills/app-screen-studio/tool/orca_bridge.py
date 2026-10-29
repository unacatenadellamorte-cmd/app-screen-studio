"""画面編集室 ⇔ Orca CLI の連携(撮影・AIへの送信登録)。標準ライブラリのみ。

方針:
- Orca の正式 CLI(`orca.exe` を引数配列で実行)だけを使う。シェルは経由しない。DBの書換えやバンドルへのパッチはしない。
- 宛先 workspace / タブ / 送信先 session は **起動時に明示された値だけ** を使う。リクエストからは受け取らない。
- 送信は `orca orchestration send`(送信登録のみ)。相手の受領・完了は分からないので、そう扱う。
  返ってきた to_handle が指定宛先と違えば(別窓口へ正規化された恐れ)、成功扱いにしない。
- プロンプト・画像・認証値はログ/例外メッセージに出さない。
"""
import base64
import json
import os
import re
import shutil
import subprocess
import urllib.parse

HANDLE_RE = re.compile(r"^orca_session_id:[A-Za-z0-9_.-]{1,120}$")
PAGE_RE = re.compile(r"^[0-9A-Za-z-]{8,64}$")
SESSION_ENV = "ORCA_AGENT_SESSION_ID"
CLI_ENV = "ORCA_CLI_COMMAND"
MAX_CLI_OUTPUT = 60 * 1024 * 1024


class BridgeError(Exception):
    """想定内の失敗。message は利用者に見せてよい(秘密・本文を含めない)。"""

    def __init__(self, code, message, ambiguous=False):
        super().__init__(message)
        self.code, self.message, self.ambiguous = code, message, ambiguous


def norm_path(p):
    p = os.path.abspath(str(p)).replace("\\", "/").rstrip("/")
    return p.casefold()


def find_cli(env=None):
    """Orca CLI の実行ファイル(.exe)。.cmd/.bat は引数がシェルに解釈されるので使わない。"""
    env = os.environ if env is None else env
    cand = env.get(CLI_ENV)
    if cand and os.path.isfile(cand) and cand.lower().endswith(".exe"):
        return cand
    for name in ("orca.exe", "orca"):
        found = shutil.which(name)
        if found and found.lower().endswith(".exe"):
            return found
    return None


def default_runner(cli, args, timeout):
    """(returncode, stdout文字列)。shell=False・stdin無し・出力サイズ上限あり。"""
    proc = subprocess.run([cli] + list(args), stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=timeout, shell=False)
    out = proc.stdout[:MAX_CLI_OUTPUT].decode("utf-8", errors="replace")
    return proc.returncode, out


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], str):
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r:
                return r
    return None


class OrcaBridge:
    def __init__(self, worktree=None, page=None, target=None, target_worktree=None,
                 sender=None, open_tab=False, cli=None, runner=None):
        self.worktree = os.path.abspath(worktree).replace("\\", "/") if worktree else None
        self.page = page
        self.target = target
        self.target_worktree = (os.path.abspath(target_worktree).replace("\\", "/")
                                if target_worktree else self.worktree)
        self.sender = sender
        self.want_open = bool(open_tab)
        self.cli = cli if cli is not None else find_cli()
        self.runner = runner or default_runner
        self.created_tab = False
        self._validate()

    # ---- 起動設定の検査(リクエストでは変更できない)
    def _validate(self):
        if self.worktree and not os.path.isdir(self.worktree):
            raise BridgeError("bad_config", "--orca-worktree がフォルダではありません")
        if self.page and not PAGE_RE.match(self.page):
            raise BridgeError("bad_config", "--orca-page の形式が不正です")
        if self.target and not HANDLE_RE.match(self.target):
            raise BridgeError("bad_config", "--ai-target は orca_session_id:... の形式にしてください")
        if self.sender and not HANDLE_RE.match(self.sender):
            raise BridgeError("bad_config", "--ai-from は orca_session_id:... の形式にしてください")
        if self.page and self.want_open:
            raise BridgeError("bad_config", "--orca-page と --orca-open は同時に指定できません")

    def missing(self):
        """送信・撮影に足りない起動設定の一覧(空なら有効化できる)。"""
        out = []
        if not self.cli:
            out.append("Orca CLI(orca.exe)が見つかりません(ORCA_CLI_COMMAND または PATH)")
        if not self.worktree:
            out.append("--orca-worktree <開発対象のworkspaceのパス>")
        if not self.page and not self.want_open:
            out.append("--orca-page <編集タブのpage id> または --orca-open")
        if not self.target:
            out.append("--ai-target <送信先のsession(orca_session_id:... または self)>")
        return out

    def configured(self):
        return not self.missing()

    def summary(self):
        """画面に見せてよい設定の要約(token等は含まない)。"""
        return {
            "configured": self.configured(), "missing": self.missing(),
            "worktree": os.path.basename(self.worktree) if self.worktree else "",
            "target": ("…" + self.target[-8:]) if self.target else "",
            "tab": ("…" + self.page[-6:]) if self.page else "",
        }

    # ---- CLI 実行
    def _sel(self, path):
        return "path:" + path

    def _json(self, args, timeout=20, ambiguous_on_timeout=False):
        try:
            rc, out = self.runner(self.cli, args, timeout)
        except subprocess.TimeoutExpired:
            raise BridgeError("cli_timeout", "Orca CLI の応答待ちが時間切れになりました",
                              ambiguous=ambiguous_on_timeout)
        except OSError:
            raise BridgeError("cli_missing", "Orca CLI を実行できません")
        try:
            data = json.loads(out)
        except ValueError:
            raise BridgeError("cli_output", "Orca CLI の出力を読めませんでした",
                              ambiguous=ambiguous_on_timeout and rc == 0)
        if rc != 0 or not isinstance(data, dict) or not data.get("ok"):
            err = data.get("error") if isinstance(data, dict) else None
            code = (err or {}).get("code") if isinstance(err, dict) else None
            raise BridgeError("cli_failed", "Orca CLI がエラーを返しました(%s)" % (code or rc))
        return data.get("result") or {}

    def _page_json(self, args, timeout=20):
        """ページIDで対象を固定し、folder指定を扱えないCLIだけselector無しで照合する。"""
        try:
            return self._json(args, timeout=timeout)
        except BridgeError as ex:
            if "selector_not_found" not in ex.message or "--page" not in args or "--worktree" not in args:
                raise
            retry = list(args)
            pos = retry.index("--worktree")
            del retry[pos:pos + 2]
            return self._json(retry, timeout=timeout)

    # ---- 編集タブ
    def open_tab(self, url):
        """--orca-open: 指定 workspace に編集タブを作り、そのpage idを固定する。"""
        if not self.want_open or self.page:
            return self.page
        res = self._json(["tab", "create", "--url", url, "--worktree", self._sel(self.worktree), "--json"])
        pid = _find_key(res, "browserPageId")
        if not pid or not PAGE_RE.match(pid):
            raise BridgeError("tab_create", "編集タブのpage idを取得できませんでした")
        self.page, self.created_tab = pid, True
        return pid

    def close_tab(self):
        """自分が作ったタブだけ閉じる(本人・他AIのタブは触らない)。"""
        if not (self.created_tab and self.page and self.cli):
            return False
        try:
            self._json(["tab", "close", "--page", self.page, "--worktree", self._sel(self.worktree), "--json"], timeout=10)
        except BridgeError:
            return False
        self.created_tab = False
        return True

    # ---- 紐づけの確認(違う workspace / ページ / 宛先には送らない)
    def _ps(self):
        res = self._json(["worktree", "ps", "--limit", "100", "--json"], timeout=30)
        items = res.get("worktrees")
        return items if isinstance(items, list) else []

    def check_tab(self, port, ps=None):
        """編集タブが 指定page・指定workspace・この編集室のshell であることを確認。問題の一覧を返す。
        git workspace の worktreeId は '<repo>::<path>'、folder workspace は 'folder:<uuid>'。
        どちらも `worktree ps` の path との対応で、指定workspaceのidかを判定する。"""
        if self.missing():
            return ["起動設定が足りません"]
        reasons = []
        try:
            ps = self._ps() if ps is None else ps
            ids = set(str(w.get("worktreeId")) for w in ps
                      if norm_path(w.get("path") or "") == norm_path(self.worktree) and w.get("worktreeId"))
            res = self._page_json(["tab", "show", "--page", self.page, "--worktree", self._sel(self.worktree), "--json"])
            tab = res.get("tab") if isinstance(res.get("tab"), dict) else res
            wid = str(tab.get("worktreeId") or "")
            by_path = wid.split("::", 1)[1] if "::" in wid else None
            if not (wid in ids or (by_path is not None and norm_path(by_path) == norm_path(self.worktree))):
                reasons.append("編集タブが指定のworkspaceにありません")
            url = str(tab.get("url") or "")
            try:
                parsed = urllib.parse.urlsplit(url)
                matches = (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
                           and parsed.port == port and parsed.path == "/__screen_studio__/shell"
                           and parsed.username is None and parsed.password is None)
            except ValueError:
                matches = False
            if not matches:
                reasons.append("編集タブがこの編集室を開いていません")
        except BridgeError as ex:
            reasons.append("編集タブを確認できません: " + ex.message)
        return reasons

    def check_target(self, ps=None):
        """送信先sessionが指定workspace(既定は同じworkspace)で動いていることを確認。
        paneKey は 'structured-agent-session-<id>:' の完全prefix一致だけを採用する(部分一致は使わない)。"""
        if self.missing():
            return ["起動設定が足りません"]
        try:
            ps = self._ps() if ps is None else ps
        except BridgeError as ex:
            return ["送信先sessionを確認できません: " + ex.message]
        prefix = "structured-agent-session-%s:" % self.target.split(":", 1)[1]
        found = [w for w in ps for a in (w.get("agents") or [])
                 if str(a.get("paneKey") or "").startswith(prefix)]
        if not found:
            return ["送信先sessionが見つかりません(終了済みの可能性)"]
        if set(norm_path(w.get("path") or "") for w in found) != {norm_path(self.target_worktree)}:
            return ["送信先sessionが指定のworkspaceと一致しません"]
        return []

    def verify(self, port):
        if self.missing():
            return {"ok": False, "reasons": ["起動設定が足りません"], "missing": self.missing()}
        try:
            ps = self._ps()
        except BridgeError as ex:
            return {"ok": False, "reasons": ["workspace一覧を確認できません: " + ex.message], "missing": []}
        reasons = self.check_tab(port, ps) + self.check_target(ps)
        return {"ok": not reasons, "reasons": reasons, "missing": []}

    # ---- 撮影(固定したタブだけ)
    def capture(self):
        res = self._page_json(["screenshot", "--page", self.page, "--worktree", self._sel(self.worktree),
                          "--format", "png", "--json"], timeout=40)
        data = res.get("data")
        if not isinstance(data, str) or len(data) > 80 * 1024 * 1024:
            raise BridgeError("shot_failed", "撮影結果を受け取れませんでした")
        try:
            png = base64.b64decode(data, validate=True)
        except ValueError:
            raise BridgeError("shot_failed", "撮影結果を読めませんでした")
        if png[:8] != b"\x89PNG\r\n\x1a\n":
            raise BridgeError("shot_failed", "撮影結果がPNGではありません")
        return png

    # ---- 送信登録(受領・完了ではない)
    def send(self, subject, body, payload):
        args = ["orchestration", "send", "--to", self.target, "--subject", subject, "--body", body,
                "--type", "status", "--payload", json.dumps(payload, ensure_ascii=False), "--json"]
        if self.sender:
            args += ["--from", self.sender]
        res = self._json(args, timeout=25, ambiguous_on_timeout=True)
        msg = res.get("message") if isinstance(res.get("message"), dict) else {}
        mid = str(msg.get("id") or "")
        if not mid:
            raise BridgeError("send_unknown", "送信登録の結果を確認できませんでした", ambiguous=True)
        if msg.get("to_handle") != self.target:
            # 別窓口へ正規化されて登録された恐れ。成功扱いにしない(自動再送もしない)。
            raise BridgeError("misrouted", "宛先が指定と違う形で登録されました(" + mid + ")。送信先を確認してください",
                              ambiguous=True)
        return {"message_id": mid, "delivered": bool(msg.get("delivered_at"))}
