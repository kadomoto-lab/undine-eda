# インストール

<a id="1-before-you-start"></a>
## 1. 始める前に

| 必要なもの | 確認方法 | 入手方法 |
|---|---|---|
| Linux x86-64(または WSL2)、glibc 2.28 以上 | `ldd --version` | Ubuntu 20.04 以降、RHEL / Rocky / AlmaLinux 8 または 9。[ディストリビューション別](#per-distribution)を参照 |
| git、curl | `git --version` | `sudo apt install git curl` / `sudo dnf install git curl` |
| [pixi](https://pixi.sh/) | `pixi --version` | 下のインストーラ(`~/.pixi/bin` に実行ファイルが1つ入ります)。その後、新しいシェルを開く |
| KLayout アプリケーション 0.30 以上(DRC / LVS) | `klayout -v` | ディストリビューションの公式パッケージ([下記](#per-distribution))。`./setup.sh check` がコマンドを表示します |
| ディスク 約 5 GB | | 大半は PDK と EDA ツール |

```sh
curl -fsSL https://pixi.sh/install.sh | sh     # pixi
```

それ以外はすべて pixi が [`pixi.toml`](https://github.com/kadomoto-lab/undine-eda/blob/main/pixi.toml)(ロックファイルは [`pixi.lock`](https://github.com/kadomoto-lab/undine-eda/blob/main/pixi.lock) の1つ)から用意します:
Python、サーバー、UI 用の Node.js、Xschem、Netgen、Magic、Verilator、vlsirtools、PDK 用の [ciel](https://github.com/fossi-foundation/ciel)、
2 つの PCell 用環境(sky130 と IHP の PyCell 用に kfactory ベースの gdsfactory 8.x、GF180MCU 用に gdsfactory 7.9)、それにサーバーのネイティブコア
(`server/src`。サーバーのパッケージに組み込んでビルド)をビルドする Rust ツールチェーンです。
システムの Python、Node、Rust、conda は不要で、どのディストリビューションでも同じものが入ります。ngspice は `setup.sh` が pixi 環境の中で
ソースからビルドします(手順 `ngspice`、pixi の C コンパイラを使用)。conda-forge の ngspice は、IHP のモデルに必要な OSDI に対応していないためです。
唯一の例外は KLayout アプリケーションです。conda パッケージが conda-forge から入らなくなったため、klayout.de のシステム
パッケージを使います(サーバー自体は pixi 経由で PyPI の `klayout` Python モジュールを使います)。

<a id="per-distribution"></a>
### ディストリビューション別

KLayout アプリケーションは [klayout.de/build.html](https://www.klayout.de/build.html) から入手します(0.30.12。`pixi.lock` の
Python モジュールと同じバージョン)。`apt` / `dnf` が依存関係をディストリビューション標準のリポジトリから入れるので、EPEL は不要です。

| ディストリビューション | KLayout パッケージ | インストール | 一緒に入る依存関係 |
|---|---|---|---|
| Ubuntu 22.04 | [Ubuntu-22 .deb](https://www.klayout.org/downloads/Ubuntu-22/klayout_0.30.12-1_amd64.deb) | `sudo apt install ./klayout_0.30.12-1_amd64.deb` | Qt 5.15、Ruby 3.0、Python 3.10、libgit2 1.1 |
| Ubuntu 24.04 | [Ubuntu-24 .deb](https://www.klayout.org/downloads/Ubuntu-24/klayout_0.30.12-1_amd64.deb) | `sudo apt install ./klayout_0.30.12-1_amd64.deb` | Qt 5.15、Ruby 3.2、Python 3.12、libgit2 1.7 |
| Ubuntu 20.04 | `Ubuntu-20` の .deb(ファイル名は同じ) | 上と同じ | そのリリースの Qt 5、Ruby、Python 3 |
| RHEL / Rocky / AlmaLinux 9 | [RockyLinux_9 .rpm](https://www.klayout.org/downloads/RockyLinux_9/klayout-0.30.12-0.x86_64.rpm) | `sudo dnf install ./klayout-0.30.12-0.x86_64.rpm` | qt5(base、multimedia、svg、tools、xmlpatterns)5.15、Ruby 3.0、Python 3.9、http-parser |
| RHEL / Rocky / AlmaLinux 8 | [CentOS_8 .rpm](https://www.klayout.org/downloads/CentOS_8/klayout-0.30.12-0.x86_64.rpm) | `sudo dnf install ./klayout-0.30.12-0.x86_64.rpm` | qt5(上と同じ)、Ruby 2.5、Python 3.6、libgit2 |
| CentOS 7 | | 非対応: glibc 2.17 は pixi 環境が必要とするものより古く(UI 用の Node.js に 2.28 が必要)、CentOS 7 は 2024 年にサポートが終了しています | |

どのディストリビューションでも **`pmap`** が必要です(Debian / Ubuntu ではパッケージ `procps`、RHEL 系では `procps-ng`)。
sky130 の KLayout LVS デッキがメモリ使用量の記録に使い、これがないと失敗します。通常のインストールには含まれていますが、
最小構成のコンテナイメージには入っていません。

動作確認済み: Ubuntu 22.04(WSL2)、Ubuntu 24.04、Rocky Linux 8 と 9(コンテナ)で、`./setup.sh`、デモの設計、シミュレーション、
Tiny Tapeout のサインオフ一式(KLayout の DRC / LVS、Magic の DRC、Magic + Netgen の LVS)を実行しました。表のほかの行も
同じパッケージに沿っていますが、実行はしていません。

<a id="2-install"></a>
## 2. インストール

```sh
git clone https://github.com/kadomoto-lab/undine-eda.git
cd undine-eda
./setup.sh
```

`setup.sh` は次の手順を実行します。何度でも実行し直せます(例: `git pull` の後)。手順名を指定すると、その手順だけを実行します
(`./setup.sh web`、`./setup.sh --help`):

| 手順 | 同じ意味のコマンド | 内容 |
|---|---|---|
| `install` | `pixi install -e default`、`-e pcell`、`-e pcell-gf180` | default 環境(サーバー、Node、EDA ツール)と PCell 環境: sky130(と IHP の PyCell)用の gdsfactory 8、GF180MCU 用の gdsfactory 7.9(`docs` 環境はこのドキュメントとスクリーンショット専用) |
| `ngspice` | `pixi run ngspice` | OSDI 対応の ngspice 47 を default 環境にビルド(初回だけ数分。入っていれば飛ばします) |
| `web` | `pixi run web` | UI を `web/dist` にビルド |
| `pdk` | `pixi run pdk` に近いもの([違い](../guide/pdks.ja.md#sky130)) | ciel で SKY130 PDK をダウンロードして有効化(`$PDK_ROOT`、既定は `~/.ciel`)。Undine で動作確認したバージョンに固定。そのバージョンがすでにあれば飛ばします |
| `osdi` | `pixi run osdi` | 既定では実行しません。インストールした IHP の PDK の Verilog-A モデルを、ngspice 用に OpenVAF でコンパイル([IHP SG13G2 の Verilog-A モデル](../guide/pdks.ja.md#ihp-sg13g2-verilog-a-models)を参照) |
| `check` | `pixi run doctor` | チェック(後述)だけを実行。`setup.sh` は毎回最後にこれを実行します |

!!! note "古い Undine から更新する場合"

    プロジェクトは、作成時のツールのバージョンを `undine.lock` に記録しています。ngspice 47 に更新すると、依存関係のチェックで
    `ngspice: locked 41, here 47 from pixi (…)` と警告が出ます(不一致には数えません)。結果を確認したら、
    `pixi run python -m undine lib lock PROJECT` で新しいバージョンを記録してください([undine.lock](../guide/libraries.ja.md#undinelock)を参照)。

<a id="other-pdks"></a>
### ほかの PDK

GF180MCU、IHP SG13G2、SKY130 の別のバージョンも ciel で入れます。[PDK](../guide/pdks.ja.md) のページに、動作確認した
バージョンとそれぞれで使えるもの、[入れ方](../guide/pdks.ja.md#installing-a-pdk)、プロジェクトが PDK を固定するしくみと
別のバージョンへ移す方法があります。

<a id="3-check-and-fix"></a>
## 3. 確認と修正

`setup.sh` は最後にチェックを行います。単独では `./setup.sh check`(または `pixi run doctor`)で実行できます。各行に見つかったものが表示され、
足りないものがあれば、それを直すコマンドも表示されます:

```
 ✓ klayout Python module >= 0.30          ok  0.30.12
 ✓ a PDK                                  ok  sky130A@8afc8346a57f, sky130B@8afc8346a57f
 - klayout (DRC / LVS)                    missing  [recommended]  -> install the KLayout application (>= 0.30) ...
```

`required` の項目は起動に必要です。`recommended` の項目がないと、その機能(シミュレーション、DRC/LVS、PCell)が使えません。
`pixi run` の中(と pixi 環境の MCP の起動役が起動したサーバー)では、ツールは pixi 環境(`$UNDINE_TOOLS`。未設定ならサーバー自身の環境、次に
`~/undine-tools`)から、それ以外は `PATH` から探します。そのため、別の方法で入れた
ツール(apt、Homebrew、IIC-OSIC-TOOLS、ソースからのビルド)も使えます。PDK は `$PDK_ROOT`、`~/.ciel`、`~/.volare` から探します
([Undine が PDK を探す場所](../guide/pdks.ja.md#where-undine-looks-for-pdks))。

<a id="without-pixi"></a>
## pixi を使わない場合

システムの Python 3.10 以上と Node 18 以上を使います:

```sh
python3 -m venv .venv
.venv/bin/pip install -e "server[pdk,dev]"     # server with its Rust core (needs cargo, e.g. rustup), ciel, pytest
(cd web && npm ci && npm run build)                                      # UI
.venv/bin/ciel enable --pdk-family sky130 8afc8346a57fe1ab7934ba5a6056ea8b43078e71
```

ngspice(IHP を使うなら `--enable-osdi` 付きでビルドしたもの)、Xschem、Netgen(LVS ツール)、Magic を `PATH`(または `$UNDINE_TOOLS/bin`)に置きます。PCell を使うには、Python 3.12 の環境に
`pixi.toml` の `[feature.pcell.pypi-dependencies]`(gdsfactory 8.32.2。kfactory 0.23 が入ります)を入れ、その `python` を `UNDINE_PCELL_PYTHON` で指定します。
GF180MCU の PCell には、`[feature.pcell-gf180.pypi-dependencies]`(gdsfactory 7.9.4)を入れたもう 1 つの環境を作り、`UNDINE_PCELL_GF180_PYTHON` で指定します。
`vlsirtools`(任意のネットリスタ)は `pip install vlsirtools` で入ります。

<a id="next-steps"></a>
## 次のステップ

- [最初のプロジェクト](first-project.ja.md): プロジェクトを作り、画面をひととおり見ます。
- [AI エージェントをつなぐ](connect-ai.ja.md): Claude などの AI アプリに Undine で作業させます。
- チェックで問題が見つかったら、[トラブルシューティングと FAQ](../troubleshooting.ja.md) を参照してください。
