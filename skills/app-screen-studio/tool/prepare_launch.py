"""画面スキルの起動時に、呼出元AIと同じワークスペースへの接続引数を自動準備する。"""
import argparse
import json
import os
import sys
import urllib.parse

from orca_bridge import OrcaBridge, BridgeError, norm_path


def prepare(project, source=None, entry="/index.html", port=4780, upstream=None, cli=None, runner=None):
    bridge = OrcaBridge(cli=cli, runner=runner)
    if not bridge.cli:
        raise BridgeError("cli_missing", "Orca CLIが見つかりません")
    status = bridge._json(["status", "--json"])
    caller = status.get("caller") or {}
    target = caller.get("orcaSessionId")
    if not target or not caller.get("live"):
        raise BridgeError("no_caller", "画面スキルを呼び出したAIのセッションを確認できません")
    prefix = "structured-agent-session-" + target.split(":", 1)[-1] + ":"
    workspaces = bridge._ps()
    matches = [w for w in workspaces if any(str(a.get("paneKey", "")).startswith(prefix) for a in w.get("agents", []))]
    paths = {norm_path(w.get("path", "")) for w in matches}
    if len(paths) != 1:
        raise BridgeError("no_workspace", "呼出元AIのワークスペースを一つに特定できません")
    workspace = matches[0]["path"]
    ids = {w["worktreeId"] for w in matches}
    tabs = bridge._json(["tab", "list", "--worktree", "all", "--json"]).get("tabs", [])
    candidates = []
    for tab in tabs:
        try:
            url = urllib.parse.urlsplit(tab.get("url", ""))
            same = (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost") and url.port == port
                    and url.path == "/__screen_studio__/shell" and tab.get("worktreeId") in ids)
        except ValueError:
            same = False
        if same:
            candidates.append(tab)
    if len(candidates) > 1:
        raise BridgeError("ambiguous_tab", "同じ接続先の編集タブが複数あります。本人の入力があるタブを確認してください")
    args = [sys.executable, "-X", "utf8", os.path.join(os.path.dirname(__file__), "studio.py"),
            "--project", project, "--entry", entry, "--port", str(port),
            "--orca-worktree", workspace, "--ai-target", target]
    if source:
        args += ["--dir", os.path.abspath(source)]
    if upstream:
        args += ["--url", upstream]
    if candidates:
        args += ["--orca-page", candidates[0]["browserPageId"]]
    else:
        args += ["--orca-open"]
    # PowerShellでは単一引用符内の単一引用符だけを二重化し、入力をコードとして解釈させない。
    command = "& " + " ".join("'" + a.replace("'", "''") + "'" for a in args)
    return {"ok": True, "worktree": workspace, "target": target, "page": candidates[0]["browserPageId"] if candidates else None,
            "argv": args, "powershell": command, "connection_check_required": True}


def main():
    parser = argparse.ArgumentParser(description="画面スキルの自動接続準備")
    parser.add_argument("--project", required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dir")
    group.add_argument("--url")
    parser.add_argument("--entry", default="/index.html")
    parser.add_argument("--port", type=int, default=4780)
    args = parser.parse_args()
    try:
        result = prepare(args.project, args.dir, args.entry, args.port, args.url)
    except BridgeError as ex:
        print(json.dumps({"ok": False, "code": ex.code, "message": ex.message}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
