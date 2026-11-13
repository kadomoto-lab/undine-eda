# RTL とシミュレーション

セルは **RTL** のビューを持てます。ライブラリの中の Verilog / SystemVerilog のファイル `<cell>.sv`(または `<cell>.v`)で、
セルと同じ名前のモジュールを定義します。**New cell**(*RTL* にチェック)で作るか、既存の `.sv` / `.v` ファイルのセルを、
ライブラリブラウザ(`rtl` のチップ)から開きます。ファイルはモジュールのひな形から始まります。

## 編集

RTL のビューはコードエディタ([CodeMirror](https://codemirror.net/))です。Verilog / SystemVerilog を色分けします(キーワード、型、数値、
文字列、システムタスク、マクロ、コメント)。ほかに次の機能があります。

- 行番号、折りたたみ、括弧の対応表示
- 検索と置換(Ctrl+F。Ctrl+G で次を検索、Ctrl+Alt+G で行へ移動)
- Tab で 2 文字分インデント
- 保存前の編集は Ctrl+Z / Ctrl+Y で取り消し・やり直し

**Save**(または Ctrl+S)でファイルに書き込み、保存のたびにライブラリの操作ログに記録されます。
そのため、Undo / Redo(エディタ上部の矢印)と提案は、ほかのビューと同じように動きます。提案の中では、エージェントの RTL の変更がレビューに現れ、
ほかの変更と一緒に採用・破棄されます。提案が作ったファイルは、作った回路図と同じく、提案を取り消しても残ります。

## リント

**Lint** は、このセルをトップモジュールにして Verilator(`--lint-only -Wall`)を実行します。ライブラリのほかの RTL ファイルもソースに含むので、
ほかのセルをインスタンス化しているセルも解決できます。エラーと警告はファイルと行付きで並び、このセルのファイルのものを押すとその行と列へ移動します。
エディタの中では Verilator が指した箇所に波線(エラーは珊瑚色、警告は砂色)が引かれ、行番号の横にも印が付きます。マウスを乗せると内容が出ます。保存すると、もう一度リントします。

## シミュレーション

テストベンチも RTL のセルです。たとえば `tb_counter` は、設計をインスタンス化し、`initial` ブロックで駆動し、`$display` で報告し、
`$error` やアサーションで確かめ、`$finish` で終わり、`$dumpfile` / `$dumpvars` で波形を書き出します。**Simulate** は、テストベンチを
Verilator(`--binary --timing --trace`)でビルドして実行します:

- エラーなく終われば **Passed**。`$error`、`$fatal`、失敗したアサーション、ビルドのエラーがあれば **Failed** になり、エラーの行を並べる
- テストベンチの出力(`$display`)、終了した時刻、Verilator のビルド時の警告を表示する
- 書き出した信号を波形で表示する(ベクタは整数値、x / z は途切れ)

実行はそれぞれプロジェクトのジョブ(`runs/<id>/`)で、シミュレーションしたソースのコピー、ログ、`wave.vcd` を持ちます。
パネルには、選んだテストベンチの以前の実行も並びます。

<a id="rtl-from-a-dependency"></a>
### 依存関係の RTL

Verilator は、`$c(...)` や `` `systemc_* `` ブロックをシミュレーションの中で実行される C++ にし、`$system(...)` はシェルを実行し、
DPI の import は C を呼び、`` `include ``、`$fopen`、`$dumpfile`、`$readmem*`、`$writemem*` はファイルを読み書きします。Undine は
[依存関係](libraries.ja.md#dependencies)のライブラリにあるこれらを、人が[その依存関係を信頼する](libraries.ja.md#trusting-a-dependencys-code)
まで拒否します: **Simulate** は、それらのどれか(や、それらを組み立てる `` `define `` の本体、絶対パスや `..`、`~` を含むパス)を持つ
ライブラリを拒否し、何もコンパイルしない **Lint** は、そのような `` `include `` のパスだけを拒否します。依存関係の RTL のファイルは、
フォルダの中のものも含めてすべて読みます。拒否はファイルと行をそれぞれ示します:

```
analog_ip/rtl/ctrl.sv (dependency analog_ip 1.2.0, not trusted): line 12: initial $c("init_model();"); a person can trust analog_ip@1.2.0 in this project (undine lib trust demo analog_ip, or Trust... in Undine); an agent cannot
```

パネルはこれを **信頼...** とともに表示し、信頼のダイアログが開きます。信頼すると RTL をそのまま実行するので、先にダイアログの
一覧を読んでください。自分のライブラリの RTL は確かめません。

<a id="gate-level-netlists-of-standard-cell-schematics"></a>
### 標準セルの回路図のゲートレベルネットリスト

PDK の標準セル(sky130 の `sky130_stdcells/*.sym`)で描いた、RTL のないセルは、Lint と Simulate では **ゲートレベルネットリスト** と
して読みます。セルと同じ名前の Verilog モジュール 1 つで、ポートは回路図の順、標準セル 1 つにインスタンス 1 つ、ピンはすべて名前で
つなぎます。テストベンチがインスタンス化したそのようなセルも同じです。セルの PDK の Verilog モデルも一緒に読みます(sky130 は
`primitives.v` と `sky130_fd_sc_hd.v`)。遅延のない機能モデルとして読み(`FUNCTIONAL`、`UNIT_DELAY` は空)、モデル自身の警告は
報告しません。RTL が自分で定義した標準セル(スタブ)は RTL のものを使い、使う標準セルをすべて RTL が定義していればモデルは読みません。電源ピンは `` `ifdef USE_POWER_PINS `` の中にあり、これらの実行では定義しないので、テストベンチは論理のポートだけを
つなぎます。

<a id="librelane-rtl-to-gds"></a>
## LibreLane: RTL から GDS へ

**LibreLane** のタブは、[LibreLane](https://librelane.readthedocs.io/) でセルの RTL からレイアウトを作ります。作り方は、プロジェクトの
**フロー設定**(`flows/<name>.yaml`。*フローを作る* でそのセル用に作れる)で決めます。トップのセル、クロックのポートと周期(組み合わせ回路なら
*なし*)、コアの使用率、ほかの LibreLane の変数(そのまま渡す)です。残りはセルの **設計意図** から取ります。ポートの辺は LibreLane のピン順の
ファイルになり(*ツールに任せる* のポートは配置器が決める)、電源 / グラウンドのネットは `VDD_NETS` / `GND_NETS` になります。

実行の前に、タブは足りないもの(クロック、各ポートの辺)と **導出した設定** を並べます。実行時に LibreLane に渡すすべての設定と、それぞれの元に
なったフローや意図の項目です。設定は実行のたびに導出し直して、その実行と一緒に保存します(`config.json`、`pin_order.cfg`、`constraints.json`)。
変更は実行の側ではなく、フローや意図の側で行います。実行はジョブ(`runs/<id>/`)で、実行中は LibreLane のログを、終わると主な結果(セル数、
面積、セットアップ / ホールドのスラックと違反、DRC・LVS・アンテナの件数、最大スルー / 容量 / ファンアウトの違反、電力)とエラーを表示します。**レイアウトを取り込む** で、最終 GDS を
使っている標準セルと一緒にセルのライブラリへ取り込みます。

<a id="from-a-schematic-of-standard-cells"></a>
### 標準セルの回路図から

`source: schematic` のフローは、標準セルで描いたセルを、RTL なしで回路図から作ります。実行は回路図のゲートレベルネットリストを
`src/<cell>.v` に書き出し、LibreLane はそれを読むだけです。合成はせず、セルはすべて描いたとおりに残ります(遅延チェーン、タイセル)。
実行は次の設定を導出し、導出した設定ではそれぞれ `flow.source` から来たと示します。フローの変数でどれも上書きできます:

| 設定 | 理由 |
| --- | --- |
| `SYNTH_ELABORATE_ONLY: true` | Yosys はネットリストを読むだけで、何もマップしない |
| `RSZ_DONT_TOUCH_RX: ".*"` | リサイザーにとって、すべてのネットとインスタンスを don't-touch にする |
| `RUN_POST_GPL_DESIGN_REPAIR`、`RUN_POST_GRT_DESIGN_REPAIR: false` | 長いネットやポートにバッファを入れない。サイズを変えない |
| `RUN_CTS: false` | クロックツリーを作らない。クロックは描いたとおりに配線する |
| `RUN_POST_CTS_RESIZER_TIMING`、`RUN_POST_GRT_RESIZER_TIMING: false` | タイミングの修正をしない |
| `DIODE_ON_PORTS: none`、`RUN_HEURISTIC_DIODE_INSERTION: false`、`RUN_ANTENNA_REPAIR: false` | アンテナダイオードを入れない |
| `DRT_ANTENNA_REPAIR_JUMPER_ONLY: true` | 詳細配線はアンテナを層の乗り換えだけで直す |

電源は、セルの VPWR / VGND のパラメータにあるネット(`vdd`、`vss`)で、回路図のポートである必要があります。意図に電源があればそれと
合わせて `VDD_NETS` / `GND_NETS` になります。そのピンは電源グリッドのストラップで、1 つの辺に並ぶピンではありません。意図が電源の
ポートに辺を決めていても使わず、チェックは *実行で使わないもの* に挙げます。

実行の前に、チェックはレイアウトでつながらないまま残るものを断ります。LibreLane はネットリストを読むだけでタイセルを入れないので、
電源に直接描いた入力ピンや、入力しかない内部ネットには、駆動するものがありません。タイセル(sky130 は `sky130_fd_sc_hd__conb_1`。1 は
HI、0 は LO)を描き、定数はそこから取ります。

実行が終わると、結果は最終ネットリストを描いたものと比べます。足された、なくなった、変わった論理のセル(ネットリストが保たれていれば
なし)と、それとは別に、レイアウトが足したフィル、タップ、デキャップ、ダイオードのセルです。回路図が描いたデキャップやダイオードは、
ほかのセルと同じように比べます。結果には、サインオフでの最大スルー、最大容量、最大ファンアウトの違反と、それが出たコーナーも
並びます。バッファもサイズ変更もないので、直すのは回路図の側です(強いドライバにする、負荷の多いネットにバッファを描く)。タイミングは、
フローが自分の SDC(`PNR_SDC_FILE`、`SIGNOFF_SDC_FILE`)を渡さなければ LibreLane の汎用 SDC で確かめます。自己タイミングや非同期の
設計では、そのスラックはあまり意味がありません。

フローが回路図から作るセルでは、LibreLane のパネルはセルのページの、水面より下にあります(*フローを表示*)。レビューでフロー設定を
選ぶと、そのページが開きます。

```yaml
# flows/sar_logic.yaml
top: {lib: sar, cell: sar_logic}
source: schematic
clock: {port: clk, period_ns: 195.3}
floorplan: {die_area_um: [0, 0, 60, 60]}
variables: {RT_MAX_LAYER: met4, FP_PDN_MULTILAYER: false, FP_PDN_VPITCH: 25, FP_PDN_VOFFSET: 5, FP_PDN_VWIDTH: 1.6,
            PL_TARGET_DENSITY_PCT: 60}
```

取り込んだレイアウトは、ほかのレイアウトと同じように回路図と照合します([検証](verification.md) の「LVS の標準セル」)。

LibreLane は、正式なインストール方法である Nix で入れます。LibreLane のバイナリキャッシュ付きで Nix を入れ、LibreLane を `~/librelane` に clone し、
そこで一度 `nix-shell --run "librelane --smoke-test"` を実行します([LibreLane のインストール手順](https://librelane.readthedocs.io/en/latest/installation/nix_installation/index.html))。
Undine はそれを `nix-shell ~/librelane --run …` で実行します。別のコマンドは `UNDINE_LIBRELANE` で指定します。実行には、プロジェクトの PDK を
インストールされたまま使います(`--manual-pdk`)。

エージェントはフローを読み(`list_flows`、`check_flow`)、実行を始め(`run_librelane`)、結果を読みます(`get_librelane_result`)。
フロー設定は、エージェントが `write_flow` で自分の変更セットに提案します。ファイルは提案の一部で、レビューに出て(ビュー *flow*、
トップのセルの下)、提案を採用するとプロジェクトに入ります。`check_flow` は保存前の設定(`config`)も確かめます。`export_verilog` は
標準セルの回路図のゲートレベルネットリストを見せます。

### Caravel: マクロを user_project_wrapper へ

ChipFoundry の Caravel(SKY130)向けには、2 つのフローでハードニングします。まずマクロ、次にそれを 2920 x 3520 um のユーザー領域に置くラッパーです。

```yaml
# flows/counter.yaml: マクロ
top: {lib: dig, cell: counter}
clock: {port: clk, period_ns: 25}
floorplan: {die_area_um: [0, 0, 150, 150]}
caravel: {role: macro}

# flows/wrapper.yaml: ラッパー
top: {lib: dig, cell: user_project_wrapper}
caravel:
  role: wrapper
  template: ~/caravel_user_project
  macros:
    - {flow: counter, instance: mprj, location: [1400, 1700], orientation: N}
```

**マクロ(`role: macro`)。**

- ユーザー領域の電源ピン `vccd1` / `vssd1` を持ちます。RTL では `` `ifdef USE_POWER_PINS `` の中で宣言し、この 2 本に辺の指定はいりません。
- 電源網に最上位のストライプを作らず、配線は met4 までです。ラッパーの電源網が met4 / met5 でつながるようにするためです。

**ラッパー(`role: wrapper`)。**

- 外形、ピン、電源リングとストライプ、タイミング制約は、[chipfoundry/caravel_user_project](https://github.com/chipfoundry/caravel_user_project)
  のチェックアウトから取ります。Undine には同梱していません。読むのは `openlane/user_project_wrapper` と `verilog/rtl` だけです。
- RTL は `user_project_wrapper` セルの rtl ビューです。テンプレートの `verilog/rtl/user_project_wrapper.v` を元にして、マクロをフローで決めた
  インスタンス名で置いてください。
- 各マクロは、そのフローで最後に成功した実行から取ります。実行はマクロのビューを `macros/<flow>/` にコピーし、マクロの `vccd1` / `vssd1` を
  ラッパーの電源網につなぎます(ほかのネットは `power` で指定)。
- ラッパーには標準セルの行がないので、詳細配線のアンテナ修正はジャンパだけで行い、ダイオードのセルは入れません。

Undine で実際にカウンタのマクロをラッパーへ組み込むと、LibreLane の Magic / KLayout DRC と LVS、**ChipFoundry** 対象のレイアウトのチェック
([テープアウト](tapeout.md))に合格します。

## エージェント

エージェントは提案の中で RTL を読み書きします。`get_rtl`、`write_rtl`(ファイル全体。結果にリントを含む)、`lint_rtl`、
`run_rtl_sim`(テストベンチの出力、エラー、終了、書き出した各信号の最後の変化)、ビュー `[rtl]` の `create_cell` です。
回路図と同じく、セルの設計意図を拠り所に作業します。RTL のセルの意図は、そのモジュールのポートを指します。
信頼の規則が拒否する依存関係の RTL は、エージェントにも拒否されます。依存関係を信頼できるのは人だけなので、エージェントは
その拒否を伝えてあなたに尋ねます。

<a id="installing-verilator"></a>
## Verilator のインストール

`pixi install` で、Undine の環境に Verilator(conda-forge)が入ります。Verilator は、C++ コンパイラと `make`(システムのもの、
または環境のもの)でシミュレーションをビルドします。
