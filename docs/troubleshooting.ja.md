# トラブルシューティングと FAQ

このページでは、よく起きる問題をその原因と対処とともに挙げ、そのあとで Undine についてのよくある質問に答えます。ほとんどの項目は
表示されるメッセージから始まります。メッセージの一部をこのページで検索してください。

ここにない問題は、[バグを報告するには](#how-do-i-report-a-bug) を見てください。

<a id="start-here-run-the-check"></a>
## まずチェックを実行する

インストールしたばかりのときの問題は、ほとんどがツールか PDK の不足です。まずチェックを実行します:

```sh
./setup.sh check          # or: pixi run doctor
```

各行に見つかったものが表示され、足りないものがあれば、それを直すコマンドも表示されます:

| 印 | 意味 |
|---|---|
| `✓` | 見つかった |
| `✗` | 見つからず、`required`: サーバーが起動しない、または何も表示されない |
| `-` | 見つからず、`recommended`(シミュレーション、DRC / LVS、PCell などの機能が使えない)または `optional` |
| `!` | 注記。Undine がサポートを終了した PDK のバージョンなど |

最後の行は `ready: python -m undine serve` か、`required items are missing`(終了コード 1)です。レベルの説明は
[インストール](getting-started/install.ja.md#3-check-and-fix) にあります。

Undine の動作中に何かが失敗したときは、サーバーの出力を見ます。出力がどこにあるかは、サーバーの起動のしかたによります:

| 起動のしかた | 出力の行き先 |
|---|---|
| `pixi run serve` | そのターミナル |
| `./scripts/restart.sh` | `$UNDINE_LOG`。既定は `/tmp/undine.log` |
| AI アプリが起動した(`undine mcp-stdio`) | `$UNDINE_LOG`。既定は `~/.undine/serve.log` |

シミュレーション、DRC、LVS のジョブは、それぞれ自分のフォルダ `<project>/runs/<job>/` も持ち、そこに `status.json` とツールのログが
あります([データ配置](reference/data.ja.md))。

<a id="installation"></a>
## インストール

### `pixi not found`

**原因。** `setup.sh` はすべてを [pixi](https://pixi.sh/) で入れますが、pixi が入っていないか、使っているシェルが pixi を入れる前に
開いたものです。

**対処。** pixi を入れ、新しいシェルを開いて、もう一度 `./setup.sh` を実行します:

```sh
curl -fsSL https://pixi.sh/install.sh | sh
```

### `failed: <step> -- see the messages above`

**原因。** `setup.sh` の手順(`install`、`ngspice`、`web`、`pdk`、`osdi`)のどれかが失敗しました。ほかの手順は実行され、最後に
チェックも行われています。

**対処。** まとめの上にあるその手順のメッセージを読み、原因を直してから、その手順だけをもう一度実行します。例えば:

```sh
./setup.sh ngspice
```

どの手順もいつでも実行し直せます。`./setup.sh --help` で一覧が出ます。

<a id="the-klayout-application-is-not-on-path"></a>
### KLayout アプリケーションが PATH にない

**原因。** DRC と LVS は、KLayout の *アプリケーション*(`klayout`)で PDK のデッキを実行します。これは pixi では入れられず、
klayout.de からシステムのパッケージとして入れます。ないと、チェックで `klayout (DRC / LVS)` が missing と表示され、DRC や LVS の
実行は `klayout not found (set UNDINE_TOOLS or put it on PATH)` で失敗します。

**対処。** KLayout 0.30 以上を [klayout.de/build.html](https://www.klayout.de/build.html) から入れます。
`./setup.sh check` が、使っているディストリビューション向けのダウンロードとインストールのコマンドを表示します。パッケージは
インストールの [ディストリビューション別](getting-started/install.ja.md#per-distribution) の表にあります。入れたら確認します:

```sh
klayout -v
```

サーバーは、レイアウトのデータベースに PyPI の `klayout` *Python モジュール*(pixi 経由)を使います。こちらは `setup.sh` が入れるもので、
アプリケーションとは別物です。

<a id="pmap-is-missing-or-sky130-lvs-fails-at-once"></a>
### `pmap is missing`、または sky130 の LVS がすぐに失敗する

**原因。** sky130 の KLayout LVS デッキは、メモリ使用量を記録するために `pmap` を呼び、それがないと失敗します。普通の Linux には
入っていますが、最小構成のコンテナイメージには入っていません。

**対処。** それを含むパッケージを入れます:

=== "Debian, Ubuntu"

    ```sh
    sudo apt install procps
    ```

=== "RHEL, Rocky, AlmaLinux"

    ```sh
    sudo dnf install procps-ng
    ```

<a id="my-distribution-is-too-old-centos-7"></a>
### ディストリビューションが古すぎる(CentOS 7)

**原因。** pixi の環境には glibc 2.28 以上が必要です(UI 用の Node.js がそれを必要とします)。CentOS 7 の glibc は 2.17 です。

**対処。** サポートしているシステムを使います: Ubuntu 20.04 以上、RHEL / Rocky / AlmaLinux 8 または 9、Windows の WSL2。glibc の
バージョンは `ldd --version` で確かめられます。インストールの [始める前に](getting-started/install.ja.md#1-before-you-start) を見てください。

<a id="undine_core-rust-core-is-missing"></a>
### `undine._core (Rust core)` が missing

**原因。** サーバーには Rust で書いたネイティブのコア(`server/src`)があり、`pixi install` がそれをコンパイルしてサーバーの
パッケージに入れます。ビルドが実行されなかったか失敗した、または(`git pull` のあとで)Rust のコードが変わったのにビルドし直していません。

**対処。** `pixi install`(または `./setup.sh install`)を実行し、出力にコンパイルエラーがないか読みます。pixi を使わない場合、ビルドには
`cargo`(例えば rustup で入れたもの)が要ります。[pixi を使わない場合](getting-started/install.ja.md#without-pixi) を見てください。

<a id="web-ui-build-webdist-is-missing-or-the-ui-looks-out-of-date"></a>
### `web UI build (web/dist)` が missing、または UI が古いままに見える

**原因。** ブラウザの UI は `web/dist` にビルドします。一度もビルドしていないか、作業コピーを更新したのにビルドが古いコードのままです。

**対処。** ビルドし直してから、ページを再読み込みします:

```sh
./setup.sh web            # same as: pixi run web
```

`git pull` のたびに `./setup.sh`(すべての手順)を実行すれば、すべてが最新になります。ngspice と PDK の手順はすることがなければ飛ばし、
UI はビルドし直します。

<a id="ngspice-not-found-or-a-tool-installed-another-way-is-not-used"></a>
### `ngspice not found`、または別の方法で入れたツールが使われない

**原因。** Undine は ngspice、Xschem、Netgen、Magic を、まず pixi の環境(`pixi run` が設定する `$UNDINE_TOOLS`)から、次に `PATH` から
探します。pixi の外の Python で起動したサーバーには、pixi の環境のツールが見えません。そのとき、チェックの `pixi environment` の行は
`none (not run with pixi)` です。

**対処。** Undine を pixi から起動します: `pixi run serve`、または `pixi run python -m undine ...`。ほかの場所のツール(apt、
IIC-OSIC-TOOLS、自分でビルドしたもの)を使うには、それを `PATH` に置くか、`bin/` にそれらがあるプレフィックスを `UNDINE_TOOLS` に
設定します([設定](reference/cli.ja.md#configuration-environment))。

<a id="pcell-environment-is-missing"></a>
### `PCell environment` が missing

**原因。** PCell は別の Python 環境で動きます: `pcell`(gdsfactory 8 と kfactory。sky130 と IHP 用)と `pcell-gf180`(gdsfactory 7.9。
GF180MCU 用)です。ないと、PCell の配置が `the PCell environment is not installed: pixi install -e pcell` で失敗します。

**対処。**

```sh
pixi install -e pcell
pixi install -e pcell-gf180     # only for GF180MCU
```

pixi を使わない場合は、それらのパッケージを入れた Python を `UNDINE_PCELL_PYTHON`(と `UNDINE_PCELL_GF180_PYTHON`)で指定します。

<a id="pdks"></a>
## PDK

<a id="no-pdk-found-a-pdk-in-the-check-or-undine-no-pdk-sky130a"></a>
### PDK が見つからない(チェックの `a PDK`、または `undine: no PDK sky130A`)

**原因。** Undine が PDK を見つけられませんでした。探す場所は `$PDK_ROOT`、`~/.ciel`、`~/.volare` です。

**対処。** Undine が動作確認したバージョンの sky130 PDK を入れます:

```sh
./setup.sh pdk            # the tested SKY130 version, then the check
```

そのあと、Undine が見つけたものを一覧します:

```sh
pixi run python -m undine pdk list
```

GF180MCU と IHP SG13G2 は [PDK のインストール](guide/pdks.md#installing-a-pdk) を見てください。

<a id="i-installed-a-pdk-but-undine-does-not-list-it"></a>
### PDK を入れたのに Undine の一覧に出ない

**原因。** サーバーは起動したときに PDK を探すので、動作中に入れた PDK はまだ一覧にありません。または、PDK が Undine の探さない
ディレクトリにあります: あるシェルで `PDK_ROOT` を設定したのに、サーバーはそれのない別のシェルから(または AI アプリによって)起動された、
という場合です。

**対処。** PDK のバッジをクリックして **PDK を再検出** を選びます(または `POST /api/pdks/rescan`)。サーバーが再起動なしに探し直します。
再起動しても同じで、作業は失われません。それでも見つからなければ、`PDK_ROOT` をサーバーの環境に含めます
(`./scripts/restart.sh` と、AI アプリが起動するサーバーでは、`~/.undine/env` に `PDK_ROOT=...` を書きます)。または、グローバル
オプション `--pdk-root DIR` で PDK のルートを指定します。これはコマンドの前に置き、複数指定でき、既定の一覧を置き換えます:

```sh
pixi run python -m undine --pdk-root /opt/pdks --pdk-root ~/.ciel serve
```

`pixi run python -m undine pdk list` は、見つかった PDK をそれぞれのディレクトリとともに表示します。
[Undine が PDK を探す場所](guide/pdks.md#where-undine-looks-for-pdks) を見てください。

### `PDK sky130A@… is not on this server`

**原因。** プロジェクトが PDK の特定のバージョン(`project.json` の `pdk` フィールド)に固定されていて、そのバージョンがこのマシンに
入っていません。例えば、新しいコンピューターにプロジェクトをクローンしたあとです。日本語の UI では「このサーバーには PDK sky130A@… が
ありません」と表示されます。

**対処。** 取得します: PDK のバッジをクリックして **取得...** を選び、ダイアログの内容を確かめて **取得** を押します。ciel が終われば
プロジェクトが PDK を見つけます。コマンドラインからは次のとおりです(その後、動いているサーバーでは **PDK を再検出**):

```sh
pixi run python -m undine pdk fetch PROJECT            # add --network when the 12-character id is not installed here
```

または、プロジェクトを手元にあるバージョンに移します:

```sh
pixi run python -m undine set-pdk PROJECT sky130A@8afc8346a57f
```

[プロジェクトの PDK を取得する](guide/pdks.ja.md#fetching-a-projects-pdk) を見てください。

`set-pdk` は移したことを `pdk_history` に記録し、新しいバージョンを `undine.lock` に記録します(プロジェクトになければ作ります)。
[プロジェクトを別の PDK バージョンに移す](guide/pdks.md#moving-a-project-to-another-pdk-version) を見てください。

### `pdk fetch`: `no PDK root scanned is a ciel root`

**原因。** ciel は PDK のルートに入れ、Undine は自分が探すルートにだけ取得します: `--pdk-root`(または `$PDK_ROOT`、`~/.ciel`、
`~/.volare`)のうち、`ciel/` があるか、まだないか、空の最初のものです。どれも当てはまりません。例えば `~/.volare` しか探さないときや、
`PDK_ROOT` がほかのファイルのあるディレクトリを指しているときです。

**対処。** ciel 用のディレクトリを指定します: コマンドの前に `--pdk-root DIR`(サーバーにも同じもの)を書くか、`PDK_ROOT` をそこに
設定します。当てはまるルートがあれば、`pixi run python -m undine pdk fetch PROJECT` は尋ねる前にそれを表示します。

<a id="pdk-libstech-differs-from-the-pin-modified"></a>
### `PDK …: libs.tech differs from the pin (…)`(*変更あり*)

**原因。** プロジェクトの `undine.lock` は PDK をファイルで固定しています([format 2](guide/pdks.ja.md#pinning-the-pdk-by-its-files))。
入っている PDK のファイルが固定したときと違います: 入れた後に PDK を編集したか、別の取得元から入れ直しました。`lib status` は
これを問題として示し、PDK のバッジには *変更あり* と出ます。

**対処。** リリースされたとおりに入れ直してから確かめます:

```sh
pixi run ciel rm --pdk-family sky130 <commit>            # the command the message names
pixi run python -m undine pdk fetch PROJECT
pixi run python -m undine pdk verify PROJECT
```

変更が意図したもの(新しいファイルを使う)なら、代わりに今のとおりに PDK を固定します:
`pixi run python -m undine pdk verify PROJECT --pin`。

### `PDK …: unchecked: run python -m undine pdk verify PROJECT`

**原因。** 問題ではなく注記です。Undine が前にハッシュを取った後に PDK のファイルが変わった(いま取得した、更新した)か、このマシンでは
一度もハッシュを取っていません。`lib status` とバッジは、自分では PDK を読みません。

**対処。** `pixi run python -m undine pdk verify PROJECT` がハッシュを取ります(数秒)。その後、状態は *未検証* か *変更あり* になります。

<a id="the-pdk-badge-says-unverified-trust-on-first-use"></a>
### PDK のバッジに「未検証（初回の内容を信頼）」と出る

エラーではありません。PDK は `undine.lock` に固定したハッシュと合っていますが、そのバージョンのハッシュを公開している PDK プロファイルは
まだないので、リリースとではなく、固定したマシンにあったものと照らしています。[PDK の状態](guide/pdks.ja.md#pdk-states) を見てください。

<a id="the-pdk-badge-says-no-longer-supported"></a>
### PDK のバッジに「サポート終了」(*no longer supported*)と出る

**原因。** プロジェクトが 2025 年より前の open_pdks の sky130(例えば `bdc9412`)を使っていて、Undine はその PCell のコードを
もう実行しません。プロジェクトは開けて、シミュレーションと DRC / LVS も動きますが、PCell は使えません。

**対処。** バッジをクリックして **… に切り替える**(*Move to …*)を選びます(サポートしているバージョンが入っていなければ、
ダイアログがそのバージョンを示します。先に `./setup.sh pdk` を実行してバッジのメニューの **PDK を再検出** を選んでください)。または、次を実行します:

```sh
pixi run python -m undine set-pdk PROJECT sky130A@8afc8346a57f
```

古い PCell から生成したレイアウトのセルは、生成し直すまでそのまま残ります(回路図駆動のレイアウトでは **すべてのデバイスを再生成**
(*Regenerate all devices*))。詳しくは [サポートを終了したバージョン](guide/pdks.md#versions-no-longer-supported) にあります。

!!! note
    `pixi run doctor` の `!` の注記は、最初に見つかったサポート対象の PDK を示しますが、それは別のファミリーのものかもしれません。
    プロジェクトは同じファミリーのバージョンに移してください(`sky130A` のプロジェクトなら `sky130A@…`)。

### `project has no PDK`

**原因。** プロジェクトを PDK なしで作成しました(`--pdk` なしの `undine new PROJECT`)。シミュレーション、DRC、LVS には PDK が必要です。

**対処。** PDK に固定します:

```sh
pixi run python -m undine set-pdk PROJECT sky130A
```

名前だけを指定すると、入っているバージョンのうち Undine が PCell を実行できるものを選びます。動作確認したバージョンが優先です。

<a id="simulation"></a>
## シミュレーション

<a id="verilog-a-models-are-not-compiled-ihp-sg13g2"></a>
### `Verilog-A models are not compiled`(IHP SG13G2)

**原因。** IHP のトランジスタのモデルは Verilog-A です。ngspice はそれをコンパイルした OSDI ファイルとして読み込み、そのファイルは PDK を
入れたあとに一度だけ作ります。それまでは、シミュレーションが
`ihp-sg13g2's Verilog-A models are not compiled (…): run ./setup.sh osdi (OpenVAF), then simulate again` で止まります。

**対処。**

```sh
./setup.sh osdi
```

これは OpenVAF をダウンロードし、コンパイルしたモデルを Undine のストア `~/.undine/store/build/<commit>/<variant>/osdi/` に書き出します。
`FORCE=1 pixi run osdi` でコンパイルし直せます。[IHP SG13G2 の Verilog-A モデル](guide/pdks.md#ihp-sg13g2-verilog-a-models) を見てください。

### `… from the old cache: run pixi run osdi to move them into the store`

**原因。** `pixi run doctor` のコンパイルしたモデルの行の注記です。以前の Undine は IHP のモデルをキャッシュ
`~/.cache/undine/osdi/<PDK id>/` にコンパイルしていました。今はストアに置くものです。シミュレーションはそこにあるものも見つけます。

**対処。** 移します(コンパイルし直しはしません):

```sh
pixi run osdi
```

モデルごとに `moved psp103.osdi from … into …` と表示し、空になった古いディレクトリを消します。

<a id="ngspice-with-osdi-is-missing"></a>
### `ngspice with OSDI` が missing

**原因。** conda-forge の ngspice は OSDI なしでビルドされていて、IHP のモデルにはそれが必要です。`setup.sh` は OSDI 付きの ngspice 47 を
自分でビルドして pixi の環境に入れますが、その手順が実行されなかったか、失敗しました。

**対処。**

```sh
./setup.sh ngspice        # same as: pixi run ngspice
```

数分かかりますが、一度だけです。

<a id="simulations-slow-the-whole-machine-down"></a>
### シミュレーションでマシン全体が遅くなる

**原因。** 既定では、サーバーはコア数から 2 を引いた数(最低 1)のシミュレーションを同時に実行し、それぞれが ngspice のスレッドを 1 つ
使います。

**対処。** サーバーの環境で `UNDINE_SIM_JOBS`(同時に実行するシミュレーションの数。ほかはキューで待ちます)を下げます。共用のマシンでは
`UNDINE_SIM_THREADS` を `1` のままにします。長くかかりすぎる実行は `UNDINE_SIM_TIMEOUT`(既定 3600 秒)で止まります。
[設定](reference/cli.ja.md#configuration-environment) を見てください。

### `two cells named inv are netlisted, a/inv and b/inv … and they differ`

メッセージの全体: `tb: two cells named inv are netlisted, a/inv and b/inv (instance x2 in tb), and they differ: a
netlist has one subcircuit per name, so one would stand for the other. Rename one of them, or place the same cell in
both places`。ネットリストのビュー、シミュレーション、LVS、スペック、スイープから、どのネットリストのバックエンドでも出ます。

**原因。** SPICE のネットリストは、サブサーキットをセルの名前だけで呼びます。ネットリストにするセルが、別々のライブラリにある同じ名前の
2 つのセル(例えば自分の `inv` と依存関係の `inv`)や、最上位のセルと同じ名前の別のライブラリのセルを配置していて、それらが違う行に
ネットリストされます。以前は、先のものが何も言わずに両方の代わりになっていました。同じ行にネットリストされる同じ名前の 2 つのセルは
受け付け、一度だけ書きます。

**対処。** 自分のライブラリにあるほうのセルの名前を変えるか、両方の場所に同じライブラリのセルを配置します。依存関係や PDK のセルは
ここでは名前を変えられないので、自分のほうを変えてください。PDK のデモのページのいくつか(sky130 の `sky130_tests/top`、IHP の
`start_page`)も同じ名前の 2 つのセルを配置しているので、同じ理由で拒否されます。これらは例であって、ネットリストにするセルではありません。

<a id="tool-versions-and-undinelock"></a>
## ツールのバージョンと `undine.lock`

プロジェクトは PDK のバージョンと、KLayout、Xschem、ngspice、Magic、Netgen のバージョンを `undine.lock` に記録しています。
`pixi run python -m undine lib status PROJECT` と、ウィンドウ上部の **固定バージョンとの不一致**(*lock issues*)のバッジが、このマシンで
違うものを一覧します([undine.lock](guide/libraries.ja.md#undinelock))。バージョンや出どころの違うツールは**警告**です(`lib status` では
`~`、ロックのダイアログでは薄い字の行)。不一致には数えず、`lib status` を終了コード 1 にもせず、何も止めません。

### `ngspice: locked 41, here 47 from pixi (…)`

**原因。** Undine が ngspice 47 に移る前に作ったプロジェクトです。エラーではありません。プロジェクトを確認したときの結果と違う結果に
なるかもしれない、と警告が知らせています。

**対処。** 新しいバージョンでプロジェクトの結果を確かめてから、記録します:

```sh
pixi run python -m undine lib lock PROJECT
```

### `klayout: locked 0.28.17, here 0.30.12 from PATH (/usr/bin/klayout)`

**原因。** 多くの場合は更新ではなく、そのツールの別のコピーが先に見つかっています。コマンドラインとサーバーは、`$UNDINE_TOOLS` のある
環境とない環境という、違う環境から起動していることがあります。`from` は実行ファイルの出どころ(`pixi`、`UNDINE_TOOLS`、`undine-tools`、
`env`、`PATH`)、括弧の中のパスはここで見つかった実行ファイルです。format 2 の lock は出どころも記録するので、同じ番号でも別の場所の
ものなら警告します。

**対処。** パスが意図したツールでなければ、サーバーとコマンドを同じ方法で起動します(どちらも `pixi run` で)。意図した更新なら、
上と同じく `lib lock` で記録します。

### `no undine.lock: run python -m undine lib lock PROJECT`

**原因。** プロジェクトにロックファイルがありません(Undine が新しいプロジェクトにロックファイルを書くようになる前に作ったものです)。

**対処。** メッセージにあるコマンドを実行します。

<a id="a-library-dependency-is-unlocked-missing-at-an-other-version-or-modified"></a>
### ライブラリの依存関係が `unlocked`、`missing`、`other version`、`modified`

**原因。** このマシンの[ライブラリの依存関係](guide/libraries.ja.md)が、`undine.lock` の記録と違います: クローンしたあとでまだ入れて
いない、`git pull` のあとで別のコミットになっている、手で編集した、のいずれかです。

**対処。** 依存関係をロックのとおりに入れます:

```sh
pixi run python -m undine lib sync PROJECT
```

<a id="a-library-dependency-is-leftover-not-in-projectjson-still-here"></a>
### ライブラリの依存関係が `leftover`(*project.json にないが残っている*)

メッセージ: `library X: not in project.json, still installed in .undine/deps/X/ and/or locked in undine.lock`。

**原因。** `project.json` がもうその依存関係を指定していない(`git pull`、元に戻す操作、採用した changeset、手での編集で消えた)のに、
そのファイルかロックのエントリがまだここにあります。`project.json` がほかの依存関係を指定している間は、そのシンボルがこのマシンでは
見つかり続け、新しいチェックアウトでは見つかりません。

**対処。** `lib sync` がそのファイルとロックのエントリを削除します。ブラウザでは、固定バージョンとの不一致のバッジをクリックし、
ダイアログが最初に示す **固定されたバージョンの依存ライブラリを導入し、project.json にないものを削除（sync）** を選びます。コマンドラインでは:

```sh
pixi run python -m undine lib sync PROJECT
```

プロジェクトがまだそのライブラリを必要とするなら、代わりに `project.json` に戻します(`lib add`)。

<a id="undinelock-has-format-2-made-by-a-newer-undine"></a>
### `undine.lock has format 3, made by a newer Undine`

**原因。** プロジェクトで作業している誰かが、より新しい Undine で、この Undine が読めない形式の lock を書きました(この Undine が読むのは
format 1 と 2 です。ストアより前の Undine は format 1 しか読まず、ここで `format 2` と言います)。`lib` と `pdk` のコマンド、
`set-pdk`、ロックのダイアログ、HTTP API の依存関係の呼び出しはどれもそれを拒否します。この Undine が lock を書き直して、より新しい
Undine が記録したものを落とさないためです。プロジェクトは何も変わっていません。プロジェクトは今までどおり開けます。

**対処。** Undine を更新してから、プロジェクトで作業します:

```sh
git pull
./setup.sh
```

<a id="undinelock-is-not-valid-json-is-not-a-lock-or-has-format-which-no-undine-writes"></a>
### `undine.lock is not valid JSON`、`is not a lock`、または `has format …, which no Undine writes`

**原因。** lock が手で、またはほかのツールで編集されたか、git のマージが衝突のマーカーを残しました。`format` のない lock は
format 1 として読み、エラーにはなりません。

**対処。** ファイルを直すか git から元に戻し、`lib status` を実行します:

```sh
git checkout undine.lock
pixi run python -m undine lib status PROJECT
```

<a id="adding-a-local-repository-from-the-browser-this-server-takes-no-local-repository-from-the-browser"></a>
### ブラウザからローカルリポジトリを追加する: `this server takes no local repository from the browser`

ほかに: `… is outside the directories this server fetches local repositories from`。

**原因。** ローカルのリポジトリ(`file://`、`/path`、`~/path`)からの[ライブラリの依存関係](guide/libraries.ja.md)を、ブラウザか
HTTP API で追加、更新、同期しようとしましたが、サーバーが `--dep-root` なしで起動されているか、リポジトリがそのディレクトリのどれかの
下にありません。ブラウザからサーバーのディスクにある任意のリポジトリを読ませることはできません。

**対処。** メッセージにある `lib` コマンドをコマンドラインで実行します。コマンドラインはどのローカルのパスも受け付けます。または、
リポジトリより上のディレクトリを指定してサーバーを起動し直します:

```sh
pixi run python -m undine serve --dep-root ~/ip
```

<a id="dependencies-and-the-store"></a>
## 依存関係とストア

<a id="lib-sync-projectjson-asks-for-rev-undinelock-locks-or-not-in-undinelock"></a>
### `lib sync`: `… project.json asks for rev …, undine.lock locks …`(または `not in undine.lock`)

メッセージは `run python -m undine lib lock PROJECT to resolve it, or python -m undine lib update PROJECT NAME; nothing was changed`
で終わります。

**原因。** `lib sync` は `undine.lock` が固定するものをそのままインストールし、何も解決しません。`project.json` が指定する依存関係を
lock がそのとおりに固定していません: `git pull` が別の `rev`、`subdir`、URL の `project.json` を持ってきた(lock は一緒にコミット
されていなかった)、手で依存関係を加えた、依存関係があって lock のないプロジェクト、のどれかです。

**対処。** `project.json` が指定するものを固定してから sync します:

```sh
pixi run python -m undine lib lock PROJECT
pixi run python -m undine lib sync PROJECT
```

`lib lock` はそれらの依存関係だけを取得して固定します。`undine.lock` の変更を確かめ、`project.json` と一緒にコミットしてください。
ブラウザでは、この場合ロックのダイアログが **lock** を先に出します。[sync は lock をそのままインストールする](guide/libraries.ja.md#sync-installs-the-lock-as-it-is)
を見てください。

### `offline, and not in the store: …`

メッセージは依存関係ごとにコミットとハッシュを示し、`run the sync with the network (without --offline or UNDINE_OFFLINE=1) to fetch
it; nothing was changed` で終わります。

**原因。** sync をオフラインで実行しました(`lib sync --offline`、サーバーの環境の `UNDINE_OFFLINE=1`、HTTP の
`{"offline": true}`)。固定したバージョンが、ストアにも、ストアに残したそのリポジトリの写しにもありません: このマシンで一度も
取得していない(またはストアを空にした、`UNDINE_STORE` で動かした)ということです。

**対処。** 一度ネットワークありで sync します。その後はそのバージョンがストアにあります:

```sh
pixi run python -m undine lib sync PROJECT
```

### `could not fetch … @ …: …`

**原因。** 固定したコミットがストアになく、git が取得できませんでした: リモートに届かない、認証を求められた(Undine は入力を求め
ません)、またはそのコミットがリモートから消えた(force-push)。メッセージは依存関係ごとにコミットと git の最後の行を示します。何も
インストールも削除もしていません。

**対処。** URL と自分のアクセス権を確かめます(`git ls-remote URL`)。コミットが消えていれば、依存関係を存在するものに移します:
`pixi run python -m undine lib update PROJECT NAME --rev REV`。

取得の途中で止められた git(時間切れ、サーバーや CLI の強制終了、停電)は、ストアに残したリポジトリの写しに `shallow.lock` などの
ロックファイルを残すことがあります。Undine はその写しに取得する前に毎回これを消します(そのときほかの取得は動いていません)。
なので次の取得は通ります。それでも git が `Unable to create '….lock'` と言うときは、メッセージがそのファイルを示します。

<a id="undinedeps-is-a-link-or-not-a-directory-or-undine-is"></a>
### `.undine/deps is a link or not a directory`(または `.undine is ...`)

**原因。** このプロジェクトの `.undine` か `.undine/deps` がシンボリックリンク(またはファイル)です。たとえば、それをコミットした
リポジトリからチェックアウトしたものです。Undine は依存関係を `.undine/deps/` に入れ、project.json がもう示さないものを消します。
リンクを通すと、リンクの先のディレクトリを置き換えたり消したりしてしまうので、依存関係のコマンドはどれも、何かを読み書きしたり
消したりする前に断ります。`lib status` はこれを問題として報告します。

**対処。** リンク(その先ではなく)を消してから sync します。Undine がディレクトリを作り直します:

```sh
rm PROJECT/.undine/deps            # or: rm PROJECT/.undine
pixi run python -m undine lib sync PROJECT
```

リンクがプロジェクトのリポジトリにあるなら、そこからも消します(`git rm --cached .undine/deps`)。`.undine/` は Undine 自身のもので、
バージョン管理しません。

<a id="dependencies-take-disk-space-in-every-project"></a>
### 依存関係がプロジェクトごとにディスクを使う

**原因。** 依存関係はストアへのハードリンクで入れるのでディスクを使いませんが、それは 1 つのファイルシステムの中だけです。ストア
(`~/.undine/store`)とプロジェクトが別のファイルシステムにあるとき(別のディスクのプロジェクト、マウントした共有、WSL の Windows の
ドライブ)、またはファイルシステムがリンクを断るときは、ファイルごとに(読み取り専用で)何も言わずにコピーします。
`UNDINE_STORE_LINK=copy` でもコピーします。

**対処。** サーバーとシェルの環境で、ストアをプロジェクトと同じファイルシステムに置きます:

```sh
export UNDINE_STORE=/data/undine-store       # a directory on the projects' file system
pixi run python -m undine lib sync PROJECT   # each project once, offline if you like, after the first fetch
```

[ストア](reference/data.ja.md#the-store) を見てください。

### `store verify`: `object …: its content does not match its name`

**原因。** ストアのファイルが変わりました: インストールした依存関係のファイルを書き込めるようにして編集した(それはストアのものと
同じファイルです)か、ディスクが壊しました。`store verify` はそれを消しました(`--dry-run` なら示すだけ)。`lib status` は、それを
使っていた依存関係を `modified` と報告します。書き込めるようにしただけで中身が変わっていないファイル(`chmod u+w`)は壊れたことに
なりません: `store verify` と次のインストールが読み取り専用に戻します。

**対処。** その依存関係を使う各プロジェクトで:

```sh
pixi run python -m undine lib sync PROJECT
```

sync は、ストアに残したリポジトリの写しからそのファイルを取り直すか、取得します。依存関係のファイルは編集しないでください。
ライブラリはそれ自身のリポジトリで変えて、`lib update` します。

### `store gc`: `nothing was removed: make these locks readable (or remove them), then run store gc again`

**原因。** `store gc` は、このマシンのどのプロジェクトも固定しているものを残します。そのうちの `undine.lock` が読めません(マージの
後の衝突のマーカー、より新しい形式、正しくない JSON)。それがないとそのプロジェクトに何が要るか分からないので、何も消しません。
メッセージの上の `!` の行が、それぞれの lock を示します。

**対処。** 示された lock を直すか、元に戻す(`git checkout undine.lock`)か、もう要らないプロジェクトを消してから、もう一度
`pixi run python -m undine store gc` を実行します(先に `--dry-run` で消すものを見ておけます)。

<a id="packages-and-trust"></a>
## パッケージと信頼

`lib add`、`lib update`、`lib lock`、`set-pdk` は、まずすべての[パッケージ](guide/libraries.ja.md#packages)を確かめ、すべての問題を
挙げて一度に拒否します。最後は `nothing was changed` です(いくつもあれば `N problems with the dependencies:` と、問題ごとに `- ` の
行)。HTTP では、フォームの上のダイアログに問題を並べます(コード `resolution_refused`)。

### `…: two versions are required, … and …`

メッセージの全体: `sar_common: two versions are required, demo -> analog_ip 1.2.0 (v1.2.0 @ 229914698b1f) ->
sar_common 1.3.1 (v1.3.1 @ 5b1c0e2a9d11) and demo -> sar_common 1.2.0 (v1.2.0 @ 0c4e2b7a1f30): align the revs (one
version per package)`。

**原因。** 2 つの要求が、1 つのパッケージに別々のツリーを求めています: ここでは、プロジェクトが `sar_common` を `v1.2.0` で指定し、
`analog_ip` はそれを `v1.3.1` で必要としています。ネットリストとレイアウトはセルを名前だけで呼ぶので、プロジェクトに入るのは
パッケージごとに 1 つのバージョンです。

**対処。** 要求が同じバージョンを指すようにします: 自分のほうを動かす(`lib update demo sar_common --rev v1.3.1`)か、別のものを
必要としているパッケージを、合うバージョンに動かします。それぞれの経路が、求める `rev` とコミットを示しています。

### `… requires … ^1.3, but … is 1.2.0`

**原因。** マニフェストの `version` の要求が、その `rev` が指すツリーの [package] のバージョンに合いません(`version` はラベルを
確かめ、何をインストールするかは `rev` が決めます)。

**対処。** 要求する側のパッケージで、`rev` を合うバージョンに向けるか、`version` を変えます。要求があなたのもののように見えても
(`project.json` には `version` がありません)、それはあなたが依存するパッケージから来ています: そのパッケージを更新してください。

### `comparator: analog_ip 1.2.0 and sar_common 1.3.1`

**原因。** 2 つのパッケージ、またはパッケージとあなたのライブラリや PDK のライブラリが、同じ名前のセルを持っています。ネットリストは
名前ごとにサブサーキットを 1 つ、レイアウトは名前ごとにセルを 1 つしか持てないので、両方をプロジェクトに入れることはできません
([セルの名前は重ならない](guide/libraries.ja.md#cell-names-are-disjoint))。名前は大文字と小文字を区別せずに比べます。パッケージの
セルを配置したときにあなたのライブラリにできたレイアウトのコピーは数えません。

**対処。** あなたのライブラリとの重なりなら、自分のセルの名前を変えます。2 つのパッケージの間なら、どちらかのセルの名前を変えた
バージョンを使うか、その作者に頼みます。`lib status` は、プロジェクトのセルとのこのような重なりを問題として報告します(`...: a
netlist has one subcircuit per name: rename one of them`)。

**通常の** git の依存関係(マニフェストなし)は拒否しません: `lib status` は、そのセルと同じ名前のプロジェクトのセルを注記するだけで
(`inv: ip (git 229914698b1f) and project library mylib: ...`)、両方を配置するテストベンチはネットリストのときに拒否されます
([下記](#two-cells-named-inv-are-netlisted-ainv-and-binv-and-they-differ))。

### `…: one local name for two packages, … and …`

**原因。** ローカル名はプロジェクト全体で共通です: 1 つのディレクトリ `.undine/deps/<名前>/` がすべての回路図に使われます。2 つの
ライブラリ(またはプロジェクトとライブラリ)が、大文字と小文字を区別せずに同じ名前を、別々のパッケージに与えています。たとえば
あなたの `bias` は `bias_gen` で、あるパッケージの `bias` は別のパッケージです。

**対処。** どちらかに別のローカル名を与えます: 自分のものなら `lib remove` してから別の名前で `lib add`、パッケージのものならその
マニフェストの `[dependencies]` で(新しいバージョンで)。パッケージは自身の [package] の名前でもインストールされるので、その名前も
使われています([ローカル名と別名](guide/libraries.ja.md#local-names-and-aliases))。

<a id="has-that-name-and-undinedeps-would-shadow-it-a-reserved-name"></a>
### `… has that name, and .undine/deps would shadow it`(予約された名前)

メッセージの全体: `sky130_fd_pr: the PDK library sky130_fd_pr has that name, and .undine/deps would shadow it: choose
another local name; nothing was changed`。マニフェストが与える名前なら: `...: ... installs it under this local name, which
is ...: a dependency may not shadow it; rename it in the requirer's [dependencies]`。

**原因。** `.undine/deps/` はシンボルの検索パスの先頭にあるので、プロジェクトのライブラリ、PDK のライブラリ、`devices`、Xschem の
ライブラリや `symbol_paths` の中のフォルダと同じ名前の依存関係は、すべての回路図でそのライブラリと入れ替わってしまいます。

**対処。** 別のローカル名を選びます(`lib add PROJECT NAME ...`)。パッケージのマニフェストが与える名前なら、そのパッケージが
変える必要があります。以前から `project.json` にある名前はそのまま使え、`lib status` が隠しているライブラリを注記します。

### `… does not fit this project's PDK: …`

メッセージの全体は、パッケージ、理由、それを求める経路を示します: `sar_common 1.3.1 does not fit this project's PDK:
sky130A@0c4e2b7a1f30 is not among commits 8afc8346a57f (required by demo -> analog_ip 1.2.0 (...) -> sar_common 1.3.1
(...))`。

**原因。** パッケージの `[pdk]` が、`undine.lock` が固定する PDK を受け入れません: 別のファミリかバリアント、並べていない
コミット、PDK のないプロジェクト、コミットを確かめられないバージョンのない PDK
([PDK との互換性](guide/libraries.ja.md#pdk-compatibility))。`tag ... is not known to this Undine`: パッケージが互換性のタグだけを
示していて、この Undine はまだそれを解決できません。

**対処。** あなたの PDK のコミットを並べたバージョンのパッケージを使うか、それが受け入れる PDK にプロジェクトを移します
(`set-pdk`)。バージョンのない PDK は、ciel で入れます(`pdk fetch`)。タグだけを示すパッケージは、作者がコミットを `commits` に
並べる必要があります。

### `the PDK … is not accepted by N locked packages`

**原因。** 固定済みのパッケージが受け入れない PDK への `set-pdk`(または PDK のバッジのダイアログでの切り替え)です。続く行が、
それぞれのパッケージを経路と理由とともに示します。何も変えていません。

**対処。** 先にそれらのパッケージを新しい PDK を受け入れるバージョンに動かす(`lib update`)か、削除し、それから PDK を移します。
[プロジェクトを別の PDK バージョンへ移す](guide/pdks.ja.md#moving-a-project-to-another-pdk-version)を参照してください。

`pdk verify --pin` や `lib lock --format 2` による固定は、入っているものの記録なので拒否しません: どちらもそのようなパッケージ
ごとに `!` の行を表示し、その後は、パッケージか PDK を動かすまで、`lib status` がそのようなパッケージごとに `! ...: does not fit the PDK ...` を表示します。

### `…: not in the store or installed: run lib sync first`

**原因。** 固定済みのパッケージを確かめるコマンド(`lib add`、`lib update`、`lib lock`、`set-pdk`)は、それぞれのマニフェストを
ストアかプロジェクトのインストールから読み、ネットワークからは読みません。クローンした直後は、どちらにもまだありません。HTTP では
コード `not_synced` です。`set-pdk` では、そのパッケージは拒否の中の 1 行になります。

**対処。** 固定したものをインストールしてから、もう一度コマンドを実行します:

```sh
pixi run python -m undine lib sync PROJECT
```

### `… needs lock format 2: run python -m undine lib lock PROJECT --format 2 first`

メッセージの全体: `analog_ip: package analog_ip names dependencies (sar_common, bias): needs lock format 2: ...; nothing was
changed`、または `...: package bias_gen is also installed under its own name bias_gen: ...`。

**原因。** 依存関係を持つパッケージや、自身の名前以外のローカル名でインストールするパッケージには、lock の format 2 の
`packages`、`names`、`deps` のキーが必要です。プロジェクトの lock は format 1 です(`new` が作るプロジェクトはすべてそうして始まります)。

**対処。** 一度 lock を変換してから、もう一度コマンドを実行します:

```sh
pixi run python -m undine lib lock PROJECT --format 2
```

ここに入っている PDK を固定する(なければ先に取得してください)ので、プロジェクトの全員がこのバージョンの Undine を使う必要が
あります。lock 自体がそのようなパッケージを持っているとき(pull の後)は、`lib lock --format 2` が変換と解決を一度に行います。
ボタンはなく、コマンドラインだけです。

<a id="an-index-requirement-or-a-version-range"></a>
### インデックスの要求、またはバージョンの範囲

メッセージ: `...: undine-package.toml: dependencies.sar_common: an index requirement (sar_common ^1.2), which comes with
the package index (phase 3): name its git repository, sar_common = { git = "https://...", rev = "v1.2.0", version =
"^1.2" }`、または `...: undine-package.toml: dependencies.sar_common.version: '>=1.2': version ranges come with the
package index (phase 3): use ^, ~ or =`。

**原因。** マニフェストが、Undine にまだないパッケージのインデックスの書き方を使っています: バージョンの文字列や `index` で示した
依存関係、または比較演算子(`>=`、`<`、`!=`)やカンマを含むバージョンの要求です。

**対処。** 依存関係を git のテーブル `{ git = "...", rev = "...", version = "^1.2" }` で書き(メッセージが書くものを示します)、
要求には `^`、`~`、`=`、または `^` のない `MAJOR.MINOR` を使います。ほかの人が公開するパッケージなら、その作者に頼むか、そう
書いているバージョンを使います。

<a id="undine-packagetoml-is-refused"></a>
### `undine-package.toml` が拒否される

メッセージは `<where>: undine-package.toml: <key>: <what>: <fix>` の形です。例: `... @ 229914698b1f:lib: undine-package.toml:
package.version: '1.2' is not semver: MAJOR.MINOR.PATCH, e.g. 1.2.0`。続いて、それを求めた経路が付きます。

**原因。** パッケージのマニフェストが Undine の読める形ではありません: 正しい TOML や UTF-8 でない、256 KiB より大きい、
シンボリックリンク、Undine の知らないキーやテーブル(`unknown key (made for a newer Undine?)`。`revision =` のような書き間違いも
これで分かります)、種類の違う値、`[a-z][a-z0-9_]*` でない名前、前方だけのコミット、パッケージの外に出る `subdir` や include の
パターン、最上位のセルを 1 つも選ばない `[files] include`、ファイルの中にない公開セル。

**対処。** 自分のパッケージなら、メッセージが示す行を直してコミットし、`rev` をそのコミットに向けます。ほかの人のものなら、
Undine が読めるマニフェストのバージョンを使うか、作者に頼みます。すべてのキーは[マニフェスト](guide/libraries.ja.md#the-manifest)
にあります。

同じ確認から出る関連のもの: `... has no undine-package.toml: a package may depend only on packages`(マニフェストが通常の git の
リポジトリを指定している: できるのは `project.json` だけ)、`... asks for package X, but ... is Y`(指定した `package` や `--package`
が、ツリーが持つものと違う)、`... is a path on this machine, and ... comes from ...`(リモートのリポジトリのパッケージがローカルの
ものを指定している: 公開者に git の URL を頼む)。

### `…: … is a dependency of this project already, as …`

メッセージの全体: `bias2: bias_gen 0.3.0 is a dependency of this project already, as bias: one package has one name in
project.json; use bias; nothing was changed`。

**原因。** `project.json` が別のローカル名ですでに指定しているパッケージへの `lib add` です。パッケージは `undine.lock` に 1 つの
エントリを持つので、`project.json` での名前も 1 つです。自身の名前や、パッケージが与える名前の下には、いずれにせよ一緒に入ります。

**対処。** 今ある名前でセルを配置する(`bias/<cell>.sym`)か、その名前を `lib remove` して新しい名前で追加します。

### `library IP exists (as ip)`、または `IP and ip: one local name spelled two ways`

**原因。** 既存のライブラリ(自身のもの、依存関係、パッケージが入っている名前)と大文字小文字だけが違うローカル名です。大文字
小文字を区別しないファイルシステム(macOS、Windows)では両方が 1 つのディレクトリなので、片方を削除するともう片方のインストール
も消えます。

**対処。** すでにある綴りの名前を使うか、別の名前を選びます。マニフェストがパッケージの名前を 2 通りに綴っているなら、その
パッケージが直す必要があります。

### `note: …: locked before packages had dependencies; … resolves it`

注記の全体: `ip: package analog_ip, which undine.lock does not install under its own name: locked before packages had
dependencies; ...`、または format 1 の lock では `...: its undine-package.toml names dependencies (...) that undine.lock does not
lock: ...`。

**原因。** 古い Undine が固定した依存関係(エントリに `package` がない)で、そのマニフェストがエントリの入れるもの以上を求めて
います。固定したとおりに動き続け、ほかの `lib` コマンドはそれを変えも確かめもしません。パッケージ自身の名前
(`analog_ip/<cell>.sym`)や、それが指定する依存関係を配置する回路図は、解決するまで解決できません。format 2 の lock では、
固定していない依存関係は注記ではなく問題です。

**対処。** 準備ができたら解決します: `lib update PROJECT NAME`、または `lib lock PROJECT`(format 1 の lock では
`lib lock PROJECT --format 2`)。

### `…: …: pin name '…' holds a space or a line break, which a netlist line cannot carry`

**原因。** シンボルのピン名(または回路図のポートのラベル)が空白か改行を含んでいます。それは `.subckt` の行の語になるので、
改行があるとネットリストに独自のカードを書き込むことになります。ネットリストは拒否されます。

**対処。** シンボルのピン(とその回路図のラベル)の名前を変えます。依存関係のシンボルなら、その作者に頼みます。

### `…: placed by …; update or remove that instead`

**原因。** パッケージだけが配置している名前への `lib update` や `lib remove` です: それはプロジェクトの依存関係ではなく、示された
パッケージの依存関係です。

**対処。** そのパッケージを更新または削除します。`lib remove` は、もう何も必要としないパッケージを一緒に削除し、ほかの依存関係が
まだ配置しているものは残して示します(`sar_common: still placed by analog_ip`)。

<a id="dependency-not-trusted-a-simulation-is-refused"></a>
### `… (dependency …, not trusted): …`(シミュレーションが拒否される)

メッセージの全体は、見つかったものすべてと、すべきことを示します: `ip/opamp (dependency analog_ip 1.2.0, not trusted): code
block s1 line 1: .control; a person can trust analog_ip@1.2.0 in this project (undine lib trust demo ip, or Trust... in
Undine); an agent cannot`。マニフェストのない依存関係では、ラベルは `git <commit>` です。

**原因。** シミュレーション(またはスイープ、仕様の確認)が、コマンドを実行する(`.control` ブロック、`*#` の行)か、PDK の外の
ファイルを読む(絶対パス、`..`、`$`、`~` を含む `.include` / `.lib` / `file=`)[依存関係](guide/libraries.ja.md#trusting-a-dependencys-code)
のセルを使っていて、このプロジェクトでその依存関係を人が信頼していません。これは**通常の git の依存関係にも**当てはまります:
依存関係のセルに `.control` ブロックや絶対パスの `.include` があり、以前はシミュレーションできたプロジェクトも、今は信頼が必要です。

**対処。** 見つかったものを読み、実行されることを受け入れるなら、そのバージョンを信頼します: 拒否の中の **信頼...**(または
ライブラリの行の盾のボタン)か、

```sh
pixi run python -m undine lib trust PROJECT NAME
```

別のバージョンへの更新は、信頼されるまでまた拒否されます。または、そのような内容のないバージョンの依存関係を使うか、そのセルを
テストベンチから外します。エージェントは信頼できません: 拒否を伝えてあなたに尋ねます。`lib trust` が `has no install record` と
言う依存関係は Undine が入れたものではありません: `lib sync` が削除するか入れ直します。

信頼のダイアログの **信頼する** が `what NAME holds changed since it was confirmed ...: confirm the trust again` と言うことが
あります: その間に sync や update がファイルを入れ替えました。ダイアログを開き直してください。

### RTL: `… (dependency …, not trusted): line …: $c(…)`

**原因。** C++ やシェルのコード(`$c(...)`、`` `systemc_* ``、`$system(...)`、DPI の import、それらを組み立てる `` `define ``)を
持つか、絶対パスや `..` でファイルを読み書きする(`` `include ``、`$fopen`、`$dumpfile`、`$readmem*`、`$writemem*`)依存関係の RTL での
**Simulate**(Verilator)です。**Lint** が拒否するのは、そのような `` `include `` のパスだけです。依存関係の RTL のファイルは
すべて読みます。

**対処。** コードが実行されることを受け入れるなら、上と同じように依存関係を信頼します。または、それのないバージョンを使います。
[依存関係の RTL](guide/rtl.ja.md#rtl-from-a-dependency) を参照してください。

<a id="the-xschem-netlister-dependency-not-trusted-tcleval"></a>
### Xschem のネットリスタ: `… (dependency …, not trusted): … tcleval(…)`

**原因。** **Xschem** バックエンドでの netlist ビューは Xschem 自身のネットリスタを実行し、それは読むどのシンボルでも、属性の
`tcleval(...)` を Tcl として評価します。そのため、プロジェクトが信頼していないインストール済みの依存関係のどれかが `.sch` や
`.sym` にそれを持っていれば、この回路図がそれを配置しているかどうかにかかわらず拒否します。

**対処。** (Tcl を受け入れるなら)依存関係を信頼するか、Tcl を決して実行せず警告するだけの Undine 自身のネットリスタを使います
(`not trusted: would be refused in a simulation: ...`)。

<a id="lvs-dependency-not-trusted-when-the-run-starts"></a>
### LVS: 実行を始めるときの `… (dependency …, not trusted): …`

**原因。** LVS の参照ネットリスト(KLayout、または Magic + Netgen)に、PDK の外の `.include` や `.lib` のパスを持つ依存関係のセルの
行があり、その依存関係を人が信頼していません。LVS は制御コードを実行しないので、パスだけを、実行を始めるときにだけ確かめます:
サインオフの状態の表示や、結果の再利用は拒否しません。

**対処。** (上と同じように)依存関係を信頼し、もう一度実行を始めます。実行を待ち行列に入れた後に与えた信頼も数えます。

<a id="server-ports-and-restarts"></a>
## サーバー、ポート、再起動

### `[Errno 98] … address already in use`

**原因。** ほかのプロセスがそのポート(既定は 8700)で待ち受けています。多くは、すでに動いている Undine です。`undine mcp-stdio` で
Undine を起動する AI アプリは、チャットが終わっても Undine を動かしたままにします。サーバーは終了コード 3 で終わります。

**対処。** それが Undine かどうかを確かめます:

```sh
curl -s http://127.0.0.1:8700/api/health
```

Undine なら、`"undine": true`、プロジェクトのルート、MCP が有効かどうかを返します。そのサーバーを使うか、止めるか、自分のサーバーを
別のポートで起動します:

```sh
pixi run python -m undine serve --port 8710
```

止めるには、そのプロセスを見つけて終了させます。サーバーのコマンドラインには `serve` が含まれます。`mcp-stdio` を含む行は AI アプリの
起動役で、チャットとともに終わります:

```sh
pgrep -af undine          # Undine processes, with their command lines
kill PID                  # PID of the server's line
```

`./scripts/restart.sh` は、AI アプリが起動したサーバーを止めません(次の項目を見てください)。

<a id="scriptsrestartsh-stopped-another-undine-or-did-not-stop-one"></a>
### `./scripts/restart.sh` が別の Undine を止めた、または止めなかった

**原因。** `restart.sh` は新しいサーバーを起動する前に、コマンドラインに `python -m undine serve` を含むマシン上のプロセスを、ポートや
プロジェクトのルートにかかわらず、すべて止めます。これは `pixi run serve` や `restart.sh` 自身が起動したサーバーに当てはまります。
`python -m undine --root … serve` や `--pdk-root … serve` のように、`serve` の前にグローバルオプションを付けて起動したサーバーには
当てはまらず、AI アプリが起動するサーバーはすべてこちらです。そのようなサーバーはポートを持ち続け、新しいサーバーは
`[Errno 98] … address already in use` で終わります。それでも `restart.sh` は `server: 200` と表示します。古いサーバーが確認に答えるからです。

**対処。** 1 台のマシンで複数の Undine を動かすときは、`restart.sh` ではなく `pixi run python -m undine serve --port …` で自分で起動し、
自分で止めます。AI アプリが起動したサーバーを置き換えるには、[`[Errno 98]`](#errno-98-address-already-in-use) のとおりにそれを止めてから
`restart.sh` を実行し(アプリが MCP を必要とするなら `UNDINE_MCP=1` 付きで)、`server: 200` を信じるのではなくログ(`/tmp/undine.log`)を
読みます。

<a id="i-cannot-open-undine-from-another-computer"></a>
### 別のコンピューターから Undine を開けない

**原因。** サーバーは既定で `127.0.0.1` で待ち受けるので、サーバーが動いているマシンからしかつながりません。

**対処。** SSH でポートを転送し、手元のコンピューターで `http://127.0.0.1:8700` を開きます:

```sh
ssh -L 8700:127.0.0.1:8700 me@eda-server
```

!!! warning
    UI と HTTP API にはサインインがありません。トークンを確かめるのは MCP のエンドポイント(`/mcp`)だけです。ポートに届く人は誰でも、
    サーバーのすべてのプロジェクトを読み、変更できます。信頼できないネットワークでは `--host 0.0.0.0` で起動しないでください。AI アプリには、
    起動役の `--ssh HOST` がポートを転送します([Undine が別のマシンにある場合](guide/assistant.ja.md#undine-on-another-machine))。

### `another server still holds the simulations of …`

**原因。** 同じプロジェクトのルートで、最初のサーバーがまだ動いている(またはまだ止まる途中の)間に 2 つめのサーバーを起動しました。
1 つのルートのシミュレーションを扱うサーバーは、一度に 1 つだけです。

**対処。** 最初のサーバーがもうすぐ止まるのなら、何もしなくてかまいません。止まれば、新しいサーバーがシミュレーションを引き継ぎます。
そうでなければ、どちらかを止めます。

<a id="ai-agents-and-mcp"></a>
## AI エージェントと MCP

すべての一覧は、AI エージェントをつなぐの [困ったときは](getting-started/connect-ai.ja.md#troubleshooting) にあります。よくあるのは
次の場合です:

### `Undine at … runs without MCP`

**原因。** AI アプリの起動役が動いている Undine を見つけましたが、それは MCP なしで起動されていました。起動役はそれを再起動しません。

**対処。** それを止め、MCP を有効にして起動し直します:

```sh
UNDINE_MCP=1 pixi run serve          # or: UNDINE_MCP=1 ./scripts/restart.sh
```

<a id="claude-code-does-not-show-undines-tools-or-only-open_in_undine"></a>
### Claude Code に Undine のツールが出ない、または `open_in_undine` しかない

**原因。** Undine を登録する前に始めたセッションである、Undine を 1 つのフォルダにだけ登録した(`--scope user` なし)、または起動役が
サーバーにつながりませんでした。

**対処。** 新しいセッションを始めます。`claude mcp list` で `✔ Connected` になっているか確かめます。ツールが 1 つのフォルダでしか
使えないなら、`--scope user` 付きで登録し直します([Claude Code](getting-started/connect-ai.ja.md#claude-code))。サーバーが起動しないなら、
`~/.undine/serve.log` を読みます。

### `… is editing this changeset. It is read only until it finishes.`

**原因。** エージェントがまだこの changeset に書き込んでいます。書き込んでいる間、changeset はそのエージェントだけのものです。日本語の
UI では「…がこの変更セットを編集中です。完了まで読み取り専用です。」と表示されます。

**対処。** 待つか、レビューで **停止**(Stop)をクリックします。チャットを閉じればすぐに、異常終了した場合は 2 分後に解除されます。

### `the server policy does not allow … connections`

**原因。** サーバーが `--llm-policy`(または `UNDINE_LLM_POLICY`)付きで起動されていて、使える LLM の接続が制限されています。
`--llm-policy none` では MCP も無効です。日本語の UI では「サーバーのポリシーでは … 接続を許可していません」と表示されます。

**対処。** サーバーを動かしている人に確認するか、必要なポリシーで起動します
([設計エージェント(アプリ内)](guide/assistant.ja.md#agent-in-app))。

<a id="changesets-and-adopting"></a>
## changeset と採用

[changeset](guide/changesets.ja.md) は、プロジェクトの git ブランチ上の提案です。**採用**(Adopt)は、それを設計に全か無かでマージします。
採用できなかったときは何も書き込まず、設計は元のままです。

### `Wait for running checks before adopting or discarding.`

日本語の UI では「採用・破棄する前に、実行中の検証の完了を待ってください。」です。

**原因。** 提案またはその起点で、ジョブ(DRC、LVS、シミュレーションの実行)が待機中か実行中です。ツールが結果を書いている途中で採用すると、
その半分だけを保存することになりかねません。

**対処。** ジョブが終わるのを待つか、取り消します。前のサーバーが実行したままにしたシミュレーションも、終わるまで採用を止めます。

### `Not adopted: … conflict with edits made since the proposal was opened.`

日本語の UI では「採用できませんでした: 提案の作成後に加えられた編集と競合する変更が … 個のファイルにあります。」です。

**原因。** あるオブジェクト(インスタンス、配線、図形、設計の意図の項目)が、設計と提案の両方で、違う値に変えられています。レビューには
ファイルごとに衝突が表示され、それぞれ今の設計の値、提案の値、提案を開いたときの値が示されます。

**対処。** それらのオブジェクトを両側で一致させてから、もう一度採用します。設計側で変える(提案を開いたときの値に戻すと、提案の値が
入ります)か、提案側で変えます(プレビューで手で直すか、**修正を依頼**(Request changes))。または提案を破棄します。提案はブランチに
残ります。[採用: マージ](guide/changesets.ja.md#adopting-the-merge) を見てください。

### `Not adopted: … changed in the design while the proposal was being adopted.`

日本語の UI では「採用できませんでした: 採用の処理中に … 個のファイルが設計側で変更されました。」です。

**原因。** ほかのプログラム(エディタ、`git pull`)が、採用の最中に設計のファイルを書きました。

**対処。** もう一度採用します。マージは今の設計から、その編集も含めて作り直されます。

### `The … file cannot be read, so it cannot be merged`

日本語の UI では「…のファイルを読み込めないため、統合できません」です。

**原因。** マージに必要なファイル(設計の意図や netlist の YAML など)が壊れています。

**対処。** ファイルを直してから(読み込めない理由はレビューに表示されます)、もう一度採用します。

### `waiting for “…” (adopting) to finish`

日本語の UI では「「…」(採用中)の完了を待っています」です。

**原因。** changeset は一度に 1 つずつ閉じます。ほかの changeset を採用、破棄、取り消ししている最中で、数 GB の実行結果の証拠があると
時間がかかることがあります。

**対処。** 何もしなくてかまいません。ほかの処理が終われば、この changeset の処理が続きます。ほかの処理の進み具合はレビューに表示されます。

<a id="adopted-but-the-merge-was-left-uncommitted"></a>
### 採用したが、`the merge was left uncommitted`

**原因。** Undine がマージをコミットするのは、プロジェクトにほかに未コミットのものがなかったときだけです。どの場合かはメッセージが
示します:

| メッセージの終わり | 対処 |
|---|---|
| `the project had other uncommitted changes` | 準備ができたら git でマージをコミットします。未コミットのものは履歴パネルに表示されます |
| `the repository had no commit yet` | 履歴パネルで **最初のコミットを作成**(Make the first commit)をクリックします。マージもそれに含まれ、以降の採用はコミットされます |
| `its commit failed` | git でコミットします |

日本語の UI では、レビューに「提案を採用しました。…」で始まる同じ内容の表示が出ます。

<a id="opening-a-changeset-is-refused-is-in-the-git-repository-which-is-not-set-up-for-undine"></a>
### changeset を開くのを断られる: `… is in the git repository …, which is not set up for Undine`

**原因。** プロジェクトが、誰も Undine 用にセットアップしていない git リポジトリの中にあります。git で管理しているホームディレクトリなど
です。Undine はそのようなリポジトリにブランチや設定を書き込みません。日本語の UI では「このプロジェクトは git リポジトリ … の中にありますが、
このリポジトリは Undine 用にセットアップされていません。…」と表示されます。

**対処。** 履歴パネルの **git をセットアップ**(Set up git)か、メッセージにあるコマンドでセットアップするか、プロジェクトをその
リポジトリの外に移します:

```sh
pixi run python -m undine git-setup PROJECT
```

<a id="opening-a-changeset-is-refused-because-of-a-library"></a>
### ライブラリのせいで changeset を開くのを断られる

**原因。** プロジェクトのライブラリが、プロジェクトの外を指す(またはどこも指さない)シンボリックリンク、絶対パスのリンク、git が
保持しないリンク、または独自の git リポジトリです。changeset の作業コピー(git の worktree)にはそれを入れられません。

**対処。** **新しい変更セット**(New changeset)のダイアログが、そのライブラリとパスを示し、どうすればよいかを表示します。ファイルそのものを
プロジェクトに入れるか、ライブラリを[依存関係](guide/libraries.ja.md#dependencies)にすれば、必ず解決します。
[changeset は git のブランチ](guide/changesets.ja.md#a-changeset-is-a-git-branch) を見てください。

<a id="undine-stopped-in-the-middle-of-an-adopt"></a>
### 採用の途中で Undine が止まった

**原因。** マージを書いている途中で、サーバーが止まりました(再起動、異常終了)。

**対処。** もう一度起動します。起動すると、UI やクライアントに答える前に、マージを終えるか元に戻し(その後にあなたが行ったことは
残します)、`the interrupted merge of the changeset … was finished or taken back` と表示します。元に戻した場合はレビューがそう表示するので、
もう一度採用してください。[Undine が途中で止まったとき](guide/changesets.ja.md#if-undine-stops-in-the-middle) を見てください。

<a id="verification"></a>
## 検証

### LVS: `pin VDD: connected to nothing inside the schematic and no VDD label in the layout's top cell`

**原因。** 回路図のポートが、セルの中で何にもつながっていません(シンボルのためにあるインターフェースのピン。CDAC の `vdd` など)。LVS
デッキは素子のないネットを取り除くので、レイアウトにはそれと照合するピンがありません。

**対処。** レイアウトのトップセルに、その名前のラベルを、ピンのラベルのレイヤー(`met1.label` …)で、そのメタルの図形の上に置きます。
すると Undine はそのポートを受け入れ、LVS のまとめの `unused_pins` に挙げます。レイアウトがすでに使っているメタルの上にはラベルを
置かないでください: `pin VDD: connected to nothing in the schematic but its label is on the layout net …` もエラーです。別のネットを
セルのポートにしてしまうからです。[検証](guide/verification.ja.md) を見てください。

### LVS: `pin VDPWR: no VDPWR label on the layout net it matched ($12)`

**原因。** 回路図のトップセルのピンが、照合したネットでレイアウトのトップセルのピンになっていません。KLayout の比較だけなら受け入れて
しまうので、ラベルを付けたストライプから切り離された電源でも「一致」しかねません。Undine はこれを不一致として報告します。

**対処。** レイアウトのトップセルで、そのネットが実際に使っている図形の上に、ピンの名前でラベルを付けます。

### LVS: `tie cell … joins the ports … and …`

**原因。** レイアウトでは `conb_1` の HI と LO の出力は電源なので、ポートにつないだタイセルはそのポートを別のポートとつないでしまい、
LVS は 1 つのネットを 2 つのピンと比べられません。

**対処。** 定数のポートに自分のドライバ(タイセルのネットからのバッファ)を付けるか、そのポートをなくします。[検証](guide/verification.ja.md)
の *LVS の標準セル(sky130)* を見てください。

### `LibreLane is not available`

**原因。** Undine が探す場所に LibreLane が入っていません。探す場所は、`PATH` 上の `librelane` か、Nix のある `~/librelane` の
LibreLane のチェックアウトです。

**対処。** LibreLane のガイドのとおりに Nix で入れ、`~/librelane` にクローンしてスモークテストを一度実行するか、それを実行するコマンドを
`UNDINE_LIBRELANE` に設定します。[LibreLane: RTL から GDS へ](guide/rtl.ja.md#librelane-rtl-to-gds) を見てください。

### `Verilator is not installed`

**原因。** RTL ビューのリントとシミュレーションには Verilator が必要で、`pixi install` がそれを入れます。

**対処。** `pixi install` を実行するか、`verilator` を `PATH` に置きます([Verilator のインストール](guide/rtl.ja.md#installing-verilator))。

## git

<a id="the-commit-is-refused-library-has-layout-edits-not-saved-to-its-cell-files"></a>
### コミットを断られる: `library … has layout edits not saved to its cell files`

**原因。** `git-setup` が入れる pre-commit フックは、ステージした操作ログに、セルのファイルに保存していないレイアウトの編集がある
コミットを断ります。

**対処。** Undine でレイアウトを保存し、セルのファイルとログを `git add` して、もう一度コミットします。
[コミットの前にレイアウトを保存する](reference/data.ja.md#save-layouts-before-committing) を見てください。

### `project.json cannot be read (…): a git merge may have left conflict markers in it`

**原因。** git のマージがプロジェクトの `project.json` に衝突マーカーを残したため、正しい JSON ではなくなっています。プロジェクトの一覧に、
このエラーとともに表示されます。

**対処。** ファイルの衝突を解決し(履歴パネルに表示されます)、再読み込みします。

<a id="a-git-merge-stopped-on-a-cell-file"></a>
### git のマージがセルのファイルで止まった

**原因。** 両方のブランチが、回路図、レイアウト、設計の意図、操作ログの同じオブジェクトを、違うように変えました。

**対処。** Undine の環境のシェルを開き、プロジェクトに移って、残っている衝突を一覧し、それぞれでどちらかを選びます(マージが ID と、
続けるためのコマンドを表示します):

```sh
cd ~/undine-eda && pixi shell
cd ~/undine-projects/PROJECT
python -m undine merge-check
python -m undine resolve PATH --theirs ID
git add :/PATH
```

履歴パネルにも、同じ衝突と、DRC / LVS で確認し直すべきセルが表示されます。
[git の履歴](guide/history.ja.md#merges-and-the-cells-to-re-check) と [意味的な差分とマージ](design/merge.ja.md) を見てください。

## WSL2

Undine は WSL2 でも Linux と同じように動きます。WSL2 の Ubuntu 22.04 で動作確認しています。いくつか違う点があります:

- **プロジェクトは Linux のファイルシステムに置きます。** 既定のプロジェクトのルート `~/undine-projects` はそこにあります。WSL から見た
  Windows のドライブ(`/mnt/c/...`)は大文字と小文字を区別しないので、大文字と小文字だけが違う名前のセルを保存できません
  ([レイアウトのセルのファイル](reference/data.ja.md#layout-cell-files))。
- **Undine のコマンドは WSL のターミナルで実行します。** PowerShell やコマンドプロンプトではありません。`claude mcp add` もそうです。
- **ブラウザは Windows のものです。** AI アプリが作業を始めると、レビューは Windows のブラウザで開きます。
- **WSL の中のファイルを Windows から見る。** `pixi run mcpb` で作った Claude Desktop の拡張は、Windows のエクスプローラーでは
  `\\wsl$\<distribution>\home\<you>\undine-eda\dist\` にあります。
- **拡張を使わない Claude Desktop** は、Undine の起動役を `wsl.exe` 経由で実行します。設定は
  [AI エージェントをつなぐ](getting-started/connect-ai.ja.md#a3-claude-desktop-without-the-extension) にあります。

<a id="large-projects"></a>
## 大きなプロジェクト

- **レビューの差分が途中で切れる。** **差分**(*Diff*)のモードが描く違いのポリゴンは最大 4000 個で、**ファイルごと**(*By file*)は
  取り込んだ大きなレイアウトだと数秒かかることがあります(一度に 1 つずつ読むので、最初からやり直すのではなく待ちます)。
- **changeset を閉じるたびに証拠が増える。** 採用や破棄をすると、提案の実行結果が証拠として残ります。閉じた changeset のレビューに
  その大きさが表示され、**波形を削除**(Prune waveforms)で生の波形を消せます。実行の記録とハッシュは残ります。
  [採用・破棄の後の証拠](guide/changesets.ja.md#evidence-after-apply-or-discard) を見てください。
- **大きなリポジトリの中のプロジェクト** では、changeset の作業コピーごとにリポジトリ全体をチェックアウトします(スパースチェックアウトは
  まだありません)。大きな設計は専用のリポジトリに置いてください。
- **多くのシミュレーションを同時に実行する。** [シミュレーションでマシン全体が遅くなる](#simulations-slow-the-whole-machine-down) のとおりに、
  `UNDINE_SIM_JOBS` と `UNDINE_SIM_THREADS` を調整します。
- **エージェントへの長い答え** は短くされます(`undine call` が表示するのは最大 `UNDINE_MAX_CHARS` 文字、既定は 25000 です。
  [長い結果](guide/assistant.ja.md#long-results) を見てください)。

## FAQ

<a id="what-is-undine"></a>
### Undine とは何ですか?

Undine は、カスタム IC(アナログ / ミックスドシグナル)設計のための AI ネイティブな Web ベースの EDA 環境です。回路図、シンボル、レイアウト、RTL、
シミュレーション、DRC / LVS、テープアウトをブラウザで行えます。設計のデータベース、PDK、PCell、DRC / LVS、シミュレーションはサーバーで
動き、HTTP API の向こうにあります。ブラウザの UI はその API のクライアントの 1 つで、スクリプトや AI エージェントもクライアントです。
すべての変更は記録され、レビューできます。[概要](overview.ja.md) を見てください。

<a id="what-is-undine-not"></a>
### Undine でないものは何ですか?

- **ホスティングされたサービスではありません。** サーバーは自分のマシンか、自分が管理するマシンで動かします。ユーザーアカウントや
  サインインはありません。
- **Undine が動かすツールの代わりではありません。** Undine は KLayout、Xschem の形式、ngspice、Magic、Netgen、Verilator、LibreLane を
  使います。それらの結果はそれらのツールのものです。Undine は Xschem の `.sch` / `.sym` ファイルを読み書きするので、Xschem でも開けます。
- **完成していません。** Undine はアルファ版です。インターフェース、ファイル形式、HTTP API は、コミットの間でまだ変わることがあります。

<a id="which-operating-systems-does-it-run-on"></a>
### どの OS で動きますか?

サーバーは、glibc 2.28 以上の Linux x86-64、または Windows の WSL2 で動きます。pixi の環境は `linux-64` 用にだけ作っているので、サーバーは
macOS や Windows でネイティブには動きません。ブラウザは、サーバーに届くどのコンピューターにあってもかまいません
([別のコンピューターから Undine を開けない](#i-cannot-open-undine-from-another-computer) を見てください)。

<a id="which-browsers-are-supported"></a>
### どのブラウザに対応していますか?

最新のデスクトップのブラウザです。UI は Vite の既定のターゲット、つまりネイティブの ES モジュールに対応したブラウザ(Chrome 87、
Edge 88、Firefox 78、Safari 14 以降)向けにビルドしています。Undine のスクリーンショットは Chromium で撮っています。

<a id="which-pdks-does-it-support"></a>
### どの PDK に対応していますか?

SkyWater SKY130、GlobalFoundries GF180MCU、IHP SG13G2 です。[ciel](https://github.com/fossi-foundation/ciel) で入れ、それぞれデッキ、
モデル、コーナー、PCell が使えます。ciel が SG13G2 と一緒に入れる SG13CMOS5L は未検証です。DRC は
設定してあり、LVS とシミュレーションは設定してありますが実行したことがなく、PCell はありません。ほかのファミリーの PDK も一覧には
出ますが、[プロファイル](guide/pdks.md#pdk-profiles) が必要な機能は使えません。動作確認したバージョンは
[サポートしている PDK](guide/pdks.md#supported-pdks) に、コマンドは [PDK のインストール](guide/pdks.md#installing-a-pdk) にあります。

<a id="is-my-design-sent-anywhere"></a>
### 設計はどこかに送られますか?

Undine 自身は送りません。サーバーは起動した場所で動き、テレメトリーはありません。ブラウザはサーバーが送ったものを描くだけです。
データが手元のマシンから出るのは、次の場合だけです:

- **AI エージェント。** MCP では、AI アプリのプロバイダーが Undine のツールの返したものを受け取ります。アプリ内の設計エージェントでは、
  追加した API キーのプロバイダーが、エージェントの送ったものを受け取ります。ローカル LLM(Ollama)なら何も出ません。送ったものは
  すべて `<project>/llm/audit.jsonl` に記録され、`--llm-policy none` で LLM の接続をすべて無効にできます
  ([AI エージェントをつなぐ](getting-started/connect-ai.ja.md))。
- **自分で始めたダウンロード。** `setup.sh` はパッケージ、ngspice のソース、PDK、OpenVAF をダウンロードします。`lib add` と `lib sync` は、
  ライブラリの依存関係をその git のリモートから取得します。
- **フォント。** UI のページはフォントを Google Fonts(`fonts.googleapis.com`)から読み込みます。そのリクエストに設計のデータは
  含まれません。

PDK もサーバーに留まります。ブラウザが見るのはレイヤーの色、シンボル、PCell のパラメータ、描画した結果で、モデル、ルールデッキ、
PCell のコードは見ません。

<a id="can-an-ai-agent-change-my-design-without-me"></a>
### AI エージェントが勝手に設計を変えることはありますか?

ありません。エージェントは changeset、つまり git のブランチ上の作業コピーで作業します。設計が変わるのは、人がレビューで **採用**
(Adopt)をクリックしたときだけで、エージェントには採用するツールがありません。レビューの **停止**(Stop)でエージェントを止めたり、
提案を前の手順の状態に戻したり、破棄したりできます。[changeset](guide/changesets.ja.md) を見てください。

<a id="will-a-restart-lose-my-work"></a>
### 再起動すると作業は失われますか?

失われません。実行中のシミュレーションは走り続け、同じプロジェクトのルートで次に起動したサーバーが引き継ぎます。待機中のシミュレーションは
順番を保ち、エージェントはセッションと changeset を保ちます。サーバーが終わらせなかった DRC、LVS、抽出、RTL、LibreLane の実行は
`interrupted` と示されるので、実行し直します(レビューの **必要な検証を実行**(Run needed checks))。
詳しくは [サーバーの再起動](reference/cli.ja.md#restarting-the-server) にあります。

<a id="can-i-use-undine-from-claudeai-or-chatgpt-in-the-browser"></a>
### ブラウザの claude.ai や ChatGPT から Undine を使えますか?

まだ使えません。それらのコネクターはプロバイダーのクラウドから MCP サーバーにつなぐので、Undine がインターネットから届き、きちんとした
サインインを備えている必要がありますが、それはまだ作っていません。Claude Desktop、Claude Code、またはほかのローカルの MCP クライアントを
使ってください。

<a id="can-i-use-my-existing-xschem-and-gds-files"></a>
### 手持ちの Xschem や GDS のファイルを使えますか?

使えます。Undine は Xschem の `.sch` / `.sym` 形式を読み書きし、GDS と OASIS のレイアウトを取り込みます。UI ではライブラリの行の
**既存セルをインポート**(Import existing cell)、コマンドラインでは次のとおりです:

```sh
pixi run python -m undine import PROJECT LIB chip.gds
```

[回路図エディタ](guide/schematic.ja.md) と [最初のプロジェクト](getting-started/first-project.ja.md) を見てください。

<a id="how-is-undine-licensed-can-i-ship-it-together-with-klayout"></a>
### Undine のライセンスは何ですか? KLayout と一緒に配布できますか?

Undine は Apache License 2.0 でライセンスされています。Undine と一緒に使うツール、Python のパッケージ、PDK は、それぞれ自分の
ライセンスのままです。サーバーは GPL-3.0-or-later の KLayout Python モジュールを import するので、Undine とそのモジュールは 1 つの
結合したプログラムになります。Undine を動かすこと(サービスとして動かすことも)は、GPL の義務を生じさせません。2 つを一緒に *配布する* 人
(コンテナイメージ、VM、インストーラー)は、その結合物を GPL-3.0 で配布することになり、その条件を満たす必要があります。全体の表は
[ライセンス](licenses.ja.md) にあります。これは要約で、法的な助言ではありません。

<a id="can-i-use-undine-in-japanese"></a>
### Undine を日本語で使えますか?

使えます。上部バーの右にある言語メニューで **日本語** を選びます([表示言語](guide/language.ja.md))。このドキュメントも、すべての
ページに日本語版があります。ヘッダーの言語の切り替えを使ってください。日本語版がまだない新しいページは英語で表示されます。

<a id="how-do-i-report-a-bug"></a>
### バグを報告するには?

<https://github.com/kadomoto-lab/undine-eda/issues> で、開いている issue と閉じた issue を検索してから、issue を作成します。何をしたか、
何を期待したか、何が起きたかと、次の出力を含めてください:

```sh
pixi run doctor                          # what is installed (tool paths) and the PDKs found
pixi run python -m undine lib status PROJECT   # tool versions vs undine.lock
git rev-parse --short HEAD               # the Undine version
```

プロジェクトの PDK の id(`project.json` の `pdk` フィールド)、使っているディストリビューション(または WSL2)、UI の問題ならブラウザ、
そして [まずチェックを実行する](#start-here-run-the-check) にあるログも添えてください。よい報告に何が必要かは
[CONTRIBUTING.md](https://github.com/kadomoto-lab/undine-eda/blob/main/CONTRIBUTING.md) にあります。

!!! warning
    issue は公開されます。秘密保持契約(NDA)のもとにある PDK のファイル、共有してはいけない設計のデータ、`~/.undine/env`(API キーが
    入っていることがあります)は添付しないでください。
