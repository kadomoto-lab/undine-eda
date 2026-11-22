# ロードマップ

Undine はアルファ版です。[機能](overview.md#features)に挙げたものはすべて今動きます。このページには、計画していて、まだ作っていないものだけを
挙げます。

## パッケージと PDK

ライブラリと PDK を、cargo の crate や npm のパッケージのように共有します。プロジェクトがライブラリのバージョン範囲を求めると、
Undine は解決して共有のストアに一度だけ取得し、取得したものを厳密に固定し、ハッシュで確かめます。公開は git の index リポジトリへの
プルリクエストで、PDK は ciel で取得し、commit と木のハッシュで固定します。設計は[パッケージと PDK](design/packages.ja.md#phases)で
決まっています。段階 0(今の依存の修正)と段階 1(各ファイルを一度だけ持ち、`lib sync` をオフラインで動かせる共有のストア。PDK を
commit 全体と木のハッシュで固定し、確認した後に ciel で取得する)は済んでいます。段階 2(パッケージの manifest で、ライブラリが
自分の依存を持ち込める。セル名の重なりと PDK の食い違いを、何かを install する前に見つける。依存のコードブロックと RTL は、人が
信頼してから使う。[ライブラリと依存関係](guide/libraries.ja.md)を参照)も済んでいます。これから作るもの:

- [x] **段階 3: index。** 公開と private の index から、パッケージを名前とバージョンで取ることはできました: `pkg search`、
  `pkg add`、`pkg update`、バージョン範囲、yank されたバージョン、複数の index
  ([インデックスのパッケージ](guide/libraries.ja.md#packages-from-an-index))。既定の index
  [kadomoto-lab/undine-index](https://github.com/kadomoto-lab/undine-index) はできていますが、まだパッケージはありません。
  `pkg licenses`、`pkg vendor`、`pkg sync --from-vendor` はでき、テープアウトのバンドルが固定した木とそのライセンスを持ちます
  ([ライセンスと vendor](guide/libraries.ja.md#licences-and-vendoring))。ライブラリブラウザにパッケージ欄があり、依存の行に新しいバージョンを表示します
  ([ブラウザのパッケージ欄](guide/libraries.ja.md#packages-in-the-browser))。パッケージのセルのシンボルのプレビューはあとに回します。ブラウザからの
  取得は、設定した index のホストと `serve --dep-host` で指定したホストにだけ行き、どの取得も `UNDINE_FETCH_MAX` で止まります。
- [ ] **段階 4: changeset とエージェント。** 依存の変更を、ほかの変更と同じようにレビューして採用する。エージェントは依存を提案
  できるが、取得するのは人だけ。結果に、それを作ったパッケージと PDK のバージョンを記録する。ここまでにできたもの:
  `project.json` と `undine.lock` を `git merge` でも changeset の採用でもエントリ単位でマージし、マージした lock が
  `project.json` の求めるものを固定していない採用を拒否する([merge.ja.md](design/merge.ja.md))。
- [ ] **段階 5: 公開。** パッケージを検証し、まとめ、公開するコマンド。その検証の報告を index からリンクする。2 つのバージョンの
  間で何が変わったかを、セル単位まで示す報告。
- [ ] **段階 6: PDK プロファイル。** 各 PDK family のプロセスの知識を、対応する PDK のバージョンを名前で示すデータのパッケージにする。
  ここまでにできたもの: 各 family のプロファイルはデータになりました。Undine に組み込んだ ciel の family ごとのプロファイルの
  パッケージにある `profile.toml` で(置き換えた Python と欄ごとに照らしてあります)、`./setup.sh`、`pixi run pdk`、Undine による
  PDK のバージョンの選択は、動作確認した PDK のバージョンをそこから読みます([PDK のプロファイル](guide/pdks.ja.md#pdk-profiles))。

後で行うこと: index の commit やパッケージのタグへの署名(それまでは git のハッシュが内容を固定する)と、index の CI での DRC と
LVS(それまでは公開する人自身の検証の報告にもとづいて公開する)。

## 設計の意図

[設計の意図](design/intent.ja.md)の「手順」のうち、まだ作っていないもの:

- [ ] 回路図とレイアウトへのオーバーレイと、最新のテストベンチの結果と古さを添えたスペックの表示。
- [ ] フローの段階ごとに導出する制約。実行と一緒に保存し、意図までたどれるようにし、上書きとエージェントの提案も扱う。
- [ ] 既存の LibreLane の設計の取り込み。

## ネットリストと回路図

- [ ] 合成結果など、図面を持たないネットリストの取り込み([netlist ビュー](design/netlist.ja.md)はすでにそれを描けます)。
- [ ] 回路図エディタで、ピンをそのネットから外すコマンド(エージェントには `disconnect_pins` があります)。

## アクセス

- [ ] サインイン。インターネット上のサーバーを安全に公開でき、ブラウザのチャット(claude.ai、ChatGPT)が MCP でつなげるように
  する。それまでは Claude Desktop か Claude Code を使ってください([AI エージェントをつなぐ](getting-started/connect-ai.ja.md))。
