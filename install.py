"""画面スキルを選択した環境へ導入する。既存のスキルは上書きしない。"""
from pathlib import Path
import argparse
import shutil

def install(target, scope, project=None, preview=False):
    base = Path.home() if scope == 'user' else Path(project or Path.cwd()).resolve()
    folder = {'claude': '.claude', 'orca': '.agents', 'codex': '.codex'}[target]
    destination = base / folder / 'skills/app-screen-studio'
    source = Path(__file__).resolve().parent / 'skills/app-screen-studio'
    if destination.exists():
        raise FileExistsError('既存のスキルを保護するため停止: ' + str(destination))
    if not preview:
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    return destination

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=['claude', 'orca', 'codex'], required=True)
    parser.add_argument('--scope', choices=['project', 'user'], default='project')
    parser.add_argument('--project', help='導入するプロジェクトのパス。省略時は現在のフォルダ')
    parser.add_argument('--dry-run', action='store_true', help='導入先の確認だけ行う')
    args = parser.parse_args()
    try:
        destination = install(args.target, args.scope, args.project, args.dry_run)
    except (FileExistsError, OSError) as error:
        parser.exit(1, str(error) + '\n')
    print(('導入先: ' if args.dry_run else '導入したスキル: ') + str(destination))

if __name__ == '__main__':
    main()
