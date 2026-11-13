# テープアウト

レイアウトエディタのロケットのタブに、シャトルが求めるものがまとまっています。対象を選び、レイアウトをチェックし、サインオフ一式を実行して、
バンドルを出力します。完成したセルからバンドルまでを順にたどるには、チュートリアルの
[Tiny Tapeout でテープアウト](../tutorials/tiny-tapeout.md)を参照してください。対象は次の 4 つです:

- **Tiny Tapeout - SKY130 analog**
- **wafer.space - GF180MCU die**
- **ChipFoundry - Caravel user project (SKY130)**
- **Layout hygiene only (any shuttle)**

## Tiny Tapeout - SKY130 analog

レイアウトのチェックは `tt-support-tools`(Apache-2.0)の事前チェックに倣っており、公式のピン表(`server/undine/data/tt_sky130A_analog.json`)を使います:
`prBoundary`(235/4)付きの、ちょうど 1x2 / 2x2 のプロジェクト領域。テンプレートのすべてのピンと電源ストライプが met4(71/20)とその pin 用途(71/16)で
決まった位置にあること。`VGND` / `VDPWR` の met4 ストライプ(1.2 um 以上、上下の端から 10 um 以内)。ピンの重なりがないこと。Metal 5 を使わないこと。
`ua[n]` が `analog_pins` の指定どおりにメタルにつながり、`info.yaml` に記述されていること。nwell と urpm の間隔。

- **サインオフ一式:** PDK のすべての KLayout デッキ(cf-precheck が実行するものを含む)、Magic の DRC、KLayout による LVS
  (フラット、基板は `VGND`)と Magic + Netgen による LVS。パネルには各チェックの最新の結果と、それが今の設計に対する結果かどうか
  (変わった内容付きの `outdated`、または `not run`)を表示します。一式を実行すると、古いものと未実行のものだけを実行し、
  まったく同じ入力(設計、PDK、デッキ、ツールのバージョン、オプション)で実行済みのものはそのまま使います。**Rerun all** ですべてを実行し直します。
- **提出用バンドル**(`<project>/tapeout/<target>/<top>-<time>/`、zip も作成): 最上位セルの名前を変えた `gds/<top>.gds`、
  テンプレートの全ピンを含む `lef/<top>.lef`、ポートのスタブ `src/project.v` と `gds/<top>.v`、`info.yaml`、`docs/info.md`、
  そしてチェック結果、PDK とツールのバージョン、全ファイルの SHA-256 を記録した `report.json`。チェックに失敗しているものがあると、エクスポートは拒否されます。
  `report.json` には、エクスポートしたセルの内容ハッシュ(`layout`)とサインオフの状態(`signoff`)も入ります。各チェックについて、ジョブ、結果、
  エクスポートした設計に対して実行したものか(`fresh`)、それ以降に何が変わったかを記録します。
- Yosys による構文チェックと、シャトル自身の事前チェックは Undine には含まれません。これらは `manual` として報告し、`pass` にはしません。

`examples/make_tt_inverter.py` は、デモのインバータを入れたタイル(`tt_um_undine_inv`: `ua[0]` から `ua[1]` へのインバータ、電源ストライプ、
ビアのスタック)を作ります。これは上のチェックにすべて合格します。`tt_um_undine_inv` は公式の事前チェック(`tt-support-tools`、Magic の DRC、
KLayout の FEOL/BEOL/offgrid、ピン、境界、電源、アナログピン、Yosys のチェック)にもかけ、すべて合格しました。

## wafer.space - GF180MCU die

レイアウトは gf180mcuD のダイ全体です。wafer.space の事前チェックが受け付ける PDK は gf180mcuD だけです。
チェックは [gf180mcu-precheck](https://github.com/wafer-space/gf180mcu-precheck)(Apache-2.0)のレイアウトの手順に倣っています:

- PDK が gf180mcuD であること。事前チェックが固定している open_pdks のバージョン(`792ef670`)と違う場合は警告です。
- ダイが原点から始まり、**スロット**の寸法にちょうど一致すること:

    | スロット | ダイ (um) |
    |---|---|
    | 1x1 | 3932 x 5122 |
    | 0p5x1 | 1936 x 5122 |
    | 1x0p5 | 3932 x 2531 |
    | 0p5x0p5 | 1936 x 2531 |

- メタルが 5 層であること。Via5(82/0)と MetalTop(53/0)には何も置けません。
- ダイの周囲にシールリング(GUARD_RING_MK、167/5)があること。
- ダイ全体の密度。ダミーフィル(データタイプ 4)も描画として数え、COMP 25-70%、Poly2 14% 以上、Metal1-5 30% 以上です。
- **Chip-on-Board** では、4 つの ID セル(`gf180mcu_ws_ip__qrcode_id`、`__shuttle_id`、`__project_id`、`__marker`)が、
  それぞれプロジェクトテンプレートと同じ位置に 1 つずつあること。パッド開口もスロットのゴールデンパッドマスクを覆う必要がありますが、
  これはプラットフォームの事前チェックが照合するため、Undine では `manual` として報告します。

サインオフ一式は、事前チェックと同じく PDK のデッキを 3 回実行します:

- 密度、アンテナ、CUP を除いたメインのチェック
- `decks=density`
- `decks=antenna`

ほかに Magic の DRC と、セルに回路図があれば LVS も実行します。パッドリング、シールリング、フィルは wafer.space のプロジェクトテンプレート
(LibreLane)で作ります。Undine は結果をチェックしますが、これらは生成しません。

バンドルには `gds/<top>.gds`、`report.json`、`SUBMIT.md` が入ります。`SUBMIT.md` には、スロットに合わせた事前チェックのコマンドと、
[platform.wafer.space](https://platform.wafer.space) での手順を書いてあります。手順は、スロット(と CoB)を指定してプロジェクトを作り、
GDS のリンクを渡し、プラットフォームの事前チェックに合格したら提出する、の 3 つです。

## ChipFoundry - Caravel user project (SKY130)

レイアウトは、sky130A または sky130B の Caravel のユーザープロジェクト用ラッパーです。**ラッパー**として、デジタル(`user_project_wrapper`)か
アナログ(`user_analog_project_wrapper`)を選びます。チェックは [cf-precheck](https://github.com/chipfoundry/cf-precheck)(Apache-2.0)の
レイアウトの手順に倣っており、ラッパーの名前に変えたセルに対して行います:

- セル名に `#` や `/` がないこと。
- prBoundary(235/4)がユーザー領域 0,0 から 2920,3520 um にちょうど一致すること。クリアエリア密度のデッキはこの内側を測ります。
- 電源リングの外縁 (-43.63, -38.27) から (2963.25, 3557.95) um より外に何もないこと。
- **ラッパーの縁の XOR:** ユーザー領域の外にあるもの(ピンの突き出しと電源リング)が、Caravel の空ラッパーと同一であること。
  cf-precheck と同じく、ラベルは除き、ピンの図形は描画レイヤーとして扱います。また、左右の met5 と上下の met4 を消してから比べるので、
  ストラップがリングに入り込んでいてもかまいません。

    Undine は Caravel のファイルを同梱していないので、XOR には参照ファイルが必要です。
    [chipfoundry/caravel](https://github.com/chipfoundry/caravel) から `gds/user_project_wrapper_empty.gds.gz`(またはアナログ用)を
    ダウンロードし、サーバーが読めるディレクトリ(`--import-root`、既定はホームディレクトリ)に置いて、そのパスを **golden_gds** に入力します。
    指定しなければ XOR は `manual` になります。

- `verilog/rtl/user_defines.v` の GPIO の既定値と、io_oeb に対する CVC のチェックは `manual` です。cf-precheck が
  リポジトリと抽出したネットリストから読みます。

サインオフ一式では、cf-precheck が実行する sky130 のデッキ(FEOL、BEOL、offgrid、クリアエリア密度、ピンラベル、面積ゼロ)、
Magic の DRC、LVS を実行します。

バンドルには `gds/<wrapper>.gds`、`report.json`、`SUBMIT.md` が入ります。`SUBMIT.md` には、`caravel_user_project` のリポジトリで GDS の横に
追加するファイルと、ChipFoundry CLI での 2 つの手順(`cf precheck` のあと `cf push`)を書いてあります。

## Layout hygiene only (any shuttle)

この対象は、上のどのシャトルにも向けない設計のために、どのシャトルにも必要な点をチェックします:

- 最上位セルが 1 つ
- データベース単位
- 面積ゼロの図形がない
- 原点
- 最上位メタル
