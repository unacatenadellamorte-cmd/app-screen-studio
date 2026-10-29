"""配布フォルダから画面編集室を起動する。終了はCtrl+C。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'skills/app-screen-studio/tool'))
from studio import main

if __name__ == '__main__':
    raise SystemExit(main())
