# 開発

## アーキテクチャ

```
 Browser (React, Canvas)          Server (Python + Rust core)
 ─────────────────────            ──────────────────────────────────────────
 tiles (PNG) ◀──────────────────  KLayout LayoutView renders tiles (cached)
 selection outlines, layer list   KLayout DB: select / edit / undo / save
 schematic primitives ◀─────────  undine._core (Rust): Xschem reader / writer, view, connectivity
 user actions ─────────────────▶  Design API (FastAPI, orjson)
 agents (MCP, CLI, Python) ─────▶ the same tools, in changesets a person reviews
                                  PDK registry and process profiles (versions pinned per project)
                                  jobs: ngspice, KLayout / Magic / Netgen, Verilator, LibreLane
```

重い処理はネイティブコードが担います。KLayout(C++)がタイルの描画と DRC / LVS を行い、ngspice、Magic、Netgen、Verilator、LibreLane は
外部ツールとして動き、`undine._core`(Rust、`server/src`、maturin でサーバーのパッケージに組み込んでビルド)が Xschem の回路図を読み込み、
描画し、接続を求めます。Python は API、編集、全体の制御を受け持ちます。9,200 インスタンスの回路図では、開くのにかかる時間が 1.5 秒から
0.12 秒に、4,800 本の配線の接続の抽出が 10 秒から 21 ミリ秒になりました。コアが置き換えた Python の実装は参照用としてコードに残っており
(`xschem._py_parse` … `schematic._py_extract`)、`server/tests/test_core.py` が両者の結果が同じであることを確かめています。

プロセスに固有のこと(レイヤー番号、ルールデッキとそのスイッチ、Magic / Netgen の設定、モデルとコーナー、PCell ライブラリと回路図の
デバイスとの対応)は、PDK のファミリーごとに `undine/pdks/pdkprofile.py` にまとめて書いてあり、サーバーのほかの部分はそこから読みます。

## リポジトリの構成

```
setup.sh           sets everything up with pixi (environments, ngspice, UI, PDK), then checks
pixi.toml          environments: default (server, UI toolchain, EDA tools), pcell, pcell-gf180, docs; tasks
server/            the server: one Python package with its Rust core
  pyproject.toml   built with maturin (undine/ plus src/ into undine._core)
  Cargo.toml, src/ the Rust core: Xschem reader / writer, schematic view, connectivity
  undine/
    __main__.py    the undine command (serve, new, call, mcp-stdio, doctor ...)
    doctor.py      what is installed, what is missing
    tools.py       finds the EDA tools ($UNDINE_TOOLS, the pixi environment, PATH)
    server/        the Design API (FastAPI), live writers, idempotent requests
    store/         projects and libraries, changesets, operation log, dependencies, provenance, cell overview
    vcs/           semantic diff and three-way merge per view, the git merge driver, resolve, merge-check,
                   the git history, changesets' branches (base commits, worktrees, merges)
    pdks/          PDK discovery, process profiles (layers, decks, models, PCell libraries), PCells
    schematics/    Xschem files, netlisting, schematic and symbol editing, schematic routing, netlist view
    layouts/       the layout database and its storage, schematic-driven layout
    checks/        KLayout DRC / LVS, Magic and Netgen, shuttle targets (tapeout)
    simulation/    ngspice jobs, their results, specs at corners and sweeps
    design/        design intent, RTL (Verilator), LibreLane
    agents/        the tools agents call, MCP (HTTP and stdio), the CLI / Python client, the in-app agent and its
                   LLM gateway, pictures for agents, the Agent Skill
    data/          the Agent Skill, Tiny Tapeout pin table, extension icon
  tests/           pytest suite
web/               the UI: React + Vite (editors, panels, i18n, theme)
docs/              this documentation (MkDocs, mkdocs.yml), English and Japanese, screenshots in docs/images
scripts/           dev.sh, restart.sh, ngspice.sh and osdi.sh (setup steps), capture_screenshots.py and
                   capture_showcase.py (docs images)
examples/          scripts that build demo designs
```

import は絶対パスで書きます: `from undine.checks import verify`、`from undine.store.project import ProjectStore`。

## ローカルでの開発

```sh
pixi run dev                      # API on :8700, Vite with hot reload on :5173 (proxies /api)
./scripts/restart.sh              # restart the API server in the background (log: $UNDINE_LOG, default /tmp/undine.log)
pixi run test                     # server tests (including the Rust core against its Python references)
pixi run -e docs docs-serve       # this documentation on :8000, rebuilt on save (docs-build: strict build into site/)
```

Rust コア(`server/src`)を変更したら、`pixi install` で編集可能なサーバーのパッケージに再ビルドされます。どのテストに EDA ツールと PDK が
必要か、時間のかかるテストをどう実行するかは、[Running tests](https://github.com/kadomoto-lab/undine-eda/blob/main/CONTRIBUTING.md#running-tests)
にあります。

ドキュメントは英語(`page.md`)と日本語(`page.ja.md`)を並べて置いています。日本語版のないページは英語版を表示します。
ページの書き方とプレビューの方法、スクリーンショットの作り直し方は
[Contributing](https://github.com/kadomoto-lab/undine-eda/blob/main/CONTRIBUTING.md#documentation) にあります。

## UI の言語

UI は i18next/react-i18next を使用します。英語と日本語の辞書は `web/src/i18n/` に機能ごとにまとめ、同じ変更で両方を更新します。
キーは英文ではなく、`review:actions.adopt` のように用途を表します。各辞書は `en` と `ja` を `as const` で公開し、共通 API が
有効なキーと必須の埋め込み変数を推論します。

- 翻訳を表示するコンポーネントで `useI18n()` を呼びます。`t` の関数参照は言語変更で変わりません。言語変更でデータ取得、
  ポーリング、ジョブ、編集ツールを再起動しないでください。コンポーネントの key に言語を含めません。
- すぐ表示する文言は `t(key, values)`、ダイアログ・入力エラー・通知・キャッシュしたラベルは `msg(key, values)`（`UiText`）で
  保持し、描画時に `resolveText()` で解決します。`joinText()` や入れ子のメッセージでも言語切り替えに追従します。
- 文章全体を翻訳します。件数には `count` と複数形キー（英語は `_one` / `_other`、日本語は `_other`）を使い、呼び出しでは
  接尾辞を除いたキーを指定します。設計名・設計内の文字、SPICE 値、単位、API/MCP 識別子、ユーザーや AI の文章、生ログは保持します。
- API に送る既定値と表示ラベルを分けます。HTTP エラーは `detail` を維持し、`code` / `params` を追加しています。未対応のエラーは
  診断原文を表示します。タイムラインの `effect_data` と検証の `summary` で表示用の構造化データを提供し、旧クライアントや過去の記録用の
  フィールドも保持します。
- `npm test` は JSX・アクセシブル名の直書き、翻訳の不足、複数形、埋め込み変数、フォームを開いたままの言語切り替えを検査します。
  `npm run build` で呼び出し側の型も検査します。製品名・単位の例外は `web/scripts/check-i18n.mjs` に明記し、通常の UI 文言は例外に入れません。

用語: **Agent → 設計エージェント**、**proposal → 提案**、**Adopt → 採用**、**Request changes → 修正を依頼**、
**Discard → 破棄**、**schematic → 回路図**、**layout → レイアウト**、**Restore to here → ここまで戻す**。
UI 言語はブラウザごとの設定（`undine.language`）で、Agent の回答や MCP クライアントの言語には影響しません。

## コントリビュート

Issue とプルリクエストを歓迎します。バグの報告、機能の提案、テストの実行、プルリクエストの準備の方法は
[CONTRIBUTING.md](https://github.com/kadomoto-lab/undine-eda/blob/main/CONTRIBUTING.md) にあります。
