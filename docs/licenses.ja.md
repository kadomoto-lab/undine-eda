# ライセンス

Undine は [Apache License 2.0](https://github.com/kadomoto-lab/undine-eda/blob/main/LICENSE) で提供しています。連携するツール、Python のパッケージ、
シンボルライブラリ、PDK は、それぞれのライセンスに従います。[サードパーティのライセンス](#third-party-licenses)を参照してください。

<a id="third-party-licenses"></a>
## サードパーティのライセンス

Undine 自身のコード(Rust コアを含む `server/`、`web/`、`scripts/`、`examples/`、`docs/`)は Apache-2.0 です。このリポジトリには、下の GPL の
プロジェクトからコピーしたコードやデータは含まれていません。それらは依存関係として宣言しているだけで、それぞれの配布元(PyPI、conda-forge / litex-hub、
klayout.de、ngspice のソースは SourceForge、OpenVAF の配布サイト)からインストールされます。

| コンポーネント | ライセンス | Undine での使い方 |
|---|---|---|
| KLayout の Python モジュール(PyPI の `klayout`) | GPL-3.0-or-later | **サーバーが import する**(レイアウトのデータベース、タイルの描画) |
| KLayout アプリケーション | GPL-3.0-or-later | 別プロセス(DRC / LVS デッキ) |
| Xschem | GPL-2.0-or-later | 別プロセス(Xschem のネットリスタ)。シンボルライブラリは実行時に読み込む。`.sch` / `.sym` 形式の読み書きは Undine 自身のコード |
| Netgen | 配布元を参照 | 別プロセス(LVS) |
| Magic | MIT | 別プロセス(DRC、抽出) |
| ngspice | BSD-3-Clause(ほか、配布元を参照) | 別プロセス(シミュレーション)。`scripts/ngspice.sh` が OSDI 付きでソースからビルドする |
| OpenVAF 23.5.0 | GPL-3.0 | 別のツール。`scripts/osdi.sh` がダウンロードし、IHP の Verilog-A モデルのコンパイルに使う |
| Verilator | LGPL-3.0-only OR Artistic-2.0 | 別プロセス(RTL のリントとシミュレーション) |
| LibreLane | Apache-2.0 | 別プロセス(RTL から GDS)。利用者が Nix で入れる |
| gdsfactory、kfactory、vlsirtools | MIT、MIT、配布元を参照 | kfactory ベースの gdsfactory 8(sky130)と gdsfactory 7.9(GF180MCU)は別の PCell 環境で動く。vlsirtools は任意のネットリスタ |
| FastAPI、uvicorn、numpy、orjson、anthropic、ciel | MIT、BSD-3-Clause、BSD-3-Clause(ほか)、MPL-2.0 AND (Apache-2.0 OR MIT)、Apache-2.0、Apache-2.0 | サーバーの Python の依存関係 |
| PyO3 | MIT OR Apache-2.0 | Rust コアの Python バインディング |
| React、Vite、lucide-react、CodeMirror 6(legacy Verilog モードを含む)、Lezer | MIT、MIT、ISC、MIT、MIT | Web UI。CodeMirror は RTL エディタ |
| SkyWater SKY130 PDK(open_pdks) | Apache-2.0 | 実行時に読み込む。Tiny Tapeout のピン表(`tt-support-tools`、Apache-2.0)は `server/undine/data/` にある |
| GF180MCU PDK(open_pdks)、IHP Open PDK | Apache-2.0、Apache-2.0 | 実行時に読み込む。PCell のコードは PCell 環境で動く |
| caravel_user_project、Caravel の空ラッパー(ChipFoundry) | Apache-2.0 | 同梱しない。指定されたパスから読む(LibreLane のラッパーのフロー、ChipFoundry の XOR) |
| gf180mcu-precheck(wafer.space)、cf-precheck(ChipFoundry) | Apache-2.0 | 同梱しない。テープアウト対象のチェックはこれらに倣った Undine 自身のコード |

**Undine を KLayout と一緒に配布する場合。** サーバーは GPL-3.0 の `klayout` モジュールを import するので、Undine とそのモジュールは 1 つの結合したプログラムに
なります。Apache-2.0 は GPLv3 と互換性があり(GPLv2 のみのものとは互換性がありません)、そのため Undine 自身のコードは Apache-2.0 のままでいられます。
両者を一緒に *配布する* 人(たとえばコンテナイメージ、VM、インストーラ、ホスティング用のアプライアンスイメージ)は、その結合を GPL-3.0 のもとで配布することになり、
その条件を満たす必要があります。
