# 画面編集室 — App Screen Studio

アプリ画面をブラウザーへ表示し、文字・フォント・位置・大きさ・画像・図形を直接編集してAIに返すローカルツール。材質10種、ガラス・金属の細かな調整、最大4色の線形/放射グラデーション、文字の発光にも対応する。

**画面編集 → 保存 → AIへ渡す → AIが元コードへ反映 → 再確認**のためのツール。画面上の変更だけでは製品コードは変わらない。

## すぐ試す

Python 3.10以上を用意し、ZIPを展開したフォルダで実行する。

```sh
python launch.py
```

ターミナルに表示されたローカルURLを開く。終了はCtrl+C。画面編集・ファイル保存はPython標準ライブラリだけで動く。素材やAI認証情報をクラウドへアップロードする処理はない。

実際のアプリを開く例：

```sh
python launch.py --project MyApp --dir /path/to/app --entry /index.html
python launch.py --project MyApp --url http://127.0.0.1:5173
```

Windowsでは `/path/to/app` を実際のフォルダパスに置き換える。空白を含むパスは引用符で囲む。

成果物の既定保存先は利用者のホーム内 `AppScreenStudio/成果物/<プロジェクト名>/`。`--output-root <フォルダ>` または環境変数 `APP_SCREEN_STUDIO_OUTPUT_ROOT` で変更できる。編集JSONは `関連資料/画面編集/`、画像は `画像/画面編集/`、AIへの依頼文は `関連資料/画面編集/依頼/` に入る。更新した旧版は一時ゴミ箱へ隔離され、復元できる。完全削除は自動では行わない。

## キャラクター編集

上部の「キャラクター編集」を押し、一覧か「画面から選ぶ」で対象を選ぶ。SVGでは目・口・体などの部品を選び、色・位置・大きさを変更できる。画像は差し替え・位置・大きさに対応する。

選択した全体または部品に、まばたき・口の開閉・左右の揺れ・上下の弾み・呼吸を設定できる。強さは3段階、1周期は0.2〜30秒、再生・停止を切り替えられる。これはプリセットの繰り返し動作で、音声に同期する口の動きではない。取り消し・保存・再適用・AIへの受渡しに対応する。動きの元コードへの反映には、同梱の `static/character-motion.css` の該当する定義も取り込む。

アプリ側でキャラのルートに `data-character` と `data-character-name`、部品に `data-character-part` と安定した `data-edit-id` を付けると選びやすい。HTMLの部品も `data-character-part` で指定できる。外部SVGを読み込んだ画像やPNGなどの目・口は個別に選べない。編集できるインラインSVGやHTMLの部品を用意する。Canvasや3D描画の内部は専用の連携実装が必要で、ここでは外枠の変更と動きが対象。

「AIへ送信」を押すと送る文章を欄から分離し、次の下書きをそのまま書ける。失敗や結果不明の場合は送った文章を下書きの先頭へ戻し、自動で再送しない。「AI用ファイルを保存」は未送信のため文章を保持する。

## 各環境への導入

まず、導入先を確認する例：

```sh
python install.py --target claude --scope project --project /path/to/project --dry-run
```

`--dry-run` を外すと導入する。既存スキルがある場合は上書きせず停止する。

| 環境 | 導入・使い方 | AIへの受渡し |
|---|---|---|
| Claude CodeアプリのCodeタブ | `--target claude`でプロジェクトへ導入し、「画面スキルで開いて」と依頼 | 依頼文ファイルをCodeタブのAIへ指定 |
| ターミナルのClaude Code | 同じ導入で `/app-screen-studio` または「画面スキル」 | 依頼文ファイルを指定 |
| VS CodeのClaude Code拡張 | 同じプロジェクトのスキルを使う | 依頼文ファイルを指定 |
| Orca | `--target orca`で `.agents/skills/` へ導入 | ファイル受渡し、または同梱のOrca接続機能 |
| その他のAI・統合ターミナル | `python launch.py`で起動 | 依頼文・JSON・画像を手動で渡す |

全プロジェクトへ導入する場合は `--scope user`。対象は `.claude/skills/`、`.agents/skills/`。Codex用には `--target codex` も用意してある。VS Code自体にAIは含まれないので、上記はClaude Code拡張か、利用中のAIとのファイル受渡しを指す。Claudeの通常のChatタブとは別。ローカルでファイルにアクセスできるセッションが対象で、クラウド実行・別PCへの自動転送には対応しない。

## 指示文と画像の渡し方

1. 編集して「保存」を押す。
2. 「AIへ渡す指示をコピー」を押す。クリップボードへコピーし、下の指示欄にも追加する。既存の文章は残り、連打で同じ指示を重複させない。コピーが拒否されても指示欄には入る。
3. 追加の希望を書き、必要なら画像を添付・貼り付ける。
4. 「AI用ファイルを保存」を押し、表示された依頼文ファイルを使用中のAIへ渡す。文章・画像のパス・編集JSONの場所がまとまる。保存だけでは送信されない。

Orcaの自動接続を使う場合だけ「AIへ送信」で同じワークスペースの呼出元AIへ登録できる。送信登録と相手の受領は別。別環境のチャット欄へ勝手に入力・送信しない。

## Orcaでの起動

呼出元AIの環境から実行する：

```sh
python skills/app-screen-studio/tool/prepare_launch.py --project MyApp --dir /path/to/app --entry /index.html --port 4780
```

返された起動引数を同じワークスペースの可視ターミナルで実行する。Orca CLIが実行可能で、対象AIセッションが取得できることが必要。Windowsの正式CLI連携を検証済み。Mac/LinuxのOrca接続は未検証のため、ファイル受渡しを利用する。

編集室の「画面をスクショ」はOrca連携とPillowが必要：`python -m pip install Pillow`。他環境では対象アプリ部分を撮った画像を添付する。編集機能そのものにスクショ連携は必須ではない。

## 現在の確認範囲と制限

Windows上でサーバー・保存・添付・AI用ファイル出力・スキル導入先・ブラウザー編集を検証する。Claude Codeアプリ/VS Codeそのものの実画面での呼出しは別途利用者確認が必要。提供APIのないチャット欄への自動送信を対応済みとは扱わない。

- 127.0.0.1のみで待ち受ける。公開サーバー用途にしない。
- アプリの再描画や画面遷移で未保存の編集が消える場合がある。再読込の前に保存し、指示文と画像を保護する。
- `--url` はWebSocketを中継しないため開発サーバーの自動更新には制限がある。
- ネイティブ専用画面には編集可能なWebプレビューが必要。ブラウザー表示は実機表示の証明ではない。
- AIへの指示文は最大8,000文字。添付は8枚、画像1枚5MBまで。

## 検証

```sh
python -m unittest discover -s skills/app-screen-studio/tool/tests -v
python -m unittest discover -s tests -v
```

テストは一時フォルダを使い、実AIへ送らない。画像試験にはPillow、ブラウザー試験にはPlaywrightと対応ブラウザーが必要。

## 配布とライセンス

このフォルダがソース一式。利用者の成果物・画像・認証情報・個別プロジェクトは含めない。ソースはリポジトリ、利用者向けZIPはGitHub Releasesで配布する。Pythonサーバーが必要なのでGitHub Pagesだけで編集室全体を動かすことはできない。

0.2.0はキャラクター編集を追加したプレビュー版。確認済みの範囲と、各環境で確認が必要なことは [検証記録](RELEASE_REVIEW.md) を参照。

[MITライセンス](LICENSE)。Python・ブラウザー・Orca・Claude Codeなど外部製品の利用条件と契約は、それぞれの提供元に従う。

参考：[Claude Codeのスキル](https://code.claude.com/docs/en/skills)、[DesktopのCodeタブ](https://code.claude.com/docs/en/desktop)、[VS Code](https://code.claude.com/docs/en/vs-code)、[GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。
