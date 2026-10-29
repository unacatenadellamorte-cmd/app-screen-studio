#!/usr/bin/env python3
"""成果物フォルダの集約・一時ゴミ箱管理ツール(標準ライブラリのみ)。

コマンド:
  publish  成果物を <root>/<project>/<category>/<name> へ移す(--copy でコピー)
  trash    root配下の実ファイルを一時ゴミ箱へ隔離する
  purge    入庫から7日経過した登録済みファイルを完全削除(既定はdry-run)
  restore  一時ゴミ箱のファイルを元の場所へ戻す
  list     一時ゴミ箱の登録一覧(読み取り専用)

設計方針:
  - 削除してよいのは manifest に登録済みで、期限経過・sha256一致・
    パス封じ込め確認済みの「実ファイル」だけ。未登録物・ディレクトリ・リンクは触らない。
  - 期限は元ファイルのmtimeではなく入庫日時(entered_at)から厳密に7日。
  - manifest が壊れていたら何もせず失敗(fail-closed)。
  - 排他ロック(msvcrt)+ manifest の原子的置換。データを失わない順序で操作する。
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
import uuid

try:
    import msvcrt  # Windows
except ImportError:  # pragma: no cover - 非Windows用の保険
    msvcrt = None
    import fcntl

DEFAULT_ROOT = os.path.abspath(os.environ.get("APP_SCREEN_STUDIO_OUTPUT_ROOT") or os.path.join(os.path.expanduser("~"), "AppScreenStudio", "成果物"))
TRASH_NAME = "一時ゴミ箱"
CATEGORIES = ("画像", "動画", "音声", "関連資料", "作業証跡")
RETENTION = dt.timedelta(days=7)
MANIFEST_NAME = "manifest.json"
LOCK_NAME = ".lock"
ITEMS_DIR = "items"
LOCK_TIMEOUT_SEC = 30

_INVALID_CHARS = set('<>:"/\\|?*')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)}
# シンボリックリンクとジャンクション(マウントポイント)のみリンク扱いにする。
# OneDriveのプレースホルダー等、他のreparse tagは通常ファイルとして扱う。
_LINK_REPARSE_TAGS = (0xA000000C, 0xA0000003)
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class ToolError(Exception):
    """想定内の拒否・失敗。code は機械可読な識別子。"""
    exit_code = 1

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class ManifestError(ToolError):
    """manifest 破損。fail-closed で何も変更せず終了する。"""
    exit_code = 2

    def __init__(self, message):
        super().__init__("manifest_corrupt", message)


# ---------------------------------------------------------------- 時刻・ハッシュ

def _now(now=None):
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)


def _fmt_ts(t):
    return t.astimezone(dt.timezone.utc).isoformat(timespec="microseconds")


def _parse_ts(s):
    try:
        t = dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        raise ManifestError(f"時刻形式が不正: {s!r}")
    if t.tzinfo is None or t.utcoffset() != dt.timedelta(0):
        raise ManifestError(f"UTC以外の時刻: {s!r}")
    return t


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- パス検査

def _st_is_link(st):
    if stat.S_ISLNK(st.st_mode):
        return True
    if getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return getattr(st, "st_reparse_tag", 0) in _LINK_REPARSE_TAGS
    return False


def _lstat_or_none(path):
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _is_regular_file(path):
    st = _lstat_or_none(path)
    return st is not None and stat.S_ISREG(st.st_mode) and not _st_is_link(st)


def check_name(name, label, top=False):
    """パス構成要素1つの安全性検査(Windows予約名・不正文字・末尾ドット等)。"""
    if not isinstance(name, str) or name in ("", ".", ".."):
        raise ToolError("bad_name", f"{label}: 空または . / .. は不可")
    if len(name) > 200:
        raise ToolError("bad_name", f"{label}: 長すぎる")
    if any(c in _INVALID_CHARS or ord(c) < 32 for c in name):
        raise ToolError("bad_name", f"{label}: 使えない文字を含む")
    if name[-1] in " .":
        raise ToolError("bad_name", f"{label}: 末尾の空白/ドットは不可")
    if name.split(".")[0].rstrip(" ").upper() in _RESERVED:
        raise ToolError("bad_name", f"{label}: Windows予約名は不可")
    if top and (name.startswith(".") or name.casefold() == TRASH_NAME.casefold()):
        raise ToolError("bad_name", f"{label}: 予約された名前は不可")


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _is_inside(path, base):
    try:
        return os.path.commonpath([_norm(path), _norm(base)]) == _norm(base)
    except ValueError:
        return False


def walk_no_links(base, parts):
    """base配下を順に辿り、リンクがあれば拒否。全要素が存在すればTrue、途中で無ければFalse。"""
    cur = base
    for i, p in enumerate(parts):
        cur = os.path.join(cur, p)
        st = _lstat_or_none(cur)
        if st is None:
            return False
        if _st_is_link(st):
            raise ToolError("link_rejected", "シンボリックリンク/ジャンクション経由は拒否")
        if i < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
            raise ToolError("not_a_directory", "途中の要素がディレクトリではない")
    return True


def check_root_path(root):
    """ルート自身と、その祖先のリンク経由も拒否する。"""
    absolute = os.path.abspath(root)
    drive, tail = os.path.splitdrive(absolute)
    walk_no_links(drive + os.sep, [p for p in tail.split(os.sep) if p])


def check_case_conflict(directory, name):
    """同じ親に、大小文字だけ違う別名があれば拒否。"""
    try:
        names = os.listdir(directory)
    except (FileNotFoundError, NotADirectoryError):
        return
    for n in names:
        if n != name and n.casefold() == name.casefold():
            raise ToolError("case_conflict", f"大小文字違いの同名が存在: {n}")


def ensure_dirs(root, parts):
    """リンク・大小文字衝突を確認しながらディレクトリを1段ずつ作成する。"""
    cur = root
    for p in parts:
        nxt = os.path.join(cur, p)
        st = _lstat_or_none(nxt)
        if st is None:
            check_case_conflict(cur, p)
            os.mkdir(nxt)
        else:
            if _st_is_link(st):
                raise ToolError("link_rejected", "リンク経由のディレクトリは拒否")
            if not stat.S_ISDIR(st.st_mode):
                raise ToolError("not_a_directory", "ディレクトリではない要素と衝突")
            check_case_conflict(cur, p)
        cur = nxt


def resolve_under_root(root, raw):
    """利用者指定パス(絶対/相対)をroot配下の構成要素リストへ。root外・..・ゴミ箱内は拒否。"""
    if not isinstance(raw, str) or not raw:
        raise ToolError("bad_path", "pathが空")
    if ".." in re.split(r"[\\/]+", raw):
        raise ToolError("path_traversal", "'..' を含むパスは拒否")
    root_abs = os.path.abspath(root)
    p = os.path.abspath(os.path.join(root_abs, raw))
    if _norm(p) == _norm(root_abs) or not _is_inside(p, root_abs):
        raise ToolError("outside_root", "root配下ではない")
    parts = os.path.relpath(p, root_abs).split(os.sep)
    if parts[0].casefold() == TRASH_NAME.casefold():
        raise ToolError("in_trash", "一時ゴミ箱内は対象外")
    # 実在する要素は実際の綴りへ正規化(大小文字違い指定の吸収)
    cur = root_abs
    for i, part in enumerate(parts):
        try:
            names = os.listdir(cur)
        except (FileNotFoundError, NotADirectoryError):
            break
        hit = [n for n in names if n.casefold() == part.casefold()]
        if len(hit) == 1:
            parts[i] = hit[0]
        cur = os.path.join(cur, parts[i])
    return root_abs, parts


# ---------------------------------------------------------------- manifest

def _validate_original(orig):
    if not isinstance(orig, str) or not orig or orig.startswith("/") or "\\" in orig:
        raise ManifestError("original が不正")
    parts = orig.split("/")
    for p in parts:
        if p in ("", ".", "..") or ":" in p:
            raise ManifestError("original に不正な要素")
    if parts[0].casefold() == TRASH_NAME.casefold():
        raise ManifestError("original がゴミ箱内を指している")
    return parts


def _validate_entry(e):
    if not isinstance(e, dict):
        raise ManifestError("item が辞書ではない")
    need = ("id", "state", "original", "stored", "sha256", "size", "reason",
            "entered_at", "deleted_at", "expires_at")
    for k in need:
        if k not in e:
            raise ManifestError(f"item に {k} が無い")
    try:
        if str(uuid.UUID(e["id"])) != e["id"]:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise ManifestError("id がUUIDではない")
    if e["state"] not in ("pending", "stored"):
        raise ManifestError("state が不正")
    orig_parts = _validate_original(e["original"])
    stored = e["stored"]
    if not isinstance(stored, str):
        raise ManifestError("stored が不正")
    sp = stored.split("/")
    if (len(sp) != 3 or sp[0] != ITEMS_DIR or sp[1] != e["id"]
            or sp[2] in ("", ".", "..") or ":" in sp[2] or "\\" in stored):
        raise ManifestError("stored が items/<id>/<name> 形式ではない")
    if sp[2] != orig_parts[-1]:
        raise ManifestError("stored と original のファイル名が不一致")
    if not isinstance(e["sha256"], str) or not _SHA_RE.match(e["sha256"]):
        raise ManifestError("sha256 が不正")
    if not isinstance(e["size"], int) or isinstance(e["size"], bool) or e["size"] < 0:
        raise ManifestError("size が不正")
    if not isinstance(e["reason"], str):
        raise ManifestError("reason が不正")
    entered = _parse_ts(e["entered_at"])
    _parse_ts(e["deleted_at"])
    if _parse_ts(e["expires_at"]) != entered + RETENTION:
        raise ManifestError("expires_at が entered_at+7日と一致しない")


def _validate_manifest(data):
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ManifestError("version が不正")
    items, purged = data.get("items"), data.get("purged")
    if not isinstance(items, list) or not isinstance(purged, list):
        raise ManifestError("items/purged が配列ではない")
    seen = set()
    for e in items:
        _validate_entry(e)
        if e["id"] in seen:
            raise ManifestError("id が重複")
        seen.add(e["id"])
    for p in purged:
        if not isinstance(p, dict):
            raise ManifestError("purged の要素が不正")


class Store:
    """排他ロック + manifest 読み書き。readonly=True は何も作らず・ロックもしない。"""

    def __init__(self, root, readonly=False):
        self.root = os.path.abspath(root)
        self.trash = os.path.join(self.root, TRASH_NAME)
        self.manifest_path = os.path.join(self.trash, MANIFEST_NAME)
        self.readonly = readonly
        self.items = []
        self.purged = []
        self._fd = None

    def __enter__(self):
        try:
            self._open()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _open(self):
        check_root_path(self.root)
        st = _lstat_or_none(self.trash)
        if st is not None and (_st_is_link(st) or not stat.S_ISDIR(st.st_mode)):
            raise ToolError("link_rejected", "一時ゴミ箱がディレクトリではない/リンク")
        if not self.readonly:
            os.makedirs(self.root, exist_ok=True)
            if st is None:
                os.mkdir(self.trash)
            self._lock()
        self._load()

    def _lock(self):
        lock_path = os.path.join(self.trash, LOCK_NAME)
        st = _lstat_or_none(lock_path)
        if st is not None and (_st_is_link(st) or not stat.S_ISREG(st.st_mode)):
            raise ToolError("link_rejected", "ロックファイルが実ファイルではない")
        self._fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
        deadline = time.monotonic() + LOCK_TIMEOUT_SEC
        while True:
            try:
                os.lseek(self._fd, 0, os.SEEK_SET)
                if msvcrt:
                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise ToolError("locked", "別プロセスがロック中(タイムアウト)")
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                os.lseek(self._fd, 0, os.SEEK_SET)
                if msvcrt:
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None
        return False

    def _load(self):
        st = _lstat_or_none(self.manifest_path)
        if st is None:
            return
        if _st_is_link(st) or not stat.S_ISREG(st.st_mode):
            raise ManifestError("manifest が通常ファイルではない")
        try:
            with open(self.manifest_path, "rb") as f:
                data = json.loads(f.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError, OSError) as ex:
            raise ManifestError(f"manifest を読めない: {ex.__class__.__name__}")
        _validate_manifest(data)
        self.items, self.purged = data["items"], data["purged"]
        self._reconcile()

    def _stored_path(self, e):
        return os.path.join(self.trash, *e["stored"].split("/"))

    def _reconcile(self):
        """中断された trash の後始末。ファイルは決して削除しない。"""
        changed = False
        for e in list(self.items):
            if e["state"] != "pending":
                continue
            sp = self._stored_path(e)
            walk_no_links(self.root, [TRASH_NAME] + e["stored"].split("/"))
            if _is_regular_file(sp) and sha256_file(sp) == e["sha256"]:
                e["state"] = "stored"
                changed = True
            elif _lstat_or_none(sp) is None and _lstat_or_none(
                    os.path.join(self.root, *e["original"].split("/"))) is not None:
                self.items.remove(e)  # 移動前に落ちた: 元ファイルは無事
                changed = True
        if changed and not self.readonly:
            self.save()

    def save(self):
        if self.readonly:
            raise RuntimeError("readonly store")
        data = {"version": 1, "items": self.items, "purged": self.purged}
        tmp = self.manifest_path + "." + uuid.uuid4().hex + ".tmp"
        with open(tmp, "xb") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.manifest_path)

    def find(self, item_id):
        for e in self.items:
            if e["id"] == item_id:
                return e
        raise ToolError("not_found", "指定IDの登録が無い")

    def stored_file_ok(self, e):
        """登録ファイルが封じ込め範囲内の実ファイルか。無ければNone、不適格なら例外。"""
        parts = [TRASH_NAME] + e["stored"].split("/")
        if not walk_no_links(self.root, parts):
            return None
        if not _is_regular_file(self._stored_path(e)):
            raise ToolError("not_regular_file", "登録ファイルが通常ファイルではない")
        return self._stored_path(e)

    def drop_item_dir(self, e):
        try:
            os.rmdir(os.path.dirname(self._stored_path(e)))  # 空のときだけ消える
        except OSError:
            pass


# ---------------------------------------------------------------- 操作本体

def _trash_item(store, parts, reason, now):
    """ロック保持中に呼ぶ。pending登録 → 移動 → stored確定の順(途中で落ちても紛失しない)。"""
    src = os.path.join(store.root, *parts)
    if not _is_regular_file(src):
        raise ToolError("not_regular_file", "実ファイル以外(ディレクトリ/リンク/不存在)は対象外")
    sha = sha256_file(src)
    item_id = str(uuid.uuid4())
    ts = _fmt_ts(now)
    entry = {
        "id": item_id, "state": "pending", "original": "/".join(parts),
        "stored": f"{ITEMS_DIR}/{item_id}/{parts[-1]}", "sha256": sha,
        "size": os.path.getsize(src), "reason": reason,
        "entered_at": ts, "deleted_at": ts, "expires_at": _fmt_ts(now + RETENTION),
    }
    store.items.append(entry)
    store.save()
    dest = store._stored_path(entry)
    item_dir = os.path.dirname(dest)
    try:
        ensure_dirs(store.root, [TRASH_NAME, ITEMS_DIR, item_id])
        os.rename(src, dest)  # Windowsでは宛先があれば失敗する(上書きしない)
        if sha256_file(dest) != sha:
            os.rename(dest, src)
            raise ToolError("verify_failed", "移動後のsha256が一致しない(元へ戻した)")
    except BaseException as ex:
        if _is_regular_file(src) and not os.path.lexists(dest):
            store.items.remove(entry)  # 元ファイルは無事
            store.save()
            store.drop_item_dir(entry)
        if isinstance(ex, (ToolError, KeyboardInterrupt)):
            raise
        raise ToolError("move_failed", f"隔離に失敗: {ex.__class__.__name__}")
    entry["state"] = "stored"
    store.save()
    return entry


def _public(e):
    return {"id": e["id"], "original": e["original"], "state": e["state"],
            "entered_at": e["entered_at"], "expires_at": e["expires_at"],
            "reason": e["reason"]}


def trash(root, path, reason, now=None):
    now = _now(now)
    root_abs, parts = resolve_under_root(root, path)
    for p in parts:
        check_name(p, "path")
    if not reason:
        raise ToolError("bad_reason", "reason が必要")
    with Store(root_abs) as store:
        if not walk_no_links(root_abs, parts):
            raise ToolError("not_found", "対象が存在しない")
        entry = _trash_item(store, parts, reason, now)
        return {"ok": True, "command": "trash", "item": _public(entry)}


def purge(root, apply=False, now=None):
    now = _now(now)
    results = []
    with Store(root, readonly=not apply) as store:
        for e in list(store.items):
            row = {"id": e["id"], "original": e["original"],
                   "entered_at": e["entered_at"], "expires_at": e["expires_at"]}
            results.append(row)
            if e["state"] != "stored":
                row["status"] = "skipped_pending"
                continue
            if now < _parse_ts(e["entered_at"]) + RETENTION:
                row["status"] = "not_due"
                continue
            try:
                path = store.stored_file_ok(e)
            except ToolError as ex:
                row["status"] = ex.code
                continue
            if path is None:
                row["status"] = "missing"
                if apply:
                    store.items.remove(e)
                    store.save()
                continue
            if sha256_file(path) != e["sha256"]:
                row["status"] = "sha_mismatch"  # 内容が変わっている: 削除しない
                continue
            if not apply:
                row["status"] = "would_delete"
                continue
            os.unlink(path)
            store.drop_item_dir(e)
            store.items.remove(e)
            store.purged.append({"id": e["id"], "original": e["original"],
                                 "sha256": e["sha256"], "entered_at": e["entered_at"],
                                 "purged_at": _fmt_ts(now)})
            store.save()
            row["status"] = "deleted"
    return {"ok": True, "command": "purge", "dry_run": not apply, "items": results}


def restore(root, item_id, now=None):
    with Store(root) as store:
        e = store.find(item_id)
        if e["state"] != "stored":
            raise ToolError("not_stored", "未確定(pending)の登録は戻せない")
        path = store.stored_file_ok(e)
        if path is None:
            raise ToolError("missing", "一時ゴミ箱内にファイルが無い")
        if sha256_file(path) != e["sha256"]:
            raise ToolError("sha_mismatch", "sha256が一致しないため戻さない")
        parts = e["original"].split("/")
        walk_no_links(store.root, parts[:-1])
        ensure_dirs(store.root, parts[:-1])
        dest = os.path.join(store.root, *parts)
        check_case_conflict(os.path.dirname(dest), parts[-1])
        if os.path.lexists(dest):
            raise ToolError("destination_exists", "元の場所が空いていないため戻さない")
        os.rename(path, dest)
        if sha256_file(dest) != e["sha256"]:
            raise ToolError("verify_failed", "戻した後のsha256が一致しない")
        store.items.remove(e)
        store.save()
        store.drop_item_dir(e)
        return {"ok": True, "command": "restore", "restored": e["original"]}


def list_items(root):
    with Store(root, readonly=True) as store:
        return {"ok": True, "command": "list", "items": [_public(e) for e in store.items]}


def publish(root, source, project, category, name=None, copy=False, now=None, subdir=None):
    now = _now(now)
    check_name(project, "project", top=True)
    if category not in CATEGORIES:
        raise ToolError("bad_category", f"category は {'|'.join(CATEGORIES)} のいずれか")
    src = os.path.abspath(source)
    st = _lstat_or_none(src)
    if st is None:
        raise ToolError("source_not_found", "sourceが存在しない")
    if _st_is_link(st) or not stat.S_ISREG(st.st_mode):
        raise ToolError("source_not_regular_file", "sourceは実ファイルのみ(ディレクトリ/リンク不可)")
    name = os.path.basename(src) if name is None else name
    check_name(name, "name")
    root_abs = os.path.abspath(root)
    if _is_inside(src, os.path.join(root_abs, TRASH_NAME)):
        raise ToolError("in_trash", "一時ゴミ箱内のファイルはpublish不可")
    nested = [] if subdir is None else subdir.replace("\\", "/").split("/")
    for component in nested:
        check_name(component, "subdir")
    parts = [project, category] + nested + [name]
    rel = "/".join(parts)
    dest = os.path.join(root_abs, *parts)
    check_root_path(root_abs)
    if walk_no_links(root_abs, parts) and os.path.samefile(src, dest):
        with Store(root_abs, readonly=True):
            return {"ok": True, "command": "publish", "status": "noop", "destination": rel}
    with Store(root_abs) as store:
        exists = walk_no_links(root_abs, parts)
        dest = os.path.join(root_abs, *parts)
        if exists and os.path.samefile(src, dest):
            return {"ok": True, "command": "publish", "status": "noop", "destination": rel}
        for i in range(len(parts)):
            check_case_conflict(os.path.join(root_abs, *parts[:i]), parts[i])
        if exists and not _is_regular_file(dest):
            raise ToolError("destination_not_regular_file", "既存の宛先が実ファイルではない")
        src_sha = sha256_file(src)
        ensure_dirs(root_abs, parts[:-1])
        stage = os.path.join(os.path.dirname(dest), f".{uuid.uuid4().hex}.tmp")
        status, trash_id = "published", None
        try:
            shutil.copy2(src, stage)
            if sha256_file(stage) != src_sha:
                raise ToolError("verify_failed", "コピーのsha256が一致しない(sourceは無変更)")
            if exists and sha256_file(dest) == src_sha:
                status = "unchanged"  # 内容同一: 旧版の退避も置換も不要
            else:
                old = None
                if exists:
                    old = _trash_item(store, parts, "publish上書きによる旧版退避", now)
                    trash_id, status = old["id"], "replaced"
                try:
                    os.rename(stage, dest)
                except OSError as ex:
                    if old is not None:  # 旧版を元の場所へ戻す(失敗時は隔離内に残る)
                        try:
                            os.rename(store._stored_path(old), dest)
                            store.items.remove(old)
                            store.save()
                            store.drop_item_dir(old)
                        except OSError:
                            pass
                    raise ToolError("commit_failed", f"確定に失敗: {ex.__class__.__name__}")
                if sha256_file(dest) != src_sha:
                    raise ToolError("verify_failed", "確定後のsha256が一致しない(sourceは残した)")
        finally:
            if os.path.lexists(stage):
                os.unlink(stage)
        source_removed = False
        if not copy and sha256_file(src) == src_sha:  # sourceが検証中に変わっていたら残す
            os.unlink(src)
            source_removed = True
        out = {"ok": True, "command": "publish", "status": status,
               "destination": rel, "source_removed": source_removed}
        if trash_id:
            out["trash_id"] = trash_id
        return out


# ---------------------------------------------------------------- CLI

def _build_parser():
    ap = argparse.ArgumentParser(description="成果物フォルダ管理ツール")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="成果物ルート(テスト用に変更可)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish")
    p.add_argument("--source", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--category", required=True)
    p.add_argument("--name")
    p.add_argument("--copy", action="store_true")
    p.add_argument("--subdir", help="用途内の日付・制作単位のサブフォルダ")
    t = sub.add_parser("trash")
    t.add_argument("--path", required=True)
    t.add_argument("--reason", required=True)
    g = sub.add_parser("purge")
    g.add_argument("--apply", action="store_true", help="指定時のみ実際に削除")
    r = sub.add_parser("restore")
    r.add_argument("--id", required=True)
    sub.add_parser("list")
    return ap


def main(argv=None):
    a = _build_parser().parse_args(argv)
    try:
        if a.cmd == "publish":
            out = publish(a.root, a.source, a.project, a.category, a.name, a.copy, subdir=a.subdir)
        elif a.cmd == "trash":
            out = trash(a.root, a.path, a.reason)
        elif a.cmd == "purge":
            out = purge(a.root, apply=a.apply)
        elif a.cmd == "restore":
            out = restore(a.root, a.id)
        else:
            out = list_items(a.root)
        code = 0
    except ToolError as ex:
        out = {"ok": False, "error": ex.code, "message": ex.message}
        code = ex.exit_code
    except OSError as ex:
        out = {"ok": False, "error": "os_error", "message": ex.__class__.__name__}
        code = 1
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
