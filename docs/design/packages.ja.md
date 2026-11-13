# パッケージと PDK(設計)

状態: 設計です。2026-11-05 に決めました([決定事項](#decisions))。**段階 0、段階 1**(ストアと lock format 2)、**段階 2**(manifest、推移的な依存、PDK の互換性、信頼の規則)
**は済んでいます**。**段階 3 から 6 は実装していません**。[段階](#phases)に、各段階でできることと、段階ごとに今あるものを書きました。
今の PDK でユーザーができることは[PDK のガイド](../guide/pdks.ja.md)に、依存関係とパッケージについては
[ライブラリと依存関係](../guide/libraries.ja.md)にあります。

この文書には基準がいくつかあります。[現状](#today)は、この設計を書いたときのコードを説明し、行番号もそのときのものです。
[段階 0 の状況](#phase-0-status)のうち、段階 0 の前半で行った項目はその直後のコードで、ほかの項目は段階 0 を終えたときのコードで
確かめ、行番号もそれぞれのものです。
[段階 1 の状況](#phase-1-status)は、段階 1 のコードを行番号ではなくモジュールと関数で説明し、[段階 2 の状況](#phase-2-status)も
段階 2 について同じようにします。

## 目的

ライブラリと PDK を、cargo の crate や npm のパッケージのように共有できるようにします。プロジェクトが `sky130A` の上で
`analog_ip ^1.2` を求めると、Undine は条件に合うバージョンを探し、共有のストアに一度だけ取得し、取得したものを厳密に固定します。
プロジェクトをどこでチェックアウトしても同じファイルが入り、ハッシュで確かめられます。ライブラリの公開は、index リポジトリへの
プルリクエストです。

ユーザーと決めたこと:

- レジストリは **git の index リポジトリ**です(crates.io-index や Homebrew の tap と同じ形)。名前とバージョンから、git の URL、
  commit、内容のハッシュを引きます。公開は index への PR です。private な index も作れます。今の git 依存を土台にします。
- **PDK**: Undine は [ciel](https://github.com/fossi-foundation/ciel) を包みます。PDK は特別なルート依存です。取得と install は
  ciel が行い、Undine は commit 全体と木のハッシュを固定して検証し、ないときは(人が確認したうえで)取得を提案します。
  `pdkprofile.py` にあるプロセスの知識は **PDK プロファイル**のパッケージにします。

目的としないこと: レジストリのサービスを運用すること(index は git リポジトリで、ファイルは各 git リポジトリに置いたまま。
テープアウトは `pkg vendor` で自分の写しを持つ。[取得とストア](#fetch-and-store)を参照)、EDA ツールを入れること(pixi の役目)、
シミュレーション以外でパッケージのコードを実行すること(パッケージはセルと manifest というデータ。ただし、そのセルを
シミュレーションすると、セルがシミュレータに渡す内容が実行される。[セキュリティ](#security)を参照)、1 つのプロジェクトに同じ
パッケージの複数のバージョンを入れること([解決](#resolution)を参照)、署名(後で行う。[決定事項](#decisions)を参照)。

<a id="today"></a>
## 現状

この表は、この設計を書いたときのコードを説明しています。その後に変わった行にはそう書いてあります。段階 0 の前半では
*PDK* の行(OSDI はもう PDK の木にコンパイルしない)と *ロック* の行(CLI とサーバーがプロジェクトのロックを取るようになった)、
段階 0 の残りでは *git 依存* の行(`format` の確認、残りもの、`--dep-root`)と *ネットリスト* の行(名前の衝突の拒否)です。
段階 1 で *git 依存*、*PDK*、*ツール* の行はさらに変わりました。表はそのままにし、今どうなっているかは[段階 1 の状況](#phase-1-status)に
書きました。段階 2 では *git 依存* の行がもう一度(manifest、推移的なパッケージ、ローカル名)、*シンボルの検索* の行(パッケージ自身の依存を、
その回路図が使う名前で入れる)、*ネットリスト* の行(各行を、それが来たセルの本体とシンボルに結び付ける)が変わりました。その内容は
[段階 2 の状況](#phase-2-status)にあります。

| | 場所 | すること / 足りないこと |
|---|---|---|
| git 依存 | `store/deps.py` | `project.json` の `libraries` に `{git, rev, subdir}`。`undine.lock`(`format: 1`)が `commit` と `sha`(`git archive` が出したファイルについて、全ファイルのパスと SHA-256 をまとめた SHA-256、`tree_sha`)を固定する。`.undine/deps/<name>/` に読み取り専用で install する。`add` / `update` / `remove` / `sync` / `lock` / `status`。依存側の manifest、バージョン範囲、推移的な依存、キャッシュ(取得のたびに一時的な bare repo を作る)はない。`read_lock` は `format` を見ない。段階 0 から: `read_lock` は 1 以外の `format` を拒否し(より新しいものは「made by a newer Undine」として)、`sync` は `project.json` から消えた依存を消し、`status` はそれを `leftover` と報告する。HTTP がローカルのリポジトリを受け付けるのは `--dep-root` の下だけ |
| シンボルの検索 | `xschem.py:311-324`、`project.py:219-234` | `C {<lib>/<cell>.sym}` を利用側のローカルなライブラリ名で探す。まず回路図自身のディレクトリ、次に検索パスの先頭から。依存が別のライブラリのシンボルを置いていると、利用側がそのライブラリを同じ名前で追加したときだけ動く。Xschem のネットリスタのバックエンド(`schematic.xschem_netlist`)と、Undine の外で開いた Xschem も、同じパスの一覧(`XSCHEM_LIBRARY_PATH`)を使う |
| ネットリスト | `schematic.py:589`、`xschem.py:482` | サブサーキットはシンボルのファイル名の stem だけで名付ける(`subckts[symname]`)。1 つのネットリストに同じ名前の別のセルが 2 つあると、先のものが黙って勝つ(拒否するのは stand-in の衝突だけ、`schematic.py:527-530`)。段階 0 から: 同じ名前で別の行にネットリストされる 2 つのセルは、両方を示して拒否する。同じ行なら一度だけ書く |
| レイアウト | `layout.py:1340-1377` | 別のライブラリのセルを置くとコピーする。同じ名前で中身の違うサブセルは `name$1` になる。置くセルの名前がレイアウトにすでにあれば、中身にかかわらずそのまま残す(`_copy_cell_from` は何も返さない)。操作ログに残るのは `src: {lib}` と内容の `sha` で、依存の commit は残らない |
| PDK | `pdks/pdk.py` | 起動時に一度だけ `$PDK_ROOT`、`~/.ciel`、`~/.volare`(または、それらの代わりに `--pdk-root`)から探す。`Pdk.version` は PDK の元の commit 全体(sky130 と gf180mcu は open_pdks、ihp-sg13 は IHP-Open-PDK)を持つが、`project.json`、`undine.lock`、provenance に記録されるのは 12 文字の `Pdk.id`(`sky130A@8afc8346a57f`)。木のハッシュも install も再走査もない。固定した PDK がなければ PDK なしでプロジェクトを開き、`status` は「locked X, here None」と出すだけ。install した後の木には書き込みがある: sky130 の `libs.tech/klayout/python/cells/__pycache__`(PCell のコードを import したとき)、IHP の `libs.tech/ngspice/osdi/*.osdi`(`scripts/osdi.sh` が作る。段階 0 からは Undine のキャッシュ `~/.cache/undine/osdi/<PDK の id>/` にコンパイルし、PDK はインストールしたときのまま) |
| プロセスの知識 | `pdks/pdkprofile.py` | family ごとの Python の `Profile`(レイヤー、deck、モデル、PCell ライブラリ、スタックの規則)。PDK と一緒にバージョン管理されていない。サポートする commit が 3 か所(`pcell.SUPPORTED`、`pixi.toml`、`setup.sh`)に書かれている |
| ツール | `tools.py:43-63`、`tools.py:116-131` | lock はバージョンのバナー文字列をそのまま比べる。どの実行ファイルが答えるかは探索順(`PATH` より先に `$UNDINE_TOOLS` か `~/undine-tools/bin`)で決まるので、CLI とサーバーで食い違うことがある。この環境では klayout は `~/undine-tools/bin` の 0.28.17 が答え、`/usr/bin` には 0.30.12 があり、どちらも pixi のものではない |
| provenance | `store/provenance.py` | ジョブの入力に、セルの内容のハッシュ(ネットリストのハッシュは生成したサブサーキットを、レイアウトのハッシュはコピーしたセルを含む)、PDK の id、ツールのバナー、KLayout の deck の SHA-256 を記録する(deck が変われば結果は古くなる)。モデルのファイル、PCell のコード、PDK の残りはハッシュしていない |
| changeset、エージェント | `api.py:1592-1595`、`agent.py:20` | 依存の変更はプロジェクトに直接書き込まれ、changeset のプレビューでは拒否される。エージェントは依存を追加できない。`project.json` と `undine.lock` はテキストとしてマージされる(`merge.md`) |
| ロック | `server/api.py`、`store/deps.py` | サーバーは依存の変更の間、サーバー全体の `srv.lock` を持つ。CLI は `project.json` と `undine.lock` を書くときにロックを何も持たないので、CLI とサーバーが同じプロジェクトに書くと競合する。段階 0 から: 取得の間に持つのはプロジェクトの `.undine/deps.lock` だけで、2 つのファイルを書くもの(リクエスト、CLI、adopt と revert)はすべて `meta_lock`(`.undine/project.lock` への flock)を取る。`srv.lock` はファイルを入れ替える間だけ持つ |

途中で見つけた問題は[段階 0](#phases) で直します。`read_lock` がどんな `format` でも受け付ける。`set-pdk` は lock があるときしか
lock を書かない。`project.json` から消えた依存のファイルと lock のエントリを `sync` が残す(しかも検索パスがそれを見つけ続ける)。
HTTP の `add` / `sync` / `update` は、最大 600 秒の git fetch の間サーバー全体のロックを持つ。CLI はロックをまったく持たずに
`project.json` と lock を書く。HTTP が `file://` と絶対パスを受け付けるので、サーバーのディスク上の任意のリポジトリを読める。
ネットリスタは同じ名前の 2 つのセルのうち先のものを黙って勝たせる。`scripts/osdi.sh` がもう存在しないモジュールを import している。
段階 0 の前半で、`srv.lock` の中での取得、ロックを持たない CLI、`osdi.sh` を直し、段階 0 の残りでほかのものを直しました。それぞれの修正が
どこにあり、どのテストが確かめるかは[段階 0 の状況](#phase-0-status)にあります。

<a id="packages"></a>

## パッケージ

**パッケージ**は 1 つのライブラリ(今と同じ、セルの入ったディレクトリ)と、その最上位に置く manifest `undine-package.toml` です。
1 つの git リポジトリのサブディレクトリに複数のパッケージを置けます。人が手で書くので TOML にします。Undine は Python の
`tomllib` で読みます。

```toml
[package]
name = "analog_ip"                 # index の中で一意。ローカルなライブラリ名の既定値。[a-z][a-z0-9_]*
version = "1.2.0"                  # semver
description = "Bandgap, LDO and comparator for sky130"
license = "Apache-2.0"             # SPDX の式。公開には必須
authors = ["Kadomoto <k@example.org>"]
repository = "https://github.com/me/analog_ip.git"

[pdk]
family = "sky130"
variants = ["sky130A"]             # sky130A と sky130B はメタルスタックが違う
compat = ["sky130/2025"]           # 互換性タグ(PDK プロファイルのパッケージが定義する)。または:
# commits = ["8afc8346a57fe1ab7934ba5a6056ea8b43078e71"]
built_with = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"   # レイアウトを描いて検証した PDK

[dependencies]
sar_common = "^1.2"                                # このパッケージを公開した index から
bias = { package = "bias_gen", version = "~0.3" }  # このパッケージの回路図では bias/<cell>.sym として置く
tech_cells = { version = "^2", index = "acme" }    # 別の index

[exports]
cells = ["bandgap", "ldo", "comparator"]          # 公開するセル。ほかのセルも入り、ネットリストにも入るが内部用

[files]
include = ["*.sch", "*.sym", "*.oas", "*.netlist.yaml", "*.intent.yaml", "rtl/**", "README.md", "LICENSE"]

[checks]                                          # `pkg check` がこのパッケージのセルで DRC / LVS をどう実行するか
drc = { deck = "sky130A_mr.drc", options = { feol = true, beol = true, density = false } }
waive = [{ cell = "unit_cap", rules = ["m1.2"], reason = "abutment cell: clean once arrayed" }]

[renames]                                         # 改名したバージョンごとに、古いセル名 -> 新しいセル名
"1.2.0" = { comp = "comparator" }
```

- **PDK の互換性**は守らなければならない条件です。PDK の commit には順序がないので範囲は書けません。パッケージは commit を
  列挙するか、互換性タグを書きます。タグ(`sky130/2025`)は、そこに属する commit がどれも同じレイヤー、規則、PCell の形、
  **そして同じデバイスモデル**(モデルのファイルとコーナー)を与えるという、PDK プロファイルのパッケージ(index ではない)による
  約束です。タグは 1 段です。形が同じでもモデルを変えた commit には別のタグを付けます。公開したセルのオフセット、帯域、スペックの
  結果がその下で変わるからです。レイアウトだけが互換というタグはありません。
  プロファイルの `[[pdk]]` の項目はライブラリごとの木のハッシュを持つので([PDK プロファイル](#pdk-profiles-as-packages)を参照)、
  更新や `set-pdk` のレビューは、モデルのファイルが違う commit を一覧にできます。PCell の形は生成器にもよります。Undine の
  `pcell` の pixi 環境(gdsfactory、kfactory)で動かす PDK の PCell のコードのことで、`GEN_VERSION` がそれを記録します
  (どの variant の名前にも入っている)。`built_with` は常に 1 つの commit です。
- **バイナリのレイアウト**は、プロジェクトのライブラリと同じく 1 セル 1 つの `.oas` ファイルのままにします。そうすれば新しい
  バージョンは、変わっていないセルを古いバージョンと共有できます([ストア](#fetch-and-store)を参照)。git LFS は前提にしません。
  パッケージに LFS のポインタファイルがあれば、理由を示して拒否します。
- **検証**(`[checks]`): パッケージの DRC に使う deck とそのオプション(単位セルや突き合わせのセルは単独では clean でないことが
  多く、密度とアンテナの規則は最上位でだけ効く)と、セルごとに免除する規則(それぞれ理由つき)。`pkg check` とレビューは免除を
  示します。
- 公開するパッケージが依存できるのは index のパッケージだけです。プロジェクトは今までどおり git 依存も使えます。

*実装済み(段階 2):* `store/manifest.py` がこのファイルを読み(`tomllib` で。Python 3.10 では `tomli` で)、最初の問題で拒否します。
キーのパスと書くべきことを示します(`<where>: undine-package.toml: <key path>: <what>: <fix>`)。知らないキーは拒否し、`[checks]` と
`[renames]` は表として受け付けますが、まだ使いません。index がまだないので、`[dependencies]` は git の形を取ります。上の index の形は
解析しますが、解決のときに拒否します。[段階 2 の状況](#phase-2-status)を参照してください。

<a id="index"></a>

## index

index は git リポジトリです。

```
config.json                      {"name": "undine", "format": 1}
an/al/analog_ip.jsonl            1 バージョン 1 行。追記するだけで書き換えない("yanked" を除く)
pr/of/profile_sky130.jsonl
3/i/inv.jsonl                    3 文字の名前: 3/<先頭の文字>/
2/op.jsonl                       1 文字か 2 文字の名前: 1/ か 2/
```

パッケージごとに 1 ファイル、バージョンごとに JSON を 1 行書きます。パスは crates.io-index と同じ決め方です。4 文字以上の名前は
先頭の 2 文字と次の 2 文字の下、3 文字の名前は `3/<先頭の文字>/` の下、1 文字か 2 文字の名前は `1/` か `2/` の下に置きます。

```json
{"name": "analog_ip", "vers": "1.2.0", "git": "https://github.com/me/analog_ip.git", "subdir": "lib",
 "commit": "229914698b1fb16a629bb73109b39eb1aba40eae", "tree": "sha256:e6d28176...",
 "pdk": {"family": "sky130", "variants": ["sky130A"], "compat": ["sky130/2025"], "built_with": "8afc8346a57f..."},
 "deps": [{"name": "sar_common", "req": "^1.2"}, {"name": "bias", "package": "bias_gen", "req": "~0.3"},
          {"name": "tech_cells", "req": "^2", "index": "git@github.com:acme/undine-index.git"}],
 "exports": ["bandgap", "ldo", "comparator"], "license": "Apache-2.0", "size": 1843200,
 "check": {"url": "https://github.com/me/analog_ip/releases/download/v1.2.0/undine-check.json", "sha256": "5f2a..."},
 "yanked": false}
```

`tree` は、commit そのもののバイト列に対するハッシュで、どのマシンでも同じになります。manifest が含めるファイル(manifest 自身も)
それぞれについて、パスの順に、`subdir` の下のパス、モード(`100644` か `100755`)、git が保存しているとおりの blob の SHA-256 を
まとめます。読むのは `git ls-tree -r` と `git cat-file` です。`git archive` は使いません。archive は `.gitattributes`
(export-subst、export-ignore、eol)とマシンの `core.autocrlf` を適用するので、公開する人、index の CI、利用する人が同じ commit を
違うハッシュにしかねないからです。シンボリックリンクとサブモジュールの項目は拒否します。

正確な符号化は次のとおりです(段階 1。`test_cas.py` の golden test、`test_tree_hash_golden_and_order_by_utf8_bytes` で固定しています):

- ファイルの**オブジェクトのハッシュ**は、生のバイト列(blob の内容。git のヘッダーなし。`git hash-object` は `blob <size>\0` を
  前に付けます)の SHA-256 で、小文字の 16 進 64 文字で書きます。
- **木のハッシュ**は `"sha256:"` に、各ファイルについて `subdir` の下の POSIX のパスの UTF-8 のバイト列の順に、
  `f"{mode} {hex} {path}\0"` の UTF-8 のバイト列をつなげたものの SHA-256 を付けたものです。`mode` は `100644`(実行ファイルなら
  `100755`)、`hex` はオブジェクトのハッシュ、`path` は `/` で区切った相対パスで、`.` や `..` の部分を含みません。
- ストアは木を `trees/sha256/<hex>.json` に `{path: [mode, hex]}` として記録します。名前は木のハッシュの 16 進の部分です。
- PDK の部分のハッシュ([lockfile](#lockfile))は、同じ符号化をディスク上のファイルに対して行います。モードは所有者の実行ビットから
  決めます。

バージョンの内容を固定するのは commit と
tree で、署名はありません(署名は[後で](#decisions))。あるバージョンの commit と tree は変わりません。**yank** は `yanked: true` に
することで、これも PR で行います。解決では飛ばし、すでに固定している lock はそのまま使え、`status` が警告します。`check` は、
公開する人の検証の報告へのリンクとその SHA-256 です([公開](#publishing)を参照)。

**既定の index** は `undine` です。GitHub の kadomoto-lab の organisation に置くリポジトリ
`https://github.com/kadomoto-lab/undine-index.git` です(リポジトリ名は仮のもので、リポジトリは[段階 3](#phases) で作ります)。URL は
Undine の設定にあります。当面、その PR は kadomoto がレビューします。

**複数の index。** ほかの index は `$UNDINE_HOME/config.toml`
(`UNDINE_HOME` の既定は `~/.undine`)に名前を付けて書き、プロジェクトの clone でも動くように `project.json` にも書きます。

```json
"indexes": {"acme": "git@github.com:acme/undine-index.git"}
```

`index` を指定した要求は、その index で解決します。指定のない要求は、それを書いたパッケージの index を意味します(cargo の規則)。
プロジェクト自身の要求なら既定の index です。だから `acme` のパッケージが `sar_common` に依存すれば acme の `sar_common` を得て、
誰かが公開の index に同じ名前で出したものを得ることはありません。index は URL で識別します。名前はローカルなもので、`pkg pack` は
index の行に URL を書き、lock も URL を記録します。1 つのプロジェクトで 1 つのパッケージ名が 2 つの index から来ればエラーにし、
両方を示します。アクセスは git に任せます(ssh 鍵か credential helper。`GIT_TERMINAL_PROMPT=0` はそのままで、パスワードは
尋ねません)。index は `$UNDINE_HOME/store/index/<URL の sha256>/` に一度 clone し、更新するのは頼まれたとき(`pkg index update`、
**更新** ボタン)か、解決にない版が要り、人が同意したときだけです。

<a id="resolution"></a>

## 解決

1. プロジェクトの依存(`project.json` の `libraries`。新たに `{"package": ..., "version": "^1.2"}` も書ける)と PDK から始める。
2. パッケージごとに、そのパッケージへのすべての要求を満たし、yank されておらず、プロジェクトの PDK の commit と variant に
   互換で、自身の依存も解決できる、いちばん新しいバージョンを選ぶ(バックトラックする。グラフは小さい)。
3. **1 つのプロジェクトでは、1 つのパッケージ名に 1 つのバージョン。** 共通のバージョンがない 2 つの要求はエラーにし、両方の
   経路を示します(`app -> analog_ip 1.2 -> sar_common ^1.2` と `app -> sar_common ~1.0`)。理由: SPICE のサブサーキットも
   レイアウトのセルも、ライブラリではなくセル名で名付けられるので、`comparator` の 2 つのバージョンが 1 つのネットリストに
   入ると衝突します。セル名に接頭辞を付けて複数のメジャーバージョンを入れることは後回しにします([決定事項](#decisions)の 3)。
4. **セル名は重ならない。** 同じ理由はパッケージの間にも当てはまります。解決したすべてのパッケージのセル(公開するものも内部用の
   ものも。内部用のセルも同じように install され、ネットリストに入るから)、プロジェクトのライブラリのセル、PDK のライブラリの
   セルは、どれも違う名前でなければなりません。重なれば解決のエラーにし、セルと両方のパッケージを示します
   (`comparator: analog_ip 1.2.0 and sar_common 1.3.1`)。1 つのパッケージを 2 つのローカル名で置くのは 1 つのパッケージで、
   重なりではありません。代わりにサブサーキットとレイアウトのセルの名前にライブラリの接頭辞を付けることは、選びませんでした
   ([決定事項](#decisions)の 4)。
5. **ローカル名。** ライブラリごとに、その回路図が使うローカル名(`bias/x.sym`)と、それぞれがどのパッケージかを宣言し、lock が
   その表を持ちます。ローカル名はプロジェクト全体で共通です。解決した各パッケージは、置かれているローカル名のすべての下に
   install します(`.undine/deps/<ローカル名>/`。ストアへのハードリンクなので、2 つ目の名前に費用はかからない)。シンボルの
   検索パスは、ディレクトリの 1 つの一覧のままです。Xschem のネットリスタのバックエンドと、Undine の外で開いた Xschem には
   これが要ります。`XSCHEM_LIBRARY_PATH` はパスによる 1 つの全体の検索で、ライブラリごとの表は表せないからです。これで、依存の
   依存は利用側が追加しなくても動き、パッケージを別のローカル名で置け(`bias` → `bias_gen`)、ダイヤモンド(`A -> C`、`B -> C`)
   では `C` をローカル名ごとに一度だけ入れます。2 つのライブラリが 1 つのローカル名に別々のパッケージを当てれば拒否し、両方を
   示します。ローカル名に PDK のライブラリ名(今の `deps.add` と同じく)やプロジェクトのライブラリ名は使えません。ライブラリごとの
   表(1 つのローカル名が 2 つのパッケージを指せる)は選びませんでした([決定事項](#decisions)の 5)。
6. PDK の互換性は、直接の依存だけでなくすべてのパッケージについて確かめます。PDK を変える(`set-pdk`)とき、互換でない
   固定済みのパッケージがあれば、その一覧を示して拒否します。

*実装済み(段階 2)、git の要求についてだけ:* index がないので、要求はどれも 1 つの git の commit を指します。要求ごとに候補は 1 つで、
バックトラックはありません。`store/resolve.py` は `project.json` の `libraries` からグラフをたどります。バージョンは(パッケージ名、木)で、
manifest の `version` はラベルです。`^`、`~`、`=`、素の形、`*` の要求があれば、それと照らします。たどる間の問題はすべて集め、
「nothing was changed」で終わる 1 回の拒否にします。1 つのパッケージへの合わない 2 つの要求(両方の経路を、バージョン、rev、commit
とともに)、求めたものと違うパッケージ名、manifest のない依存先(素の git 依存を書けるのは `project.json` だけ)、index の形、
リモートの要求元が書くローカルの URL、壊れた manifest、2 つのパッケージに当てたローカル名、予約されたローカル名、セルの重なり、
パッケージが受け付けない PDK です。`add` と `update` は、名前を指定した要求の下だけを解決し直します。ほかの固定済みのパッケージは
固定したとおりに保ち、ストアかその install から読み、取得はしません(`store/locked.py`)。セル名(規則 4)は、論理のビュー(`.sch`、
`.sym`、`.sv`、`.v`)の間で、大文字小文字を区別せずに比べます。プロジェクトのライブラリはそれだけを出すので、パッケージから置いた
レイアウトのコピーが重なることはなく、パッケージのレイアウトだけのセルは、自身の依存の閉包と PDK に対しては問いません。ローカル名
(規則 5)も大文字小文字を区別せずに比べます。プロジェクトと PDK のライブラリ名のほか、`devices`、Xschem のライブラリと
`project.json` の `symbol_paths` の項目のサブディレクトリの名前も使えません。1 つのシンボルのパスで `.undine/deps` がそれらより
前にあるからです。PDK の互換性(規則 6)は `pdks/compat.py` で、family、variant、commit 全体、`built_with` を見ます。タグはまだ
知りません(段階 6)。[素の git 依存についての段階 2 の変更](#phase-2-status)のうち 2 つはここから来ます。`status` は、依存の論理の
セルと同じ名前のプロジェクトのライブラリの論理のセルを報告します(manifest のあるパッケージとなら問題、素の依存となら注記。解決が
重なりを拒否するのは、片方が manifest のあるパッケージのときだけ)。`lib add` は、上のように予約された新しいローカル名を拒否します。
`project.json` にすでにある名前は注記するだけです。

<a id="lockfile"></a>

## lockfile

`undine.lock` を `format: 2` にします。

```json
{
  "format": 2,
  "pdk": {"id": "sky130A@8afc8346a57f", "family": "sky130", "variant": "sky130A",
          "commit": "8afc8346a57fe1ab7934ba5a6056ea8b43078e71",
          "source": "ciel static-web:https://fossi-foundation.github.io/ciel-releases",
          "libraries": ["sky130_fd_pr", "sky130_fd_sc_hd"],
          "tree": {"libs.tech": "sha256:41c0...", "libs.ref/sky130_fd_pr": "sha256:9a7e...",
                   "libs.ref/sky130_fd_sc_hd": "sha256:0f3b..."},
          "verified": "profile_sky130 1.0.0"},
  "profile": {"package": "profile_sky130", "index": "https://github.com/kadomoto-lab/undine-index.git", "version": "1.0.0",
              "commit": "5b1c...", "tree": "sha256:77aa..."},
  "packages": {
    "analog_ip": {"package": "analog_ip", "index": "https://github.com/kadomoto-lab/undine-index.git", "version": "1.2.0",
                  "git": "https://github.com/me/analog_ip.git", "subdir": "lib",
                  "commit": "229914698b1fb16a629bb73109b39eb1aba40eae", "tree": "sha256:e6d28176...",
                  "names": ["analog_ip"], "deps": {"sar_common": "sar_common", "bias": "bias_gen"}},
    "bias_gen": {"...": "..."}, "sar_common": {"...": "..."}
  },
  "libraries": {"old_ip": {"git": "...", "rev": "v1", "commit": "...", "tree": "sha256:..."}},
  "tools": {"undine": "0.1.0 (3f9c2e1)", "pixi_lock": "sha256:13be...",
            "klayout": {"version": "0.30.12", "from": "PATH"}, "xschem": {"version": "3.4.5", "from": "pixi"},
            "ngspice": {"version": "47", "from": "pixi"}, "magic": {"version": "8.3.678", "from": "pixi"},
            "netgen": {"version": "1.5.272", "from": "pixi"}}
}
```

- **形式。** 段階 0 で、Undine は知らない `format` の lock を「より新しい Undine が作った」として拒否するようにします。それより
  前の Undine が format 2 を読むと、`pdk` のオブジェクトを問題として報告し、その `set-pdk` は `pdk` を文字列で上書きしてしまいます。
  そこで format 2 を書くのは、それを必要とする明示的なコマンドだけにし、ほかの変更のついでには書きません。しかもそのコマンドは
  段階 1 から入り、段階 1 は段階 0 の確認を含むリリースが出回ってから出します。古い Undine を使う共同作業者は、壊れた lock ではなく
  拒否を受け取ります。段階 1 で書くのは `pdk verify PROJECT --pin` と `lib lock PROJECT --format 2` で、どちらも 1 つの関数
  (`pdkops.upgrade`)を通ります。`pkg add` と `pkg migrate` も、[段階 3](#phases) でできれば書きます。`lib lock --format 2` が
  なければ、段階 1 では PDK のないプロジェクトが format 2 にできません(どちらのコマンドも、ここに入っていない PDK は拒否します。
  先に `pdk fetch`)。format 1 の lock は
  読め、今のコマンド(`lib add`、`set-pdk` など)はこれまでどおり format 1 で書きます。*実装済み(段階 0):* `read_lock` は、より
  大きな整数の `format` を「made by a newer Undine」として拒否し、1 以外のほかの値(文字列、`true`、`1.0`、`0`)を「no Undine
  writes」ものとして拒否します。`format` のない lock は format 1 として読みます。プロジェクトを変えるコマンドはどれも先に lock を
  読むので、拒否した lock は `project.json`、`undine.lock`、`.undine/deps/` をそのまま残します。*実装済み(段階 1):* `read_lock` は
  format 1 と 2 を読み、3 以上を同じように拒否します(「this one reads formats 1 and 2」)。format 2 の lock はどのコマンドの後も
  format 2 のままで、それを変えるコマンド(`lib add`、`update`、`set-pdk`、`lib lock`)は変えないところをそのまま残します。
- **packages** はパッケージ名をキーにします。2 つの index からの同じ名前は拒否するので、プロジェクトの中で名前は一意です。
  エントリは(index の URL、名前)で、`sync` は両方を確かめます。`names` は install するローカル名です。
- **PDK**: commit 全体(sky130 と gf180mcu は open_pdks、ihp-sg13 は IHP-Open-PDK)、ciel の取得元、取得したライブラリ、部分
  ごとの木のハッシュを持ちます。部分ごとなので、より多くのライブラリと一緒に入れた PDK も検証に通ります。ハッシュには、install
  した後に PDK の木に書き込まれるものを入れません。`__pycache__/` と `*.pyc`(PCell のコードを import したときにできる。段階 1
  からは PCell のワーカーも `PYTHONDONTWRITEBYTECODE=1` で動かす)と `libs.tech/ngspice/osdi/`(段階 0 より前にセットアップした
  install で、`scripts/osdi.sh` がそこにコンパイルしたもの)です。期待するハッシュは、その commit についてのプロファイルのパッケージの `[[pdk]]` の
  項目から取ります(`verified` がそれを示す)。こうして PDK は、最初に見たマシンにあったものではなく、公開されたハッシュと
  照合されます。sky130 の 1 つの variant(1.1 GB)のハッシュは一度なら数秒で済みます。Undine は結果をファイルのスタンプと一緒に
  `$UNDINE_HOME/store/pdk/` に保ち、スタンプが変わったファイルだけを読み直します。`project.json` の `pdk` は id(要求)のまま
  残し、lock が固定値です。両者が食い違えば `status` が報告します。`set-pdk` は常に両方を書きます。
- **ツール**(決定): ツールはパッケージの仕組みに入れず、pixi が固定します(Undine の `pixi.lock`)。lock には、Undine の
  バージョンと commit(`0.1.0 (3f9c2e1)`。追跡しているファイルが変わっていれば commit の後に `+dirty`。バージョンが違えば警告、
  同じバージョンの別の commit なら注記だけ)、その `pixi.lock` の SHA-256(pixi なしで入れた Undine では `null`。そのとき `status` は
  ツールが固定されていないと伝える)、そしてツールごとにバージョンの**番号**と実行ファイルの出どころ(`pixi`、`UNDINE_TOOLS`、
  `~/undine-tools/bin` なら `undine-tools`、pixi のものでないサーバー自身の conda や仮想環境なら `env`、`PATH`)を記録します。
  `tools.find` は `PATH` より先に `$UNDINE_TOOLS` か `~/undine-tools/bin` を探すので、`pixi.lock` のハッシュだけでは、どの実行
  ファイルが答えたかは分かりません。番号や出どころが違えば `status` が警告しますが、拒否はしません。format 1 の lock はバナーだけを
  記録しています。`undine` も `pixi_lock` もなく、ツールに出どころもないので、`status` は比べられないことを注記します。
  `project.json` の古い `tools.klayout` はなくします(もう書きも読みもしません。今あるキーはディスクにそのまま残します)。番号は今のバナーから、ツールごとの規則で取り出します
  (どの規則にも合わないバナーは番号なしの文字列として記録し、`status` がそう伝える)。

  | ツール | 今のバナー | 規則 |
  |---|---|---|
  | klayout(`-v`) | `KLayout 0.28.17` | `KLayout (\d+(\.\d+)+)` |
  | xschem(`-v`) | `XSCHEM V3.4.5` | `XSCHEM V(\d+(\.\d+)+)` |
  | ngspice(`-v`) | `ngspice-47 : Circuit level simulation program` | `ngspice-(\d+(\.\d+)*)` |
  | magic(`--version`) | `8.3.678` | `^(\d+(\.\d+)+)$` |
  | netgen(`-batch quit`) | `Netgen 1.5.272`(今と同じくビルド日付を切った後) | `Netgen (\d+(\.\d+)+)` |

- `libraries` には、今までどおり素の git 依存を書きます。format 2 では、その `tree` を index の木と同じように計算します(git の
  blob から、サブディレクトリの全ファイル、依存に manifest があればその `include` について)。format 1 の `sha`(`git archive` の
  出力に対するもの)は、そのエントリが次に変わるまで残します。
- *実装済み(段階 2):* index がないので、lock の新しいキーは上の例より少なくなります。`libraries` には、`project.json` の直接の git
  依存をすべて置きます。manifest のないものと、解決し直していない段階 1 のものは、段階 1 のエントリそのままです。段階 2 で解決した
  manifest のあるものには、`files: "include"`、`package`、`version`、`names`(すべてのローカル名。キーが先頭)、`deps`
  (`{ローカル名: パッケージ}`)が加わります。`packages[package]` には、manifest を通してだけ届くパッケージを
  `{package, version, git, rev, subdir?, commit, tree, files: "include", names, deps}` として置きます。`index` はありません(段階 3)。
  sibling は要求元の `git` と `rev` を記録します。1 つのパッケージ名は、`packages` のキーと `libraries[*].package` を合わせて一度だけ
  現れます。木が manifest の `include` の組だけを含むのは、エントリに `files: "include"` があるときだけです。`files` のキーがなければ、
  段階 1 と同じくサブディレクトリのすべてのファイルを含み、`sync` はこのキーを足しも消しもしません。format 1 の lock にはこれらの
  キーは入りません。依存を書く manifest や、キーと違うパッケージ名は、そこでは「needs lock format 2: run `lib lock PROJECT --format 2`
  first」として拒否します。このコマンドは、変換と解決を 1 回の書き込みで行うか、format 1 の lock をそのまま残します。`sync` はグラフ
  全体について固定どおりのままです。`libraries` と `packages` のエントリを、それぞれの `names` のすべての下に、できればストアから
  install し、足りないグラフ(パッケージがない、名前が 2 回使われている、予約された名前)は何かを読む前に拒否します。手で書き換えた
  lock も、そのまま信じます。[セキュリティ](#security)の信頼の注記を参照してください。

<a id="fetch-and-store"></a>

## 取得とストア

共有の、内容でアドレスするストア `$UNDINE_HOME/store/` を作ります(既定は `~/.undine/store/`。`UNDINE_STORE` でストアだけを
動かせる)。

```
objects/sha256/e6/d28176...       すべてのファイルを SHA-256 で。読み取り専用
trees/sha256/e6d28176....json     パッケージの 1 バージョン: {パス: [モード, オブジェクトのハッシュ]}
trees/archive/<sha>.json          format 1 のエントリについて同じもの。名前はその `sha`(git archive のファイル)
git/<URL の sha256>.git            取得に使った bare リポジトリ。次の取得のために残す
index/<URL の sha256>/             index の clone
pdk/<commit>-<variant>-<root>.json   PDK の木のハッシュとファイルのスタンプ。<root> は PDK の実際のパスの SHA-256 の先頭 16 進
                                  12 文字なので、2 つのルートにある 1 つの commit は記録を 2 つ持つ(バージョンのない PDK は
                                  nocommit-<root>-<name>.json)
build/<commit>/<variant>/osdi/    コンパイルした OSDI モデル(もう PDK の木には書かない。バージョンのない PDK は <commit> の代わりに
                                  local-<root>)
projects/<パスの sha256>.json       依存を追加、更新、sync、固定したプロジェクト: {path, synced}
tmp/                              書き込みはすべてまずここに入り、名前を変えて置く
.lock                             flock。依存の操作は共有で、`store gc` は排他で取る
```

ストアは `$UNDINE_STORE` があればそこ、なければ `$UNDINE_HOME/store` です。`UNDINE_HOME`(既定は `~/.undine`)には接続の設定も
あります。これらの変数と、`UNDINE_OFFLINE`、`UNDINE_STORE_LINK`、`UNDINE_CIEL`、`UNDINE_FETCH_MAX` は
[データの配置](../reference/data.ja.md#the-store)にあります。

- **取得**: 残しておいた bare リポジトリに commit を `git fetch --depth 1` し(サーバーが commit 単体を渡さないときだけ、今と
  同じく全 ref を取る)、[木のハッシュ](#index)の定義と同じく、ファイルを git のオブジェクトから直接読みます(`git ls-tree -r -z`、
  `git cat-file`)。取るのは通常のファイルだけです。ファイルはすべて `objects/` に入れ、何かを install する前に木を lock か index の
  エントリと照合します。新しいバージョンの変わっていない `.oas` はもうストアにあるので、**ファイル単位で重複を省けます**。
  *実装済み(段階 1、`store/gitsrc.py`):* 素の git 依存について、書いたとおりです。ファイルの数(100 000)と大きさ
  (`UNDINE_FETCH_MAX`、1 GiB)は、blob を読む前に `git ls-tree -l` で確かめます。format 1 のエントリは、`git archive` の出力に対する
  `sha`(段階 0 の定義。`.gitattributes` と `core.autocrlf` を適用する)を保ちます。archive は、段階 0 で空のリポジトリに深さ 1 で
  取得した後と同じものだけを持つ使い捨てのリポジトリで動かします: 残したリポジトリから深さ 1 で取得した(ローカルの読み出し)その
  commit だけを持ち、ref は 1 つも持ちません。そうすると `export-subst`(`%d`、`%D`、`%(describe)`)がそのときと同じに展開され、
  `%h` の長さも同じになり(git は見えるオブジェクトの数で省略の長さを決めるので、大きくなった残したリポジトリでは変わります)、
  `sha` も同じになります。git 自身が木の中で拒否する `.git` の部分(大文字小文字を問わず、`.git.`、`git~1` も)を持つパスは、
  `blob_tree` もどの install も拒否します。
- **install**: `.undine/deps/<ローカル名>/` はどれも、木から `objects/` へのハードリンクで作ります(別のファイルシステムのときや
  `UNDINE_STORE_LINK=copy` のときはコピー)。ファイルは読み取り専用です。`store verify` はオブジェクトをハッシュし直し、
  `status` は今と同じく install した木をハッシュし直します。*実装済み(段階 1):* ファイルシステムがリンクを断るとき(`EPERM`、
  リンク数の上限の `EMLINK`、`ENOTSUP`)もコピーにします。コピーも読み取り専用です。オブジェクトはリンクする前にどれもハッシュし
  直し、合わないものは git から読み直します。changeset の worktree も、install した依存をハードリンクします
  ([git ブランチ上の changeset](changeset-branches.ja.md))。
- **sync は固定どおり**: `sync` は lock に書かれたものをそのまま install し、`project.json` をもう満たさない lock(要求を変えた pull の
  後など)は拒否します。そのエントリを示し、`pkg lock` か `pkg update` を実行するよう伝えます。解決するのは `add`、`update`、
  `lock` と changeset の操作だけで、sync のついでには解決しません。だから sync が evidence やエージェントの見るものを変えることは
  ありません。今の `sync` は、変わった依存や固定していない依存を解決し直します。これは段階 1 で変わります(頼まれたときに解決し
  直すのは、今もある `lib lock` と `pkg lock`)。*実装済み(段階 1):* `lib sync` は、何かを読んだり取得したりする前に拒否し、
  エントリごとに示し(`analog_ip: project.json asks for rev v2, undine.lock locks v1`、または `not in undine.lock`)、
  `lib lock PROJECT` か `lib update PROJECT NAME` を実行するよう伝えます。`lib lock` は、`project.json` の指定どおりに lock が固定して
  いないものだけを解決し直します(取得もします)。sync は lock のエントリを書き換えません。format 1 のエントリは、`add`、`update`、
  `lib lock` がそのエントリを format 2 の lock に書くまで `sha` のままで、そのとき `tree` を得ます。
- **オフライン**(`--offline`、`UNDINE_OFFLINE=1`、ネットワークがないとき): 解決には手元の index の clone をそのまま使います。
  固定したすべての木がストアにあれば `sync` は動き、なければ足りないものを示します。プロジェクトを clone や pull した後の
  `sync` は、前に取得したバージョンならネットワークなしで済みます。*実装済み(段階 1):* `lib sync --offline`(または
  `UNDINE_OFFLINE=1`、HTTP では `{"offline": true}`)は、ストアの記録とオブジェクトから、次にその commit を持つ残したリポジトリから
  install し、どちらもないエントリをすべて示して拒否します(`offline, and not in the store: analog_ip @ 229914698b1f (sha
  e6d28176...)`)。何も変えません。ストアができる前に入れた依存は、ファイルが lock と合えば最初の sync でストアに取り込むので、
  その後はオフラインでも sync できます。
- **PDK**: `pdk fetch` は lock の取得元を使って `ciel fetch --pdk-family <family> <commit> -l <lib>...` を実行します。入れる先は、
  Undine が探す PDK のルートのうち最初の ciel のルート(`--pdk-root` があればそれ、なければ `$PDK_ROOT`、なければ `~/.ciel`。ciel
  の `--pdk-root` をそこにする)です。探すルートに ciel のルートが 1 つもなければ(`~/.volare` だけなど)拒否します。ほかの場所に
  取得した PDK は見つからないからです。ciel はそこにある自身のストアに入れます(コピーはしない)。その後 Undine はハッシュを取って
  lock と比べ、PDK を探し直します(新規: 再起動なしで探し直す)。install するのは ciel のままで、Undine は PDK の木を書き換えません。
  *実装済み(段階 1):* ciel のファミリーは variant から決めます(`ihp-sg13g2` → `ihp-sg13`)。ルートは、`ciel/` があるか、まだないか、
  空なら使えます。format 1 の id と、ciel で入れた PDK の固定は、ciel の既定の取得元
  `static-web:https://fossi-foundation.github.io/ciel-releases` を前提にします。volare で入れた PDK の固定は拒否します。12 文字の id は
  入っている PDK から広げ、なければ、ネットワークを使ってよいとき(`--network` か、ダイアログのチェックボックス)に `ciel ls-remote`
  で広げます。CLI は ciel を動かす前に尋ね、HTTP では確認のトークン([セキュリティ](#security))が要り、ジョブとして動きます。CLI は
  動いているサーバーの PDK を探し直せないので、**PDK を再検出** か `POST /api/pdks/rescan` を使うよう伝えます。
- `store gc` は、このマシンのどのプロジェクトも固定していないオブジェクトを消します(プロジェクトは、サーバーの `--root` と、
  sync したプロジェクトをストアに記録した一覧から見つける)。*実装済み(段階 1):* プロジェクトは、`--root` の下のもの、その
  changeset の worktree の lock、ストアの `projects/` の一覧です。それぞれが固定しているものと、その `.undine/deps/` に入っている
  ものを残します。読めない lock があれば、何も消す前に止まります。消すのは木とオブジェクトだけ(と 1 日より古い `tmp/` の中身)で、
  `git/`、`pdk/`、`build/` は残します。
- **vendor**(`pkg vendor`)。ファイルは公開する人のリポジトリに残るので、リポジトリが消されたり、private にされたり、
  force-push されたり(その commit が後で GC される)すると、そのバージョンはストアに持っていないマシンでは手に入らなくなります。
  そこでテープアウトは自分の写しを持ちます。`pkg vendor PROJECT DIR` は、固定したすべてのパッケージ、素の git 依存、プロファイルの
  パッケージの固定した木を、ストアから `DIR/vendor/<package>@<version>/` に書き出します(git 依存は `<name>@<commit>`)。あわせて
  `vendor/vendor.json`(エントリごとに index の URL、git の URL、subdir、commit、木のハッシュ。lock と同じ)と、`pkg licenses` に
  よる `vendor/LICENSES.json` と `vendor/LICENSES.md` を書きます。書いた木はどれもハッシュし直して lock と比べます。ストアにない
  木は先に取得し(先に尋ねる)、オフラインならそれを示して拒否します。テープアウトの書き出しはこれをバンドルの中
  (`<project>/tapeout/<target>/<top>-<time>/vendor/`)に実行し、`report.json` が vendor した木を一覧にします。PDK は
  `vendor.json` に記録します(commit、ciel の取得元、ライブラリごとの木のハッシュ、ライセンス)が、コピーはしません。ciel の
  リリースが保っており、sky130 の 1 つの variant は 1.1 GB あるからです。`pkg sync --from-vendor DIR` は vendor したディレクトリを
  ストアに読み戻し、どの木も lock と照合します。こうしてリポジトリもネットワークもなしに、プロジェクトの依存をもう一度 install
  できます。

<a id="publishing"></a>

## 公開

```sh
undine pkg init libs/analog_ip            # ライブラリから undine-package.toml を書く(セル、プロジェクトの PDK)
undine pkg check libs/analog_ip           # 公開に必要なこと。下に書く
undine pkg pack libs/analog_ip            # タグを付けた commit での木のハッシュ。index の行を表示する
undine pkg publish libs/analog_ip --index undine   # index の clone にブランチと commit を作り、PR を開く
```

`check` が求めること:

- 作業ツリーがきれいで、`repository` の URL が渡すタグの上にあること。タグは `v<version>`、リポジトリに複数のパッケージがあれば
  `<name>-v<version>`。
- 公開するセルにはどれも、`check_symbol` に通るシンボルと、ネットリストにできる回路図があること。
- レイアウトのあるセルはどれも、`[checks]` の deck、オプション、免除で、`built_with` について DRC と LVS に通ること。
- 公開するセルのうち意図のスペックを持つものは、それを実行すること(`check_specs`)。アナログの IP ではスペックが約束そのものです。
- 依存が index から解決できること。ライセンスがあること。サイズの上限(既定 50 MB)を超えるファイルと LFS のポインタがないこと。
- パッケージのコードブロック(`.control`、`shell`、`.include` / `.lib` のパス)と RTL を一覧にし、[信頼の規則](#security)が拒否する
  ものに印を付けること。

前のバージョンと比べて **semver の変化**も報告します。公開するセルを `[renames]` なしで消したり改名したりしたとき、シンボルの
ピンを消したり改名したりしたとき、公開するセルのレイアウトの外形、ピンの位置、ピンのレイヤーを変えたとき(利用側の配置と配線が
壊れる)は、メジャーバージョン(1.0 より前はマイナー)が要ります。新しいセルにはマイナーバージョンが、ほかのレイアウトの変更には
少なくともパッチが要ります。結果の変わったスペックも一覧にします。

**検証の報告。** `check` は `undine-check.json` を書きます。中身は、パッケージの名前、バージョン、実行した commit と木のハッシュ、
PDK の commit(`built_with`)とツール(lock の `tools` と同じ)、セルごとの DRC と LVS の結果(免除を適用したもの)、スペックの
結果、semver の報告、印を付けたコードブロックと RTL、公開する人のプロジェクトでの実行のジョブ ID です。報告は、そこに書いた木に
ついてだけ有効です。

**公開。** `publish` はパッケージのリポジトリに commit もタグも push しません。`repository` から commit を取得し直して木を
確かめ、その同じ木について通った報告を求め(なければ `check` をもう一度実行する)、index の PR からリンクできる場所に報告を置きます。
リポジトリが GitHub にあれば、公開する人が確認したうえで、タグの GitHub release の asset にします(`gh release upload`)。
そうでなければ、公開する人が `--check-url` で示す URL です。index の行には `check: {url, sha256}` を書き、`gh` があれば PR を
開きます。なければ、ブランチと PR を開く URL を表示します。PR の本文には、報告の要約(セルごとの DRC / LVS、理由つきの免除、
スペックの結果、semver の報告、印を付けたコードブロックと RTL)とリンクを載せます。既定の index のレビューをする人(当面は
kadomoto)は、その要約を読みます。証拠は公開する人の実行であり、PR にもそう書きます。

index 側の CI(Undine がワークフローのファイルを用意する)は、PDK の要らないことを繰り返します。取得と木の確認、名前の規則、
コードブロックと RTL の一覧、報告の確認(リンク先のファイルの SHA-256 が行のものと等しく、行の commit と木を示し、通っていること)
です。index の CI での DRC と LVS は、CI に PDK(sky130 で variant ごとに 1.1 GB)が要るので、後で行います
([決定事項](#decisions)の 9)。

<a id="cells-across-versions"></a>

## バージョンをまたいだセル

セルは名前で識別します。`[renames]` がバージョンごとに古い名前から新しい名前への対応を持つので、更新は「削除と追加」ではなく
「`comp` は `comparator` になった」と言え、人が同意すれば(その更新である changeset の中で)利用側の `C {analog_ip/comp.sym}` を
書き換えられます。安定したセル ID も考えましたが、選びませんでした。すべてのセルファイルに ID が要り、名前はすでに回路図、
レイアウト、ネットリストでの識別子になっているからです。

`pkg update`(と更新のレビュー)は、公開するセルごとに次を報告します。追加 / 削除 / 改名 / 変更、どのビューが変わったか
(シンボルのピン、回路図、レイアウト、意図)、どのスペックの結果が変わったか。それを置いている利用側のセル(今の `used_by`)。
古い形のままのレイアウトのコピー。これは操作ログの由来から見つけ、由来には
`src: {"lib": "analog_ip", "package": "analog_ip@1.2.0", "tree": "sha256:..."}` を記録するようにします(それより前のコピーは、
内容の `sha` を古いバージョンのセルと比べて見つける)。古くなる結果。

**レイアウトのコピーの更新。** パッケージのセルを置くと、そのレイアウトが利用側にコピーされます。もう一度置いたり sync したり
しても、レイアウトにすでにある同じ名前のセルは置き換わりません。そこで `dep_update` はコピーも更新します。由来が古いバージョンの
コピーしたセルはどれも、changeset の中で、同じ名前のまま新しいバージョンのセルとサブセルに置き換えます(同じ名前で中身の違う
サブセルは、今と同じく `name$1` になる)。利用側がその後で編集したコピー(中身が古いバージョンのセルともう等しくない)は置き換えず、
人に示します。レビューはそれぞれの更新をレイアウトの差分として出します。それを置いている利用側のセルの DRC と LVS は古くなり
(レイアウトのハッシュが変わる)、changeset の中で実行し直します。これがなければ、回路図と LVS の参照は新しいバージョンから
来るのに、レイアウトは古い形のままになります。

## PCell の variant と PDK のバージョン

生成した PCell のセルは、生成器の世代とパラメータで名付けられ(`pcell.cell_name`)、PDK の commit は名前に入りません。プロジェクトの
セルはどれも、その 1 つの PDK で生成されます。パッケージは、`built_with` で生成した variant のセルを持って配られます。互換な
タグの下ではそのまま使います(タグが同じ形を約束している。後で index の CI が PDK を持てば、列挙した commit ごとに同じ `pcell`
環境で生成し直して比べられる)。コピーしたセルが持ち込む variant のサブセルと同じ名前で中身の違うものが利用側のレイアウトにあれば、コピーの側の
サブセルが `name$1` になり(今のサブセルと同じ)、更新の報告に載せます。PDK の commit を variant の名前には入れません。入れると、
1 つの互換性タグの中では意味がないのに、既存の variant の名前がすべて変わるからです。

<a id="pdk-profiles-as-packages"></a>

## パッケージとしての PDK プロファイル

`pdkprofile.py` をデータにします。family ごとに 1 つのパッケージ(`profile_sky130`、`profile_gf180mcu`、`profile_ihp_sg13g2`)を
作り、今の `Profile` が持つもの(レイヤー、メタル、deck とそのオプション、Magic / Netgen のファイル、モデルとコーナー、PCell
ライブラリ、スタックの規則、LVS の素子、制限)を `profile.toml` に書きます。さらに次を加えます。

```toml
schema = 1                 # プロファイルの形式。それを知らない Undine はプロファイルを拒否する
undine = ">=0.2"           # このプロファイルを読める Undine のバージョン

[[pdk]]
commit = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"
compat = ["sky130/2025"]
tested = true          # pcell.SUPPORTED と、pixi.toml / setup.sh にあるその写しの代わり
license = "Apache-2.0"
tree = { "libs.tech" = "sha256:41c0...", "libs.ref/sky130_fd_pr" = "sha256:9a7e...",
         "libs.ref/sky130_fd_sc_hd" = "sha256:0f3b..." }   # 期待する PDK の木のハッシュ。部分ごと
```

コードは入れません。計算する唯一の欄(`LvsElement.params`。sky130 の MIM の面積と周長)は、Undine が実装する名前付きの式に
します(`params = "mim_area_perimeter"`)。知らない欄は無視せず拒否します。`Profile` は Undine の機能とともに増え(strap、ring、
fill、stack、`erc_tap` など)、欄を黙って落とす Undine は DRC や LVS を違うやり方で実行してしまうからです。`verified` と
`magic_drc` は Undine の側に、family とプロファイルのバージョンをキーにした表として残します。これは Undine 自身のテストが何を
実行したかの記録で、PDK についての事実ではないからです。index がないときのために、Undine は今のプロファイルを組み込みで持ちます。
プロファイルの変更(deck のオプション、モデルのパス)はプロファイルのパッケージの更新で、ほかの更新と同じくレビューします。
ジョブが使うプロファイルの欄はジョブの入力に入るので([evidence](#changesets-and-evidence)を参照)、変わった欄を使った結果は
古くなり、ほかの結果は古くなりません。

## ライセンス

公開には `license`(SPDX)が必須です。PDK のライセンスはプロファイルのパッケージから取ります。`pkg licenses PROJECT` は、
固定したすべてのパッケージと PDK のライセンスを一覧にします。`pkg vendor` はその一覧を、vendor した木と並べてテープアウトの
バンドルに書きます([取得とストア](#fetch-and-store)を参照)。サードパーティの IP や参照データは
パッケージとして取得し、Undine にもプロジェクトにもコピーしません(たとえば `server/undine/data/tt_sky130A_analog.json` が候補)。

<a id="changesets-and-evidence"></a>

## changeset と evidence

依存の変更は、ほかの編集と同じく **changeset の操作**にし、レビューして採用します。

- `dep_add` / `dep_update` / `dep_remove` / `pdk_set` は changeset の作業コピーの `project.json` と `undine.lock` を変え、作業
  コピーの `.undine/deps` にストアから install します(ハードリンクなので軽い)。changeset はそのまま新しいセルを置き、検証を
  実行できます。プレビューは依存の変更を拒否しなくなります。`dep_update` はレイアウトのコピーも更新します
  ([バージョンをまたいだセル](#cells-across-versions)を参照)。
- レビューには、lock の差分、上に書いた更新の報告、ライセンス、ダウンロードの大きさ、パッケージのコードブロックと RTL の
  ファイルを出します([セキュリティ](#security)を参照)。
- **採用**では `project.json`(`libraries`、`indexes`、`pdk`)と `undine.lock` をエントリ単位でマージし、もう一度解決します。
  マージした要求が解決できないとき、またはマージした lock がそれを満たさないときは、理由を示して採用を拒否します。その後、
  ストアからプロジェクトに install します。採用の時点では何も取得しません。これは新しいことです。今の採用は git のマージで、
  2 つのファイルはテキストとしてマージされます(`merge.md`)。そのため、それぞれ依存を変えた 2 つの changeset は行の衝突を起こすか、
  install した木と合わない lock にきれいにマージされてしまいます。段階 4 で、Undine の git のマージドライバにこの 2 つのファイルの
  マージの規則を加えます。
- 人がライブラリブラウザで自分で **追加** しても、同じ操作を小さな changeset に作り、1 クリックで採用します。経路は 1 つです。
- **エージェント**は、手元の index の clone をそのまま使って、自分の changeset に `dep_add` / `dep_update` / `dep_remove` を
  提案できます。取得はしません。木がストアになければ、操作を *取得が必要*(URL、大きさ)として記録し、レビューに **取得** ボタンを
  出します。取得するのは人が確認したときだけです([HTTP](#ui-cli-http-agent-tools) のトークンを参照)。index の更新と PDK の取得も
  同じです。取得するまで、レビューはその操作を *未検証* とします(changeset の中でパッケージのセルを置いても検証してもいない)。
  人の取得が終わると、エージェントの changeset に通知が届き(`wait_for` で待っているエージェントはそれを見る)、操作は *install 済み*
  になり、エージェントはセルを置いて検証を実行できます。
- **evidence** はすでに `project.json` と `undine.lock` をハッシュしています。これに加えて、ジョブの入力の `key` の外に(参考と
  して。evidence が中身を厳密に示せるように)、そのジョブのセルが使った各 `package@version` と木、PDK の commit、プロファイルの
  パッケージとバージョンを記録します。古いかどうかの判定は、これまでどおりジョブが読むものにもとづきます。セルの内容
  (ネットリストのハッシュは生成したサブサーキットを、レイアウトのハッシュはコピーしたセルを含む)、deck の SHA、そして新たに、
  ネットリストが読み込むモデルのファイル(`.lib` / `.include` の対象と OSDI のファイル、ハッシュで)と、ジョブが使うプロファイルの
  欄(deck のオプション、deck の変数、モデルのパス)です。パッケージの README や関係のないセルの変更、プロファイルのレイヤーの色の
  変更では、何も古くなりません。

<a id="ui-cli-http-agent-tools"></a>

## UI、CLI、HTTP、エージェントのツール

- **ライブラリブラウザ**: *パッケージ* の欄で index の clone を検索します。パッケージのページには、バージョン、ライセンス、説明、
  シンボルのプレビュー付きの公開セル、依存、このプロジェクトの PDK に合うか(合わなければその理由)を出します。依存の行には
  `analog_ip 1.2.0` と、あればより新しいバージョンを出します。
- **lock のバッジ**: 今と同じ `locked` / `N lock issues` に加えて、PDK(検証済み / 未検証 / なし → **取得...**)、更新できる
  バージョン、yank されたバージョンを出します。*実装済み(段階 1):* PDK の状態は PDK のバッジに出し、そのメニューには状態の
  説明、**取得...**(PDK がないか変更されているとき)、**PDK を再検出** があります。lock のダイアログは、lock の形式、依存ごとの
  固定のしかた(`sha` か `tree`、それと *ストアにない*)、PDK の行を示し、警告と注記は問題の下に薄い字で出します。
- **CLI**(`undine pkg ...`。素の git 依存には `lib ...` を残す):

  | コマンド | |
  |---|---|
  | `pkg search TEXT`、`pkg info NAME[@VER]` | index の clone から |
  | `pkg add PROJECT NAME[@REQ] [--as LOCAL] [--index I]` | 解決し、取得し(先に尋ねる)、固定し、install する |
  | `pkg update PROJECT [NAME] [--to VER] [--dry-run]`、`pkg lock`、`pkg outdated`、`pkg tree`、`pkg why NAME` | `lock` は取得せずに解決し直す |
  | `pkg remove`、`pkg sync [--offline] [--from-vendor DIR]`、`pkg licenses` | `sync` は lock をそのまま install する |
  | `pkg vendor PROJECT DIR` | 固定した木とそのライセンス。テープアウトのバンドル用 |
  | `pkg init`、`pkg check`、`pkg pack`、`pkg publish`、`pkg yank NAME@VER` | 公開 |
  | `pkg index list / add NAME URL / update [NAME]` | index |
  | `pdk list`、`pdk fetch PROJECT`、`pdk verify PROJECT [--pin]` | ciel を通した PDK |
  | `store verify`、`store gc` | ストア |

- **HTTP**: `GET /api/packages?q=`、`GET /api/packages/{name}`、`POST /api/indexes/{name}/refresh`、changeset の操作
  `POST /api/p/{p}/changesets/{id}/deps`、`POST /api/p/{p}/deps/fetch` と `POST /api/p/{p}/pdk/fetch`(進み具合の出るジョブ)、
  `POST /api/pdks/rescan`。今の `/deps` のエンドポイントもそのまま使えます。どれも `docs/reference/api.md` に載せます(今は
  `/deps`、`POST /libs`、`GET /api/pdks`、`POST /api/p/{p}/pdk` が載っていない)。*実装済み(段階 1):* `POST /api/pdks/rescan`、
  `GET /api/p/{p}/pdk/fetch-plan`(取得のダイアログが示すもの)、`POST /api/p/{p}/pdk/fetch`、`GET .../pdk/fetch/{job}`。
  `/deps/sync` は `{offline}` を受け取り、`/deps/lock` は解決し直します。どれも [HTTP API](../reference/api.ja.md#pdk-fetch) に
  あります。`lib lock --format 2` と `pdk verify --pin` のルートはありません。段階 1 ではコマンドラインだけです。
- **確認のトークン。** HTTP からの取得、index の更新、PDK の取得には、これが要ります。UI は、URL、大きさ、ライセンスを示す
  ダイアログで人がクリックした後にだけ、`POST /api/confirmations {kind, url, commit, tree}` でトークンを求めます。トークンは
  1 回だけ使え、5 分で切れ、その(URL、commit、tree)の組(PDK なら commit、variant、ライブラリ)だけに使え、その応答でだけ返します。
  MCP のツール、`undine call` のコマンド、Python クライアントのメソッド、組み込みのエージェントのツールは、どれもトークンを求めず、
  持たず、返しません。はっきり書いておくと、Undine にはまだ認証がなく、リクエストの actor はリクエストが名乗るとおりです(今の
  採用や evidence の prune と同じ)。だから、シェルを持ち、人と同じようにサーバーに触れるプロセスは、人のブラウザができることを
  何でもできます。トークンが止めるのは、Undine のツールを通して動くエージェントと、うっかりした操作です。人として動く悪意の
  あるプロセスへの守りではありません。*実装済み(段階 1)、PDK の取得についてだけ*(決定: 段階 4 でほかの種類に広げる):
  `POST /api/confirmations {kind: "pdk", project, commit, variant, libraries, allow_network?}` は、リクエストをサーバーがいま作る取得の
  計画と照らし(`commit` は計画の先頭 7 文字以上でよい。format 1 の id は 12 文字なので)、トークンをプロジェクトと計画の全体
  (ファミリー、variant、commit 全体、ライブラリ、取得元、ルート)に結び付けます。ほかの種類、changeset のプレビュー、ciel の既定
  でない取得元は拒否します。`POST /api/p/{p}/pdk/fetch {token}` はトークンを使い切り、計画を作り直して、その後に変わった計画を
  拒否します。トークンがない、知らない、使用済み、期限切れ、別のプロジェクトのもの、はどれも 403 です。取得はメモリ上のジョブ
  (`GET /api/p/{p}/pdk/fetch/{job}`)として動き、再起動で失われます。`test_api_phase1.py` のテストが、どのツール、`undine call` の
  コマンド、クライアントのメソッドもどちらのルートにも届かないことを確かめています。*実装済み(段階 2):* 2 つ目の種類 `trust`。
  `POST /api/confirmations {kind: "trust", project, name}` はトークンと一緒に指摘を返し、トークンはプロジェクト、ローカル名、
  信頼のキー、install した木に結び付きます。`POST /api/p/{p}/trust {name, token}` はそれを使い切り、その後に変わったキーや木は
  拒否します。信頼を取り消すこと(`{name, untrust: true}`)にトークンは要りません。`test_agent_phase2.py` のテストが、どのツール、
  `undine call` のコマンド、クライアントのメソッドもどちらにも届かないことを確かめます。
- **エージェントのツール**(MCP と組み込みのエージェント): `search_packages`、`get_package`、`get_dependency_status`、
  `propose_dependency`(エージェントの changeset での追加 / 更新 / 削除)。パッケージと index の文字列(説明、README、セル名、
  意図のメモ、index の行)はエージェントにとってデータです。ツールは `agent.py` の DATA の規則のもとで返し、指示としては扱いません。
  `agent.py` のシステムプロンプトと `docs/guide/changesets.md` は「依存を追加できない」から「提案できる」に変えます。skill には
  依存についての短いリファレンスを加えます(今は依存に触れていない)。

## 移行

- **git 依存**はそのまま動きます。`pkg migrate PROJECT NAME` は、index のあるバージョンのファイル(その manifest が含めるもの)が
  install したものとファイルごとに等しければ、それを index の依存に変えます。git 依存が固定している commit はたいてい manifest より
  前のもので(manifest のない commit は公開できない)、しかも今の `sha` はサブディレクトリ全体を、index の `tree` は含めるファイルと
  manifest だけを対象にするので、2 つのハッシュは比べません。migrate は manifest のファイルの組で install し直し、変化を報告します。
  加わる manifest と、`include` の外にあって消えるファイルです。lock の format 1 は読み、format 2 は `pkg migrate` が書きます
  ([lockfile](#lockfile) を参照)。
- **PDK の id**: 12 文字の id(`sky130A@8afc8346a57f`)は、入っている PDK から(一意な接頭辞として)commit 全体に広げます。
  なければ、人がネットワークを許したときに `ciel ls-remote` から広げます。PDK は、その commit についてプロファイルのパッケージが
  列挙する木のハッシュと照合します。プロファイルが列挙していない commit は、見つけたハッシュで固定し、プロファイルが列挙するまで
  `status` とバッジで *未検証* と示します(初回を信頼すること。そうはっきり伝える)。バージョンのない PDK(素の `<root>/<name>/`
  ディレクトリ)は木のハッシュだけで固定し、警告します。`pdk_history` は残します。*実装済み(段階 1):* すべて。ただしプロファイルの
  パッケージはまだないので、合っている固定した PDK は *未検証* で、問題ではなく注記です(決定)。複数の commit に合う接頭辞は、
  それらを並べて拒否します。
- **lock のないプロジェクト**(lock より前に作ったもの)は、今と同じく最初の依存の変更で lock を得ます。ただし、作ったことを
  `status` が伝えるようにします。*実装済み(段階 1):* `status` は後からそれを知れないので、作った操作の結果として伝えます。
  `undine.lock` を作った CLI のコマンド(`new`、`set-pdk`、`lib` のコマンド、`pdk verify --pin`)はどれも
  `created undine.lock (format N)` と表示し、`relock`、`pdkops.upgrade`、`set-pdk` は `created` を返します。
- **プロファイル**: 段階 6 で family ごとの `Profile` を `profile.toml` に移し、読み込んだデータが今の Python のオブジェクトと
  すべての family で等しいことを確かめてから、Python の方を消します。

<a id="security"></a>

## セキュリティ

- **信頼。** パッケージはデータですが、それをシミュレーションすると、セルがシミュレータに渡す内容が実行されます。回路図の
  コードブロックは ngspice に渡り、`.control` は `shell` を実行でき、`.include` はサーバーの任意のファイルを読めます。パッケージの
  `rtl/` は Verilator を通り、`$c(...)` と `` `systemc_* `` のブロックは C++ です。そこで段階 2 から、ネットリストが依存のセルを
  含むとき、そのセルの `.control` のブロック、`shell` のコマンド、絶対パスかパッケージの外に出る(`..`)`.include` / `.lib` /
  OSDI のパスは、セルを示して拒否します。Verilator も、`$c` か `` `systemc_* `` を含むパッケージの RTL を拒否します。人は
  プロジェクトでパッケージのバージョンを信頼済みにできます(`project.json` の `"trusted": ["analog_ip@1.2.0"]`)。エージェントは
  それを提案できますが、レビューに出ます。更新すると外れます。依存の変更のレビューはパッケージのコードブロックと RTL のファイルを
  一覧にし、`pkg check` と index の CI はそれに印を付けます。PDK 自身のモデルのファイルと PCell のコードは、今と同じく PDK として
  信頼します。*実装済み(段階 2):* 中身を見つけるのは `store/trust.py` で、`.undine/deps` に入っている依存のうちプロジェクトが
  信頼していないものは、ジョブが動く直前にどれも確かめます。素の git 依存も確かめます。これが[素の git 依存についての段階 2 の
  変更](#phase-2-status)の 1 つ目で、依存のセルに `.control` か絶対パスの `.include` があるシミュレーションには、`lib trust` が要る
  ようになりました。拒否するものは、正確には次のとおりです:

    - **ngspice**(`start_sim`。実行を再利用する前): `.control` のブロックすべて(`shell` があってもなくても。`.subckt` の中でも。
      ngspice と同じく接頭辞で見るので `.controlx` や `.control;` も)、
      `*#` の行すべて、そして `.include` / `.lib` のカードや XSPICE の `file=`、`input_file=`、`state_file=` のパラメータが読むファイルの
      うち、ネットリスタの変数の置き換えの後も `$` か `~` が残るもの、`pdk.root` の外の絶対パス、`..` の部分を持つ相対パス。`+` の
      続きは先につなげます。スキャナが読めないカードは *unparsed* として拒否します。許す絶対パスのルートは PDK 自身のもの
      (`pdk.root`)だけで、依存の install のディレクトリは許しません。そこへの `.include` は、何も調べていないファイルを読みうるから
      です。1 行は、それがあるセルの本体と、それを作ったシンボルの両方に結び付けます(`schematic.Source`)。だから、信頼されていない
      セルで値を与えた信頼済みのシンボルの `@value` も、プロジェクトのセルに置いた信頼されていないシンボルのテンプレートの既定値も
      見つかります。生成したコーナーの `.lib` の行は通ります。`.subckt` の行も 1 行です(ピンは置いたシンボルのもの)。空白か改行を
      含むピン名やポート名は、ネットリストを作るときに拒否します。
    - **LVS**(KLayout と netgen): 同じパスの規則だけです。制御のコードを実行しないからです。実行が始まるとき(`Run.launch` で、
      ゲートを通して)確かめるので、sign-off の状況や changeset での再利用は今までどおり結果を示します。
    - **Verilator**(`rtl.py`。依存のライブラリの lint とシミュレーション): どこに現れても `$c`(と `$c8` ... `$c64`。`$clog2` など
      のシステム関数は含まない)と `$system`(マクロの引数も展開すれば呼び出しになる)、`` `systemc_* ``、`import` / `export "DPI-C"`、
      これらや `import`、`export`、`DPI` を含むかトークンをつなげる(``` `` ```)`` `define `` の本体、そして `` `include ``、`$fopen`、
      `$dumpfile`(シミュレーションは `--trace` つきでビルドする)、`$dumpports`、`$readmem*`、`$writemem*` の、絶対パス、`~` か
      `..` の部分を持つパス、リテラルでないパス。コメントは飛ばします。何もコンパイルしない lint は、`` `include `` のパスだけを
      確かめます。
    - **LibreLane**(`librelane.submit`、ツール `run_librelane`、実行のルート): トップのライブラリが依存であるフローは、Verilator の
      規則すべてで確かめます(その Verilator の lint と Yosys が `$readmem*` も含めて RTL を読むため)。回路図のゲートレベルの書き出しも
      同じです。何かをコピーしたり始めたりする前に確かめます。
    - **Xschem のネットリスタのバックエンド**(`backend=xschem` のネットリストのルート): 信頼されていない install の `.sch` か `.sym`
      にある `tcleval(`(Xschem は値のエスケープを外してから見るので、バックスラッシュをすべて除いて読む。`tcl\eval(` も数える)と、
      引数に `[` か `$` を持つ `expr(`。Undine 自身の前処理がそのセルをどう扱ったかにかかわりません。Tcl だけ
      です。そのネットリストは表示のためのもので、Xschem はネットリストを作るときにほかのものを評価しないからです。

    拒否は、セル、依存、行つきの各指摘を示し、人はそのバージョンを信頼できる(`undine lib trust PROJECT NAME`、または Undine の
    **信頼...**)がエージェントはできない、と伝えます。キーは `<package>@<version>` で、manifest のない依存は `<ローカル名>@<commit12>`
    です。`update`、`remove`、`lib lock` は、エントリの commit か木が変わればいつでもそれを外し、`sync` は取り除くもののキーを外し
    ます。ロックになかった内容でパッケージを解決する `add`(と `update`、`lib lock`)は、以前の内容から残ったその名前のキーを外します。
    名前とバージョンはどのリポジトリでも名乗れるからです。changeset の worktree は本当のプロジェクトだけから信頼を読みます(自身の
    `project.json` は開いたときの写しです)。だから、エージェントが changeset を開いた後に人が与えた信頼も、取り消した信頼も届きます。
    扱っていないもの: パッケージの
    キーは木ではなくバージョンのラベルを示すので、同じバージョンの別の commit に手で書き換えた(または pull した)`undine.lock` は、
    解決しない `sync` を通っても信頼を保ちます。キーを木に結び付ければ閉じられますが、決めずに残してあります。Undine の外で動かす
    Xschem や ngspice は守りません。シェルを持つプロセスが書き換える `project.json` の `trusted` の一覧も同じです。確認のトークンと
    同じく、この規則が止めるのは Undine のツールを通して動くエージェントで、その人として動く敵対的なプロセスではありません。

- パッケージ、index、プロファイルのそれ以外のコードは実行しません。プロファイルにあるのは名前付きの式だけです。PCell のコードは
  今と同じく PDK のもので、PCell のワーカーで動かします。
- ファイル: git のオブジェクトの通常のファイルだけ(シンボリックリンクもサブモジュールもなし)、絶対パスと `..` の禁止、大きさと
  ファイル数の上限、LFS のポインタの拒否。チェックアウトもフックの実行もしません(bare リポジトリ)。format 1 の lock を持つ素の
  git 依存は、エントリが次に変わるまで、今の `git archive` とその tar の `data` フィルタを使います。*実装済み(段階 1):* ストアの
  そろった記録とオブジェクトからの install は、リポジトリを読まず、git も動かしません(オブジェクトはリンクする前にどれもハッシュし
  直す)。残すリポジトリと archive 用のリポジトリは `git init --template=` で作るので、ユーザーの `init.templateDir` からフックは
  入りません。
- git: 今と同じく `protocol.ext.allow=never`。`file://` と絶対パスは CLI からは使えます。HTTP からは `--dep-root`
  (`--import-root` と同じ形)で指定した場所の下だけにし、ブラウザからサーバーのディスク上の任意のリポジトリを読めないように
  します。*実装済み(段階 0):* `serve --dep-root DIR`。何度でも指定でき、既定値はありません(既定がホームの `--import-root` とは
  違う)。指定しなければ HTTP はローカルのリポジトリを受け付けません。この確認は、リクエストの URL にも `project.json` が書く
  URL(`sync`、`update`)にも、最初の取得の前に行います。実際のパス(シンボリックリンクと `..` を解決したもの)を取り、存在する
  ディレクトリであることを求め、git がそこで読む git ディレクトリとその共通ディレクトリもルートの下にあることを求めます。git は
  その git ディレクトリから取得します。`localhost` 以外のホスト、クエリ、フラグメントを持つ `file://` の URL は HTTP からは
  拒否します。扱っていないもの: ルートの下のリポジトリの `objects/info/alternates` がその外のオブジェクトを指す場合(その `.git`
  への書き込み権が要る)。同じく段階 0 で、CLI と HTTP の両方について: `-` で始まる `rev` や `subdir` の部分、完全な 16 進の id
  でない固定した commit、ライブラリ名でない依存の名前や lock のキーを、git を動かす前、何かを消す前に拒否します。git には
  それらの前に `--end-of-options` と `--` を渡します。*実装済み(段階 1):* リポジトリを残すと、git は取得なしに commit を読めるので、
  これらの確認(`_check_spec`、`_check_commit`、`_own_dir`、この順)と、HTTP では `--dep-root` の確認を、取得の前だけでなく、残した
  リポジトリを読むたびにその前に行います。リクエストは、ルートの外の URL から残したリポジトリを、`lib` コマンドが取得したもので
  あっても読めません。ストアから install する sync はどれも読まないので、確かめません。
- **リモートのホスト。** 信頼できない clone の `project.json` や HTTP のリクエストは、どんな URL でも書けます。HTTP からの取得は、
  設定した index のホスト(`$UNDINE_HOME/config.toml`)、`--dep-host`(何度でも指定できる)で指定したホスト、または取得の
  ダイアログで全体を見せて人が確認した URL にだけ行きます。取得ごとに大きさの上限(`UNDINE_FETCH_MAX`、既定 1 GB)を設け、取得の
  途中で確かめます。全 ref を取る代わりの方法は、HTTP からはその確認なしには使いません。*実装済み(段階 2):* manifest が書く git の
  URL、rev、subdir は、`project.json` のものと同じ確認(`_check_spec`、HTTP では `_source` の `--dep-root` の規則)を、どの git の
  呼び出しよりも前に通ります。manifest の中のローカルの URL(`file://`、絶対パス、`~`)は、要求元自身の URL がローカルでなければ
  拒否します。リモートのパッケージが、マシンの上の private なリポジトリをプロジェクトに読み込めないようにするためです。リモートの
  ホストはまだ制限しません。段階 3 の `--dep-host` までは、HTTP からの追加、更新、固定は、たどった manifest が書くどのホストからも
  取得します。リクエストが書くどのホストからも取得するのと同じです。
- index の行は使う前に確かめます(スキーマ、名前の規則、URL のスキーム、ハッシュの形式)。木は、lock か index と一致して
  はじめて install します。
- **人が確かめる取得。** *実装済み(段階 1)、PDK についてだけ:* HTTP からの PDK の取得には、1 回だけ使える確認のトークンが要り、
  トークンはプロジェクトと計画そのもの(ファミリー、variant、commit 全体、ライブラリ、ciel の取得元、ルート)に結び付きます。
  それを求めるのは、人がクリックした後のブラウザのダイアログだけです。HTTP からは ciel の既定の取得元からだけ取得します
  ([確認のトークン](#ui-cli-http-agent-tools))。コマンドラインは ciel を動かす前に尋ねます。HTTP からの依存の取得には、段階 4 まで
  トークンは要りません(上の `--dep-root` の規則はかかります)。
- **署名**(後で行う)。当面、内容を固定するのは git のハッシュだけです。index の行の commit と木のハッシュ(blob に対する
  SHA-256 なので、git の SHA-1 には頼らない)で、取得、`sync`、`sync --from-vendor` のたびに確かめます。ファイルの変更、タグの
  付け替え、force-push は検出できます。公開できるのは index の PR をマージできる人で、index に push できる人は、別の内容を指す行を
  公開できてしまいます。index の commit やパッケージのタグへの署名は後で行います。
- **ロック。** 取得の間はサーバー全体のロックを持ちません。取得はストアにだけ書くジョブとして動きます(オブジェクトは一時的な
  名前で書いて名前を変えるので、2 つのプロセスが同じファイルを取得しても害はない)。プロジェクトのロックは
  `<project>/.undine/` の中の flock で、CLI もサーバーも短い間だけ持ちます。`project.lock` は `project.json` と `undine.lock` を
  書くたびに(changeset の adopt と revert でも)、`deps.lock` は依存の操作 1 つの間(取得の間も)持ちます。段階 0 で加えます。
  *実装済み(段階 0):* 2 つのロックを、この名前で(以前の案では `.undine/lock` 1 つでした)。取得は今もリクエストの中で
  (`srv.lock` なしで)、`.undine/deps/` の中の作業用ディレクトリへ行い、ストアへのジョブとしては動きません。*実装済み(段階 1):*
  依存の取得はストアに入れますが、今もリクエストの中で動きます(そのジョブは段階 4)。HTTP からの PDK の取得はジョブです。順序は
  `deps.lock`、次にストアの `.lock` を**操作全体の間、共有で**(取得、木の記録、install、lock の書き込み、`projects/` へのプロジェクトの
  記録)、次に残したリポジトリごとの flock(`git/<h>.git.lock`。読むほうは共有で取る)です。`store gc` はストアのロックを排他で取る
  ので、木を記録してからそれを書いた lock を書くまでの間に動くことはありません。オブジェクト、記録、スタンプのキャッシュ、
  プロジェクトの記録はどれもまず `tmp/` に書き、名前を変えて置きます。PDK のハッシュを取る間と ciel を動かす間は、プロジェクトの
  ロックもサーバーのロックも持ちません。1 つのルートへの 1 つの PDK の commit の `ciel fetch` は、プロセスをまたいで(CLI とどの
  サーバーでも)一度に 1 つだけ動きます: `pdkops.fetch` は取得とそれに続くハッシュの間、
  `<root>/ciel/<family>/.undine-fetch-<commit>.lock` の flock を持ちます。メモリの中のジョブのロックは、ブラウザからの 2 つ目の取得を
  待ち(queued)と見せるだけです。git が強制終了されて残したリポジトリに置いていったロックファイル(`shallow.lock`、
  `packed-refs.lock`、`refs/**/*.lock`)は、次にそこへ取得する前に、そのリポジトリの排他の flock(それに書くものはどれも持つ)の
  もとで消します。プロジェクトの `.undine` と `.undine/deps` は本当のディレクトリでなければなりません。そこがシンボリックリンクなら、
  依存の操作は何かを一覧したり、ロックしたり、作業用に置いたり、消したりする前に拒否します。

<a id="phases"></a>

## 段階

| 段階 | できること | テスト | 状況 |
|---|---|---|---|
| 0. 修正 | 読むときに `format` を確かめる(知らない形式は拒否)。`set-pdk` が常に lock を書く。`project.json` から消えた依存を `sync` が消し、`status` が残りを報告する。CLI とサーバーのためのプロジェクトの flock。HTTP の取得を `srv.lock` の外で行う。`--dep-root`。ネットリスタが、1 つのネットリストに入る同じ名前で中身の違う 2 つのセルを拒否する。`osdi.sh` を直す(まだ PDK の木に書く。段階 1 で移す)。載っていないエンドポイントの docs | `test_deps.py` の lock のテスト。`format: 2` の lock をこのコードが拒否する。CLI とサーバーが同時に 1 つのプロジェクトを sync しても lock と `project.json` が壊れない。遅い偽のリモートが別のリクエストを止めない。HTTP からの `file://` を拒否する。1 つのテストベンチに中身の違う `inv` を持つ 2 つのライブラリがあれば、両方を示して拒否する | **済み**。[段階 0 の状況](#phase-0-status)を参照 |
| 1. ストアと lock 2 | ストア、ファイル単位の重複の省略、固定どおりでオフラインの `sync`。git 依存の lock format 2(明示的なコマンドだけが書く)。git の blob からの木のハッシュ。除外の一覧つきの PDK の commit 全体と木のハッシュ、`pdk verify`、確認つきの ciel による `pdk fetch`(ルートは上のとおりに選ぶ)、PDK の再走査。`PYTHONDONTWRITEBYTECODE=1` で動く PCell のワーカー。ストアに作る OSDI: プロファイルの `osdi` の項目はモデルの名前(`psp103`)になり、まず `store/build/<commit>/<variant>/osdi/` で、次に PDK の木の今のパス(それを持つ install)で探す。`sim.py` と `doctor` は 1 つの関数からパスを得る。ツールのバージョン番号と出どころ | 2 つのバージョンが変わっていないオブジェクトを共有する。ストアからオフラインで sync する。`project.json` をもう満たさない lock を `sync` が拒否する。1 つの commit が `core.autocrlf=true` と export-subst のファイルがあっても同じハッシュになる。lock 1 → 2 は明示的なコマンドのときだけ。接頭辞 → commit 全体。`__pycache__` のある sky130 の木と `osdi/` のある IHP の木が検証に通る。PDK のファイルの変更を報告する。ciel は偽の実行ファイルで置き換える。`~/.volare` しか探さないとき `pdk fetch` を拒否する。IHP のシミュレーションがストアから OSDI を読み込む。上の各ツールのバナーから番号が取れる | **済み**。[段階 1 の状況](#phase-1-status)を参照 |
| 2. manifest と解決 | `undine-package.toml`。manifest からの推移的な依存(まだ git の URL)。プロジェクト全体で共通のローカル名と、それぞれの下への install。セル名が重ならないこと。PDK の互換性。コードブロックと RTL についての信頼の規則 | ダイヤモンドを一度だけ入れる。両立しない要求が両方の経路を示す。`comparator` を持つ 2 つのパッケージを、両方を示して拒否する。ローカルな別名で解決でき、Xschem のネットリスタのバックエンドでも解決できる。2 つのライブラリが 1 つのローカル名に別々のパッケージを当てれば拒否する。互換でない PDK を拒否する。`.control` か絶対パスの `.include` を持つ依存のセルを、信頼するまで拒否する | **済み**。[段階 2 の状況](#phase-2-status)を参照 |
| 3. index | index の形式、複数の index、既定の index を作る(`kadomoto-lab/undine-index`)、`pkg search / info / add / update / lock / outdated / tree / why`、バージョン範囲、yank、リモートのホストの方針と取得の大きさの上限、ライブラリブラウザのパッケージ欄。`pkg licenses`、`pkg vendor`(テープアウトの書き出しも実行する)、`pkg sync --from-vendor` | 一時ディレクトリの `file://` の index。yank したものは飛ばすが、固定済みなら残す。`project.json` に書いた private な index。`index` のない依存が、それを書いたパッケージの index から来る。2 つの index からの同じ名前を拒否する。一覧にないホストは、HTTP からは確認なしに拒否する。大きさの上限を超えた取得を止める。パッケージのリポジトリを消し、ストアを空にしても、vendor したバンドルから `sync --from-vendor` でオフラインに、固定した木のとおり install し直せる。手で変えた vendor のファイルを拒否する。テープアウトのバンドルに `vendor/` があり、`report.json` がそれを一覧にする | **予定** |
| 4. changeset とエージェント | changeset での依存の操作、依存の変更のレビュー(コードブロックと RTL の一覧)、確認のトークン、取得の後の通知、DATA の規則つきのエージェントのツール、git のマージドライバでの `project.json` と `undine.lock` のマージの規則(`merge.md` も直す)、`key` の外のパッケージと PDK の情報と、`key` の中のモデルのファイルとプロファイルの欄を含む evidence | changeset で追加、採用、元に戻す。エージェントの追加は *取得が必要* を記録し、取得せず、人の取得の後に続けられる。トークンを返すツールはない。別々のパッケージを追加する 2 つの changeset を、どちらの順でも採用できる。両立しない要求は採用のときに拒否する。1 つのセルを変える更新は、そのセルを使う結果だけを古くする。README の変更では何も古くならない | **予定** |
| 5. 公開 | `pkg init / check / pack / publish / yank`、`[checks]`、検証の報告と、index の行と PR からのそのリンク、semver の報告、報告に入るスペックの結果、`[renames]`、セル単位の更新の報告、レイアウトのコピーの更新、index の CI のワークフロー(DRC / LVS なし) | この木について通った報告がなければ publish は拒否し、PR の本文は報告をその SHA-256 とともにリンクする。SHA-256、commit、木が行と違う報告を index の CI が拒否する。改名したセルが改名として報告され、参照が書き換わる。ピンを消したりレイアウトのピンを動かしたりするとメジャーバージョンが要る。免除した規則は `check` に通り、示される。リポジトリが渡さない木の publish を拒否する。2 つのパッケージを持つリポジトリでのタグ `<name>-v<version>`。更新の後、コピーしたセルが changeset の中で新しくなり、その LVS が古くなる。編集したコピーは置き換えず、示す | **予定** |
| 6. PDK プロファイル | `schema` と `undine` を持つ family ごとの `profile.toml`、式の登録、期待する PDK の木のハッシュを持つ index のプロファイルのパッケージ、互換性タグ。`pcell.SUPPORTED`、`pixi.toml`、`setup.sh` はそこから読む | データのプロファイルが、sky130、GF180、IHP で今の `Profile` と等しい。知らない欄を拒否する。deck のオプションを変えると DRC の結果が古くなり、レイヤーの色を変えても古くならない | **予定** |

<a id="phase-0-status"></a>
### 段階 0 の状況

段階 0 の各項目がコードのどこにあるか。段階 0 の前半で行った項目はその直後のコードで、ほかの項目は段階 0 を終えたときのコードで確かめました。テストは、ほかに名前を書いていなければ `server/tests/test_deps.py` にあります。

| 項目 | 状況 | 場所 |
|---|---|---|
| 読むときに `format` を確かめる | 済み | `read_lock`(`store/deps.py:90-115`)は、より大きな整数の `format`(「made by a newer Undine ... update Undine」)、1 以外のほかの値(「which no Undine writes」)、解析できない JSON、オブジェクトでない JSON を、それぞれすべきことと「nothing was changed」を添えて拒否する。`format` のない lock は 1 として読む。読むものはすべてそれを拒否する: `lib add` / `update` / `remove` / `sync` / `lock` / `status`、`set-pdk`、`/deps` のルートと `POST /pdk`(HTTP 400。CLI は 1 行と終了コード 1)。`add` と `update` は取得の前に読み(`store/deps.py:402-472`)、`remove` と `set_pdk` は `project.json` が変わる前に読む。プロジェクトを開くこと(`GET /api/p/{p}`)は読まない。テスト: `test_a_lock_of_a_newer_format_is_refused_by_every_operation_and_left_byte_for_byte`、`test_a_lock_of_a_newer_format_is_refused_over_http_and_by_the_cli`、`test_a_lock_without_format_is_read_as_format_1_and_a_broken_one_is_refused` |
| `set-pdk` が常に lock を書く | 済み | `Project.set_pdk`(`store/project.py:164-187`)は、lock がないか別の PDK を固定しているとき、`project.json` がすでに新しい PDK を書いていても、`undine.lock` に `pdk` を書く。lock のないプロジェクトには、`lib lock` と同じように今のツールのバージョンを持つ format 1 の lock を新しく作る。ツールは `meta_lock` を取る前に答える。`cli.md`、`api.md`、`pdks.md` にそう書いてある。テスト: `test_set_pdk_writes_the_lock_whether_or_not_the_project_has_one`、`test_set_pdk_over_http_and_from_the_cli_creates_a_missing_lock` |
| `project.json` から消えた依存を `sync` が消し、`status` が残りを報告する | 済み | `leftovers`(`store/deps.py:351-369`): `project.json` が書いていない `.undine/deps/` のディレクトリか lock の項目を、固定した commit と install した commit とともに返す。`sync`(`store/deps.py:488-550`)はそれぞれを lock の項目とともに消し、`removed` と報告する。`sync` が取得している間に `project.json` から消えたものは次の sync に任せ、取得に失敗した sync は何も消さない。`status`(`store/deps.py:597-611`)はそれぞれを状態 `leftover` として、`lib sync` を示す問題とともに一覧にし(なので `ok` は false)、ライブラリ名でない lock のキーは `sync` が触らない問題として示す。`.undine/deps/` はシンボルのパスの 1 項目のままなので(`Project.symbol_paths`、`store/project.py:237`。理由もそこに書いてある)、残りもののシンボルは `sync` まで見つかる。ネットリストのキャッシュのキーは `.undine/deps/` のすべてのディレクトリを含む(`server/api.py:353-357`)。UI は状態を示し、まず sync を勧める。テスト: `test_status_lists_a_dependency_gone_from_project_json_and_sync_removes_its_files_lock_entry_and_symbols`、`test_a_sync_that_fails_removes_no_leftover_either`、`test_the_cli_and_http_report_a_leftover_and_sync_removes_it`、`test_status_reports_a_leftovers_installed_commit_from_its_files_not_from_the_lock`、`test_the_netlist_hash_changes_when_sync_removes_a_leftover_a_testbench_places`、`test_sync_never_removes_a_directory_a_lock_key_names_outside_the_deps_folder`、段階 0 からの `test_what_changed_while_fetching_is_not_written_back` |
| CLI とサーバーのためのプロジェクトの flock | 済み | `meta_lock` は `.undine/project.lock` への flock(`store/deps.py:110-115`)で、`_committing`(`store/deps.py:238-244`)、`relock`、`Project.editing`(`store/project.py:184-190`。`set_pdk` と `create_library` も)、changeset の adopt と revert(`store/changeset.py:2401-2404`)が取る。依存の操作を 1 つずつにする `.undine/deps.lock`(`store/deps.py:118-121`)。テスト: `test_project_files_are_written_one_writer_at_a_time`、`test_meta_lock_holds_across_processes_and_a_rename`、`test_an_adopt_waits_for_a_writer_of_project_json`、`test_an_operation_that_waited_works_from_what_the_one_before_wrote` |
| HTTP の取得を `srv.lock` の外で行う | 済み | `add`、`update`、`sync` は `.undine/deps.lock` だけを持って取得し、install と書き込みのときに呼び出し側の `guard`(`srv.lock`)を取る(`store/deps.py:238-244`、`server/api.py:1654-1694`)。テスト: `test_slow_fetch_does_not_stop_the_server`、`test_sync_is_all_or_nothing` |
| `--dep-root` | 済み | `serve --dep-root DIR`(`__main__.py:267-270`。何度でも指定でき、既定値はない)を `Server.dep_roots`(`server/api.py:141-143`)に入れ、`local_roots` として `add`、`update`、`sync` に渡す。`_source`(`store/deps.py:206-263`。`_local_path`、`_under_roots`、`_git_dirs` とともに)は、取得するすべての URL を、リクエストのものも `project.json` のものも、最初の取得の前に確かめる: ローカルのものは存在するディレクトリで、その実際のパス、git ディレクトリ、共通ディレクトリがルートの下になければならず、git はその git ディレクトリから取得する。ホスト、クエリ、フラグメントを持つ `file://` の URL は拒否する。CLI はルートを渡さず、どのパスも受け付ける。CLI についても: `_check_spec` と `_check_commit`(`store/deps.py:182-203`)は `-` で始まる `rev` や `subdir` の部分と、完全な 16 進の id でない固定した commit を、`_own_dir`(`store/deps.py:384-391`)はライブラリ名でない名前を拒否し、git には `--end-of-options` と `--` を渡す(`_fetch`、`store/deps.py:266-310`)。扱っていないもの: ルートの下のリポジトリの `objects/info/alternates`。テスト: `test_a_local_repository_is_refused_over_http_without_dep_root_and_the_cli_still_takes_it`、`test_a_local_repository_under_a_dep_root_is_added_updated_and_synced_over_http`、`test_a_path_that_escapes_the_dep_root_by_dotdot_or_a_symlink_is_refused_over_http`、`test_a_path_whose_git_directory_is_outside_the_dep_root_is_refused_over_http`、`test_sync_over_http_fetches_a_local_path_project_json_names_only_under_a_dep_root`、`test_serve_dep_root_is_repeatable_and_off_by_default`、`test_a_rev_or_a_lock_commit_that_git_would_read_as_an_option_is_refused_before_git_runs`、`test_a_subdir_that_looks_like_a_git_option_is_refused_before_anything_is_fetched` |
| ネットリスタが同じ名前で中身の違う 2 つのセルを拒否する | 済み | `spice` はサブサーキットの名前ごとに、その元の回路図を覚えておく(`homes`、`schematics/schematic.py:630-633` と `654`。最上位のセルも含む)。`same_name_check`(`schematics/schematic.py:563-584`)はその名前の 2 つ目のセルをネットリストにし、同じ行にならなければ、両方を示して `SameNameError`(`schematics/schematic.py:390`)を出す。同じ行なら一度だけ書く。なのでどのモードも、それとともにシミュレーション、LVS、スペック、スイープも、これを拒否する。vlsirtools のバックエンドは先に `spice` を動かす(`schematics/schematic.py:789`)。Xschem のバックエンドは Xschem を探す前に `XschemOrder`(`schematics/schematic.py:796-806`。Xschem 自身のシンボルの順)でそれを動かし、そのエラーのときだけ止まる(`schematics/schematic.py:815-823`)。PDK の起動用のページのいくつかは拒否されるようになった(sky130 の `sky130_tests/top`、IHP の `start_page`)。テストは `test_netlist.py` に: `test_two_cells_of_one_name_with_other_content_are_refused_naming_both`、`test_the_xschem_backend_refuses_only_what_xschem_itself_would_netlist_as_one_cell`、`test_every_pdk_schematic_netlists_the_same_from_its_netlist_view`。stand-in の衝突は今までどおり `test_substitute.py` に |
| `osdi.sh` を直す | 済み(それ以上) | `undine.pdks.pdk` を import し、`libs.tech/verilog-a` のコピーを `Pdk.osdi_dir`(`pdks/pdk.py:43-58`)にコンパイルするので、PDK はインストールしたときのまま。ストアなしでの段階 1 の一部。テストは `test_pdk.py` に: `test_a_pdk_with_verilog_a_models_needs_them_compiled` |
| 載っていないエンドポイントの docs | 済み | `GET /api/pdks`、`POST /libs`、`POST /pdk` と `/deps` の呼び出しを `docs/reference/api.md` に記載 |

段階 0 では、ほかに次のことも行いました。PDK の名前だけが指定されたとき、`pick_pdk` が PCell を実行できるバージョンを、
次に `pcell.SUPPORTED` を優先する(`store/project.py:115-121`、`test_pdk.py` の `test_a_bare_pdk_name_picks_the_supported_version`)。依存の操作が
依存のロックを取った後で `project.json` を読み直す。`sync` をすべて成功するか何もしないかにする。マシンのパスを `undine.lock` に
入れない(`test_refusals_and_drift`)。

段階 1 のうち 2 つの部分は段階 0 で先に入りました。OSDI を PDK の木の外(Undine のキャッシュ
`~/.cache/undine/osdi/<PDK の id>/`。まだストアではない)にコンパイルし、シミュレーションと `doctor` がそのパスを 1 つの関数
(`Pdk.osdi_files`)から読むこと。lock がツールのパスではなくバージョンを記録すること(まだバナーのままで、番号も出どころもない)。
段階 1 で両方を仕上げました([段階 1 の状況](#phase-1-status))。

<a id="phase-1-status"></a>
### 段階 1 の状況

段階 1 の各項目がコードのどこにあるかを、モジュールと関数で示します(`server/undine/`)。テストは `server/tests/` にあります。ciel は
偽の実行ファイル(`fake_ciel.py`)で置き換え、どのテストも一時的な `UNDINE_HOME` と `UNDINE_STORE` で動くので、`~/.undine`、
`~/.ciel`、ネットワークには触れません。

| 項目 | 状況 | 場所 |
|---|---|---|
| ストア | 済み | `home.py`(`home()`: `$UNDINE_HOME` か `~/.undine`。`store_root()`: `$UNDINE_STORE` か `home()/store`)と `store/cas.py`: バイト列の SHA-256 を名前にした、モード 0444 のオブジェクト。木の記録と format 1 の archive の記録(読むたびに名前とハッシュを照らす)。`install` はどのオブジェクトもハッシュし直してからハードリンクする(誤りがモードだけのもの、つまりプロジェクトのリンクを通した `chmod` は、読み取り専用に戻して残す)(`link_or_copy`: `EXDEV`、`EPERM`、`EMLINK`、`ENOTSUP` のとき、または `UNDINE_STORE_LINK=copy` なら読み取り専用のコピー)。`store_lock`、`register_project`、`verify`、`gc`。書き込みはどれも `tmp/` を通る。テストは `test_cas.py`: `test_tree_hash_golden_and_order_by_utf8_bytes`、`test_put_twice_keeps_one_read_only_object`、`test_install_links_read_only_files`、`test_install_copies_when_asked_or_when_linking_fails`、`test_install_restores_the_mode_of_a_good_object_changed_through_a_project`、`test_install_accepts_a_good_object_whose_mode_does_not_stick`、`test_an_object_edited_in_place_is_caught`、`test_gc_keeps_live_trees_and_frees_the_rest`、`test_gc_waits_for_a_shared_lock` |
| git の blob からの木のハッシュ、ファイル単位の重複の省略 | 済み | `store/gitsrc.py`: URL ごとに残す bare リポジトリ(`git/<URL の sha256>.git`。取得した commit は `refs/undine/<commit>` で残し、リポジトリごとに flock を 1 つ)、`fetch`(深さ 1。全 ref を取る代わりの方法はこれまでどおり。結果は固定した commit と照らす)、`blob_tree`(`ls-tree -r -z -l` の後に `cat-file --batch`。通常のファイルだけ。シンボリックリンク、サブモジュール、LFS のポインタ、ディレクトリの外に出るパス、`.git` の部分を持つパスは拒否し、blob を読む前にファイル数と `UNDINE_FETCH_MAX` を確かめる。取得の前に、残したリポジトリに残った git のロックファイルを消す。時間切れの git には先に SIGTERM を送る)。テストは `test_gitsrc.py`: `test_one_commit_hashes_the_same_with_autocrlf_and_export_subst`、`test_modes_enter_the_tree`、`test_two_versions_share_unchanged_objects`、`test_limits_are_checked_before_any_blob_is_read`、`test_blob_tree_refuses_what_cannot_be_installed`、`test_a_kept_commit_needs_no_network_and_survives_gc`、`test_ext_protocol_is_refused`、`test_a_git_part_in_a_path_is_refused_as_git_refuses_it`、`test_a_lock_file_left_by_a_killed_fetch_does_not_stop_the_next_one`、`test_a_timed_out_git_is_asked_to_stop_before_it_is_killed`。`test_store_sync.py`: `test_update_keeps_the_inode_of_an_unchanged_file` |
| format 1 のエントリは `sha` を保つ | 済み | `gitsrc.archive_tree`: 残したリポジトリから深さ 1 で取得したその commit だけを持ち、ref を持たない使い捨てのリポジトリで `git archive` を動かすので、`export-subst` は段階 0 の深さ 1 の取得の後と同じに展開される(残したリポジトリがどれほど大きくなっても `%h` も)。`cas.archive_sha` は段階 0 の `tree_sha` とバイト単位で同じ。テスト: `test_archive_sha_equals_phase0_in_a_kept_repository_with_refs`、`test_export_subst_h_is_abbreviated_as_phase_0_did_in_a_large_kept_repository`(`test_gitsrc.py`。段階 0 のコードの凍結した写しと比べる)、`test_a_format_1_lock_keeps_format_1_and_phase_0_shas`(`test_store_sync.py`) |
| 固定どおりの `sync`、`lib lock` が解決し直す | 済み | `store/deps.py`: `sync` は何かを読む前に `_stale` と `_frozen` で拒否し、エントリごとに示して `lib lock` / `lib update` を伝える。lock にエントリを書くことはない。`relock`(`lib lock`、`POST /deps/lock`)は、`project.json` の指定どおりに lock が固定していないものだけを、add と同じ確認のもとで取得して install する。古いものがなければ `deps.lock` を取らない。テストは `test_store_sync.py`: `test_sync_refuses_a_lock_that_no_longer_satisfies_project_json_and_lib_lock_resolves_it`、`test_a_project_with_git_dependencies_and_no_lock_is_refused_and_no_lock_is_made` |
| オフラインの `sync` | 済み | `deps.sync(offline=...)`: ストアの記録とオブジェクトから(リポジトリを読まず、git も動かさない)、なければその commit を持つ残したリポジトリから。どちらもなければ、足りないエントリをすべて示して拒否する(`_not_here`)。`lib sync --offline`、`UNDINE_OFFLINE=1`、`POST /deps/sync {"offline": true}`。lock のエントリと合うがストアにない install は取り込む(`_seed`)。形式 2 の install は、ストアの記録がなくても、目印が記録したモード(`modes`: モード 100755 のファイル)で、なければ 100644 として、`#!` で始まるファイルは 100755 として、その木と照らす。テストは `test_store_sync.py`: `test_offline_sync_installs_from_the_store_with_no_fetch`、`test_offline_sync_with_an_empty_store_names_what_is_missing_and_changes_nothing`、`test_an_install_made_before_the_store_is_imported_by_sync_and_then_syncs_offline`、`test_a_failed_online_fetch_is_named_with_its_commit_and_hash`、`test_a_tree_with_an_executable_file_is_ok_and_seeded_without_the_stores_record`、`test_a_script_is_found_by_its_shebang_when_the_marker_has_no_modes`、`test_a_chmod_through_one_project_does_not_cost_another_its_offline_sync`、`test_a_linked_deps_directory_is_refused_and_nothing_outside_is_touched` |
| ストアのロックと、残したリポジトリへの `--dep-root` の規則 | 済み | `deps._in_store` は最初の取得から `register_project` まで `cas.store_lock(shared=True)` を持つ。規則 (e) の確認は lock のデータを git に渡すどの `gitsrc` の呼び出しの前にも行い、HTTP では残したリポジトリを読む前に `_source` を行う(`_resolve` が git を読む直前にだけ呼ぶ関数として渡す)。テストは `test_store_sync.py`: `test_over_http_a_kept_repository_outside_the_dep_roots_is_not_read_but_the_store_installs`、`test_an_option_looking_locked_commit_is_refused_before_any_git_call`、`test_gc_waits_for_an_operation_between_its_tree_and_its_lock_write`、`test_the_store_lists_the_project_and_its_locked_and_installed_trees` |
| lock format 2、明示的なコマンドからだけ | 済み | `deps.read_lock` は format 1 と 2 を読む(`READS`)。`lock_pdk`、`lock_tools`、`entry_hash`。`add`、`update`、`lib lock` は format 2 の lock には `tree` のエントリを、format 1 の lock には `sha` のエントリを書く。`pdks/pdkops.py`: format 2 を書く唯一の関数 `upgrade` を、`pdk verify --pin` と `lib lock --format 2`(format 2 の lock の固定には触れない)が使う。テスト: `test_lock_format_2_only_on_an_explicit_command`、`test_lib_lock_format_2_prints_what_it_resolved_and_leaves_a_format_2_pin_alone`(`test_cli_phase1.py`)、`test_a_format_2_lock_gets_trees_from_update_and_sync_never_converts_an_entry`(`test_store_sync.py`)、`test_a_format_2_lock_is_read_and_lib_add_keeps_its_format_and_pdk_object`、`test_today_s_commands_write_format_1_with_banners_and_a_lock_without_format_is_format_1`(`test_lock_format2.py`) |
| PDK の commit 全体と木のハッシュ、除外の一覧つき | 済み | `pdks/pin.py`: `parts`(`libs.tech`、`libs.ref/<lib>` のそれぞれ、ほかの最上位の `libs.*` のそれぞれ)、`excluded`(`__pycache__/`、`*.pyc`、`libs.tech/ngspice/osdi/`)、`store/pdk/` のスタンプのキャッシュを使う `part_hashes`(git のインデックスと同じく、直前 2 秒以内に書かれたファイルは次にまた読む)、`pin_object`、`verify`、`expected_trees`(段階 6 のための継ぎ目。None を返すので、合っていれば *未検証*)。PDK の中のシンボリックリンク、FIFO、デバイスは、それを示すエラー。テストは `test_pdk_pin.py`: `test_a_sky130_tree_with_pycache_still_matches`、`test_an_ihp_tree_with_compiled_osdi_still_matches`、`test_a_part_hash_is_the_store_tree_hash_of_its_files_with_the_exec_bit_as_mode`、`test_a_changed_pdk_file_is_reported_by_part_and_file`、`test_an_extra_library_verifies_and_a_missing_locked_one_is_reported`、`test_a_second_hash_reads_no_file_and_a_touched_file_is_read_again`、`test_hashing_writes_nothing_into_the_pdk` |
| 接頭辞から commit 全体へ | 済み | `pin.resolve_commit`: その variant の install のうち一意に合うもの、なければネットワークを許したときに `ciel ls-remote --pdk-family F`。複数に合えば、それらを並べて拒否する。テストは `test_pdk_pin.py`: `test_a_unique_prefix_is_found_among_installs`、`test_an_ambiguous_prefix_is_refused_listing_both`、`test_an_unknown_prefix_offline_is_refused_saying_how_to_allow_the_network`、`test_with_the_network_the_fake_ciel_ls_remote_gives_the_commit_by_family`。`test_a_format_1_id_is_pinned_to_the_full_installed_commit`(`test_pdk_ops.py`) |
| `pdk verify` と `status` の PDK | 済み | `pdkops.verify`(全体のハッシュ。変わっていないところはスタンプのキャッシュから。`--pin` は `upgrade` を通る)と、`deps.status` が使う `pdkops.pdk_block`: スタンプのキャッシュだけを読み、PDK は読まないので、キャッシュが古ければ *未確認*。走査の間にファイルが消えれば(その間の `ciel rm`)、リクエストが失敗するのではなく *error* の状態になる。`ciel rm` の案内は、lock のファミリーからではなく、ここで見つけた install から作る。未検証、未確認、未固定は注記。要求と固定の食い違い、PDK がない・変更されている、固定したライブラリがない、は問題。テスト: `test_verify_without_pin_writes_nothing_and_pin_writes_format_2`、`test_a_modified_pdk_is_reported_and_pin_re_pins_it_reporting_what_changed`、`test_pdk_block_issues_and_notes`(`test_pdk_ops.py`)。`test_a_pinned_pdk_with_a_warm_cache_is_unverified_as_a_note_and_ok`、`test_project_json_changed_by_hand_is_an_issue_naming_set_pdk`、`test_no_stamp_cache_is_unchecked_and_reads_no_file_of_the_pdk`、`test_the_reinstall_hint_names_the_install_found_here_not_the_locks_family`、`test_a_pdk_file_that_vanishes_while_status_walks_it_is_an_error_state_not_a_crash`(`test_status_pdk.py`) |
| ciel による、確認つきの `pdk fetch` | 済み | `pdks/ciel.py`(`exe`: `$UNDINE_CIEL`、次に Undine の Python の隣の ciel、次に `PATH`。ciel のファミリーで `fetch`。`ls_remote`)、`pdkops.fetch_plan` と `fetch`、`pin.fetch_root`。CLI は先に尋ね(`--yes`)、HTTP はトークンが要り、ジョブとして動く(`pdks/fetchjobs.py`)。1 つの PDK の commit の取得は、プロセスをまたいで一度に 1 つ(flock。ロックの段落を参照)。テスト: `test_pdk_fetch_is_refused_when_only_a_volare_root_is_scanned`、`test_pdk_fetch_asks_then_installs_with_ciel_and_reports_verify`(`test_cli_phase1.py`)、`test_a_pdk_fetch_needs_a_fresh_token_bound_to_what_is_fetched`、`test_no_agent_tool_or_client_method_reaches_a_confirmation_or_a_pdk_fetch`(`test_api_phase1.py`)、`test_fetch_with_the_fake_ciel_installs_and_verifies_against_the_lock`、`test_two_processes_fetching_one_pdk_commit_run_one_after_the_other`(`test_pdk_ops.py`)、`test_ihp_is_fetched_as_its_family`(`test_ciel.py`) |
| PDK の再走査 | 済み | `Server.rescan_pdks`、`POST /api/pdks/rescan`。HTTP からの取得の後にも動く。ルート、またはファイルから走査が得たもの(レイヤーの属性、xschem と KLayout のディレクトリ、出どころ)が違えば id は変わったとみなすので、展開の途中で最初に走査した PDK も置き換わる。新しい id と変わった id を先に入れ、消えた id を後で抜くので、読む側が空の dict を見ることはない。読む側は写しを回る(`pick_pdk`)。PDK の解決が変わったプロジェクトの開いているレイアウトとエディタは捨てる。テストは `test_api_phase1.py`: `test_rescan_finds_a_pdk_added_after_start_and_an_open_project_resolves_it`、`test_rescan_racing_readers_never_shows_an_empty_dict_or_loses_an_unchanged_id`、`test_a_pdk_first_scanned_half_extracted_is_replaced_once_its_files_are_there` |
| format 2 の lock での `set-pdk` | 済み | `Project.set_pdk` は新しい PDK の固定(`meta_lock` の前にハッシュを取る)を書き、形式とツールは残す。同じ PDK なら固定はそのままなので、変更された木は報告され続ける。テスト: `test_set_pdk_on_a_format_2_lock_writes_the_new_pin_and_on_format_1_the_id`(`test_lock_format2.py`)、`test_set_pdk_on_a_format_2_lock_hashes_without_the_server_lock`(`test_api_phase1.py`) |
| `PYTHONDONTWRITEBYTECODE=1` で動く PCell のワーカー | 済み | `pcell._run` と `pcell_worker._load`。テストは `test_pcell.py`: `test_the_worker_is_run_without_writing_bytecode`、`test_a_worker_run_leaves_no_pycache_in_the_pcell_package` |
| ストアに作る OSDI | 済み | プロファイルの `osdi` の項目はモデルの名前(`psp103`)。`Pdk.osdi_dir` は `store/build/<commit>/<variant>/osdi/`(バージョンのない PDK は `local-<ルートのハッシュ>`)で、`Pdk.osdi_files` はそこ、次に PDK の `libs.tech/ngspice/osdi/`、次に段階 0 がコンパイルしたキャッシュを探す。`scripts/osdi.sh` はストアにコンパイルし、古いキャッシュにあるものを移す。`doctor` は古いキャッシュに残るモデルを注記する。テストは `test_osdi_store.py`: `test_profile_lists_model_names`、`test_an_ihp_simulation_loads_osdi_from_the_store`、`test_osdi_sh_moves_the_old_cache_into_the_store`、`test_store_gc_and_verify_leave_build_alone`。`test_osdi_row_hints_at_moving_the_old_cache`(`test_doctor.py`) |
| ツールのバージョン番号と出どころ | 済み | `tools.py`: `VERSION_RULES` と `parse_version`([lockfile](#lockfile) の表)、`locate`(パスと出どころ)、`tool_record`、`tools_record`(`undine_version` と `pixi_lock_sha` つき)。`deps.status` は番号や出どころの違いを警告し、Undine の別の commit や別の `pixi.lock` を注記する。`doctor` はツールごとの番号と出どころ、Undine のバージョン、`pixi.lock` のハッシュを示す。テストは `test_tools.py`: `test_each_tools_banner_gives_its_number`、`test_an_unmatched_banner_is_kept_as_text_and_a_silent_tool_as_a_question_mark`、`test_locate_labels_where_each_tool_came_from`、`test_undine_version_without_git_and_in_a_checkout`。`test_status_on_format_2_warns_of_another_tool_and_notes_another_commit`(`test_lock_format2.py`)。`test_tool_rows_show_number_and_origin`(`test_doctor.py`) |
| `store verify`、`store gc` | 済み | `__main__.store_command`: `verify` はどのオブジェクトと記録もハッシュし直し、合わないものを消す(`--dry-run` は報告だけ)。`gc` は `--root` の下のプロジェクト、その changeset の worktree、ストアの一覧の lock と install を、ストアの排他ロックのもとで読み、読めない lock があれば何も消さない。テストは `test_cli_phase1.py`: `test_store_verify_finds_an_edited_object_and_removes_it`、`test_store_gc_keeps_what_projects_here_lock_or_install_and_removes_the_rest`、`test_store_gc_with_an_unreadable_lock_removes_nothing` |
| changeset の worktree が依存をハードリンクする | 済み | `changeset._dep_copy`(`cas.link_or_copy` を通る)。テストは `test_changeset_deps_link.py`: `test_worktrees_link_the_dependency`、`test_copies_where_linking_is_refused` |

段階 1 を作る間に決めたこと(ユーザーと):

- format 2 を書くのは `pdk verify --pin` と `lib lock --format 2` で、1 つの関数を通ります。`pkg add` と `pkg migrate` は段階 3 です
  ([lockfile](#lockfile))。
- HTTP からの PDK の取得は、いまは PDK だけの狭いトークンで確かめます。段階 4 で一般化できる形にしてあります。
- 段階 0 が `~/.cache/undine/osdi/<id>/` にコンパイルした OSDI モデルは、`pixi run osdi` がストアに移すまでそこでも見つけます。
  `doctor` がそう伝えます。
- *未検証*(初回を信頼)の PDK は問題ではなく注記です。`status` は ok のままで、バッジも赤くなりません。
- 段階 1 より前に入れた依存は、最初の `sync` がストアに取り込むので、取得なしにオフラインで sync できます。

上の本文との違いと、加えたもの:

- `GET /api/p/{p}/pdk/fetch-plan` を加えました。取得のダイアログが、人が確かめる前にファミリー、variant、commit、ライブラリ、
  取得元、ルートを示すためです。`POST /api/confirmations` も計画を返します。
- すでに format 2 の lock での `lib lock --format 2` は、PDK を固定し直しません(変更された PDK を黙って受け入れてしまうため)。
  `pdk verify --pin` を使うよう伝えます。
- `status` は PDK を読みません。取得の後の PDK は、最後の書き込みから 2 秒より後に `pdk verify` がハッシュを取るまで *未確認* です。
- CLI の `pdk fetch` は動いているサーバーの PDK を探し直せません。**PDK を再検出** か `POST /api/pdks/rescan` を使うよう伝えます。
- `store gc` は 1 日より古い `tmp/` の中身も消し、ストアのプロジェクトの一覧からはなくなったプロジェクトを外します。

後の段階に残したもの: `pkg` のコマンド(固定どおりのオフラインの sync は、段階 1 では `lib sync --offline`)、manifest、推移的な
解決と `include` のファイルの集まり(段階 2。段階 1 の木はサブディレクトリのすべてのファイルを含む)、index の clone、`pkg migrate`、
`--dep-host` と `git fetch` の間の大きさの監視(段階 3。段階 1 は blob を読む前に `ls-tree` から大きさとファイル数を確かめる)、
依存の取得のジョブ、一般の確認のトークン、format 2 のマージの規則(段階 4)、`lib lock --format 2` と `pdk verify --pin` の HTTP の
ルートやボタン(コマンドラインだけ)、期待する PDK のハッシュと `verified_by`(段階 6)、`store gc` による `git/` の整理、PDK の取得の
ジョブを再起動の後まで残すこと。

<a id="phase-2-status"></a>
### 段階 2 の状況

段階 2 の各項目がコードのどこにあるかを、モジュールと関数で示します(`server/undine/`、UI は `web/src/`)。テストは `server/tests/`
(web のものは `web/src/`)にあります。段階 1 と同じく、どのテストも一時的な `UNDINE_HOME` と `UNDINE_STORE`、ローカルの git
リポジトリで動くので、`~/.undine`、`~/undine-projects`、ネットワークには触れません。

| 項目 | 状況 | 場所 |
|---|---|---|
| `undine-package.toml` | 済み | `store/manifest.py`: `parse`(最初の問題を `<where>: undine-package.toml: <key path>: <what>: <fix>` として拒否する。知らないキーは知っているキーを示して拒否し、`[checks]` と `[renames]` は段階 5 のために表として残す。`tomllib`、Python 3.10 では `tomli`)、`select` と `check_files`(`[files]` の include の組: `*.sch` は最上位のファイルだけ、`rtl/**` はどの深さにも合い、manifest は常に入る。セルのない include の組や、公開するセルが入らない組は拒否する)、`from_files`、`from_tree`、`from_archive`、`from_dir`(ストアのオブジェクトは解析する前にハッシュし直す。シンボリックリンクの manifest は拒否する)。`store/semver.py`: `Version`(厳密な SemVer 2.0.0。build のメタデータは優先順位で無視する)と `Req`(素の形、`^`、`~`、`=`、`*`。比較演算子とカンマの並びは段階 3 の案内とともに拒否する)。`gitsrc.blob_tree(files="include")` は含める blob だけを読み、`gitsrc.read_file` はある commit の 1 つのファイルを読む。テスト: `test_manifest.py`(`test_the_design_example_parses`、`test_git_and_sibling_requirements`、`test_each_refusal_names_its_key_path_and_what_to_do`、`test_select_matches_whole_paths_segment_by_segment`、`test_check_files_wants_a_top_level_cell_and_every_export`、`test_from_files_refuses_a_modified_store_object`、`test_parse_falls_back_to_tomli_on_python_3_10` など)、`test_semver.py`(`test_precedence_follows_semver_section_11`、`test_requirement_meanings`、`test_ranges_are_refused_with_the_phase_3_hint` など)、`test_gitsrc_include.py`(`test_the_include_tree_is_the_selected_files_plus_the_manifest_and_nothing_else_is_read`、`test_without_files_or_without_a_manifest_include_gives_the_all_tree`、`test_a_link_or_submodule_is_refused_only_inside_the_include_set`、`test_an_oversized_or_irregular_manifest_is_refused_before_any_other_blob_is_read`、`test_read_file_reads_one_file_without_the_tree` など) |
| manifest からの推移的な依存(git の URL) | 済み | `store/resolve.py`: `resolve`(たどること。パッケージ名ごとに 1 つのノード。問題はそれぞれコードと経路を持つ `Problem` として集め、`ResolveError` は 1 回)、`same_package`(木が等しいか、files のモードが違うときの規則)、`chain_text`、`lock_graph`、`reachable`、`incomplete`。`store/locked.py`: `files_of` と `manifest_of`。固定したエントリのファイルと manifest を、ストアか確かめた install から読み、なければ `NotAvailable`(「run lib sync first」)。`store/deps.py`: `add`、`update`(名前を指定した要求の下だけ)、`remove`(lock のグラフだけから。取得しない。ほかのパッケージがまだ置くものは残す)、`relock`(`lib lock --format 2` のための `upgrade=`)。どれも `_Graph` を通る。パッケージが置いた名前には `NotDirect`、format 1 の lock では `NeedsFormat2`。テスト: `test_resolve.py`(`test_conflicting_requirements_name_both_chains`、`test_a_phase_1_entry_and_an_include_requirement_at_one_commit_agree_and_the_locked_one_stands`、`test_a_sibling_is_read_at_the_requirer_s_commit_with_a_root_relative_subdir`、`test_every_problem_of_a_requirement_is_collected_in_one_error`、`test_a_cycle_is_walked_once` など)、`test_locked.py`、`test_deps_phase2.py`(`test_update_moves_a_package_and_its_dependencies_and_drops_what_it_no_longer_needs`、`test_update_of_a_placed_name_is_refused_and_remove_keeps_a_name_another_package_places`、`test_a_fresh_clone_must_sync_before_add`、`test_format_1_refuses_dependencies_and_another_name_and_adds_a_plain_package_as_a_sha_entry`、`test_relock_upgrade_writes_format_2_once_and_a_refusal_leaves_format_1`、`test_plain_dependencies_keep_phase_1_entries_and_a_phase_1_entry_is_not_converted`、`test_a_remote_manifest_may_not_name_a_local_repository`、`test_over_http_dep_root_applies_to_the_urls_a_manifest_names` など) |
| プロジェクト全体で共通のローカル名と、それぞれの下への install | 済み | `resolve.resolve` は各ノードに名前(要求元のキーとパッケージ自身の名前)を与え、2 つのパッケージに当てた名前や予約された名前を(大文字小文字を区別せずに)拒否する。`deps._reserved` と `deps._shadowed`(プロジェクトと PDK のライブラリ、`devices`、Xschem のライブラリのフォルダー、`symbol_paths` のフォルダー)。`deps._apply` と `deps.sync` は 1 つの木をすべての名前の下に、ストアからのハードリンクで、それぞれの目印(`name`、`direct`、`package`、`version`、`via`)とともに入れる。`store/project.py`: 推移的な名前は、`package`、`direct`、`via` を持つ読み取り専用の依存の `Library` で、目印から知る(inode、mtime、大きさでキャッシュする。lock は読まない)。`create_library` は install した名前を拒否する。テスト: `test_an_alias_installs_one_package_under_both_names`、`test_one_local_name_for_two_packages_is_refused_naming_both_requirers`、`test_two_local_names_differing_only_in_case_are_one_name`、`test_a_reserved_local_name_is_refused_naming_the_chain`(`test_resolve.py`)。`test_an_alias_installs_the_package_under_both_names_sharing_inodes`、`test_one_local_name_for_two_packages_is_refused_naming_both`(`test_deps_phase2.py`)。`test_a_transitive_install_is_a_read_only_dependency_library_and_the_lock_is_never_read`、`test_markers_and_manifests_are_read_once_until_they_change`、`test_create_library_refuses_an_installed_local_name`(`test_project_phase2.py`)、`test_a_new_local_name_may_not_be_a_folder_of_xschems_library_or_of_symbol_paths`、`test_a_name_project_json_already_gives_keeps_working_and_status_notes_what_it_shadows`(`test_reserved_names_phase2.py`) |
| セル名が重ならないこと | 済み | `resolve.cell_names`(最上位の論理のビューと、レイアウトだけの `.oas` のセル)と `resolve.check_disjoint`。`deps._project_cells` と `deps._pdk_cells` から入力を得る。拒否するのは片方が manifest のあるパッケージのときだけで、残りは `status` が報告する(`Project.dependency_cell_names`)。テスト: `test_check_disjoint`、`test_cell_names_are_top_level_logic_and_layout_only_cells`(`test_resolve.py`)。`test_cell_names_clash_between_packages_but_not_with_one_package_under_two_names_or_a_layout_copy`(`test_deps_phase2.py`)。`test_a_project_cell_named_like_a_packages_cell_is_an_issue_a_layout_copy_is_not`(`test_store_sync_phase2.py`) |
| PDK の互換性 | 済み | `pdks/compat.py`: `project_target`(lock の固定、または接頭辞としての format 1 の id)、`check`(`family_of` による family、variant、commit 全体、`built_with`。バージョンのない PDK や PDK がないときは、すべきことを添えて拒否する)、`note`(描いた commit と使う commit が違う)、`tag_commits`(段階 6 のための継ぎ目。タグを知らない)、`locked_incompatible` と `IncompatiblePdk`。`Project.set_pdk` はハッシュの前と、`editing()` の中でもう一度、これを出す。`pdkops.upgrade` と `verify --pin` は、固定済みのパッケージが受け付けない固定を `incompatible` として報告する。テスト: `test_pdk_compat.py`(`test_family_variant_and_commit_accepted_and_each_refusal`、`test_a_format_1_prefix_matches_a_full_commit`、`test_compat_only_is_refused_with_the_phase_6_hint`、`test_a_pdk_without_a_version_and_no_pdk_are_refused`、`test_ihp_family_accepts_its_variant` など)、`test_pdk_check_names_a_transitive_package_and_its_chain`(`test_resolve.py`)、`test_set_pdk_is_refused_listing_every_locked_package_that_does_not_accept_it`、`test_plain_git_dependencies_never_block_set_pdk`、`test_upgrade_reports_locked_packages_the_new_pin_does_not_accept`(`test_project_phase2.py`) |
| コードブロックと RTL についての信頼の規則 | 済み | `store/trust.py`: スキャナ(`spice_findings`、`rtl_findings`、`tcl_findings`、`scan_dir`)、記録(`key_of`、changeset の worktree では本当のプロジェクトのキーだけを読む `trusted_keys`、`untrusted_dirs`、`set_trusted`、`drop`)、ゲート(`check_netlist`、`check_tcl`、`check_rtl`。`UntrustedContent` を出す)。`schematic.spice` は各行の `Source`(セルの本体とシンボル)を記録し、`owner` がそれを名付ける。ゲートが動くのは `Server.start_sim`(規則 `sim`)、KLayout と netgen の LVS の `Run.launch`(`Server._paths_gate`、規則 `paths`)、`backend=xschem` のネットリストのルート(Tcl だけ)、`rtl.lint` と `rtl.submit_sim`、`librelane.submit`(トップのライブラリが依存であるフロー。`run_librelane` は拒否を返し、実行のルートは 403 `untrusted` を返し、sign-off のスイートは LVS の拒否をチェックごとにコードと指摘つきで返す)。テスト: `test_trust_scan.py`(`test_control_blocks_and_control_lines_with_their_lines`、`test_paths_after_substitution`、`test_an_include_into_a_dependency_install_is_refused`、`test_rtl_findings`、`test_tcl_findings_in_a_symbol`、`test_scan_dir_lists_cells_rtl_and_tcl` など)、`test_netlist_sources.py`(`test_code_blocks_are_attributed_to_their_cell_body_with_the_exact_text`、`test_a_trusted_symbol_placed_in_a_dependency_cell_is_attributed_to_that_cell`、`test_owner_names_packages_plain_dependencies_and_directories` など)、`test_trust_run.py`(下) |
| lock のグラフにもとづく固定どおりの sync、残りもの、status | 済み | `deps.sync` は lock のグラフのすべてのエントリを各名前の下に、できればストアから入れ(`_plan`、`_frozen`、`_fetch_missing`)、名前や要求元が変わったパッケージの目印を書き直し、`project.json` のどの依存からも届かなくなったものを消す。素の依存の行は `{name, action, commit}` のまま。`deps.status`(`_Installed`)はローカル名ごとに 1 行を、`package`、`version`、`names`、`direct`、`via`、`chain`、`trusted`、`flagged`、`internal_cells`、`pdk` とともに返す。テストは `test_store_sync_phase2.py`: `test_a_frozen_sync_installs_every_package_and_name_from_the_store_with_no_fetch`、`test_offline_with_an_empty_store_names_each_missing_package_once_and_changes_nothing`、`test_plain_dependency_rows_stay_name_action_commit`、`test_a_phase_1_entry_whose_manifest_has_files_still_syncs_every_file_from_git`、`test_removing_a_dependency_from_project_json_removes_what_only_it_reached`、`test_sync_refuses_an_incomplete_graph_or_a_name_claimed_twice_before_anything_is_read`、`test_status_rows_carry_the_package_fields_and_one_row_per_local_name`、`test_status_reports_an_incompatible_package_and_notes_a_built_with_difference`、`test_a_broken_installed_manifest_is_an_issue_row_not_a_crash`、`test_a_plain_dependency_with_control_gets_the_trust_note` |
| `[exports]` | 済み | `Library.internal` と `Library.cells` の `internal` の印。`Server.symbols` とエージェントの `list_symbols` は内部用のセルを外す。内部用のセルも解決でき、ネットリストに入り、名前を指定すれば置ける。テスト: `test_a_dependency_row_shows_its_package_and_cells_outside_exports_are_internal`(`test_project_phase2.py`)、`test_symbols_leave_out_internal_cells_and_the_cell_list_marks_them`(`test_api_phase2.py`) |
| CLI | 済み | `__main__.py`: `lib add --package`、`lib add` / `update` / `remove` が示す推移的な(`+`)、動いた、外れたパッケージの行、`lib status` の `via` の経路と別名の行、信頼されていない内容の `~` の行、`lib trust PROJECT NAME [--untrust] [--yes]`(指摘を示して尋ねる。端末がなく `--yes` もなければ拒否する)、変換と解決を 1 回の書き込みで行う `lib lock --format 2`、すべてのパッケージを示して拒否する `set-pdk`。テストは `test_cli_phase2.py`: `test_lib_add_prints_the_packages_it_brings_and_what_is_not_trusted`、`test_lib_status_shows_via_and_alias_rows_and_remove_names_what_stays_placed`、`test_lib_update_prints_the_packages_it_brings_moves_and_drops`、`test_lib_trust_lists_asks_and_writes_trusted`、`test_lib_lock_format_2_converts_and_resolves_in_one_write_or_leaves_format_1`、`test_set_pdk_to_a_pdk_locked_packages_refuse_lists_each_and_changes_nothing`、`test_a_pdk_pinned_by_lib_lock_format_2_that_a_package_refuses_is_a_warning`、`test_lib_sync_shows_a_package_the_locked_pdk_does_not_fit` |
| HTTP | 済み | `server/api.py`: `/deps` のルートは新しい欄(`resolved`、`flagged`、`packages_changed`、`removed_packages`、`kept`)を返す。拒否はコードを持ち、`detail` は CLI と同じ文: 409 `resolution_refused`(`problems`)、409 `needs_lock_format_2`、400 `not_direct`(`placed_by`)、409 `pdk_incompatible`(`packages`)、409 `not_synced`、403 `untrusted`(`findings`)。`GET /api/p/{p}/trust`、確認の種類 `trust`、`POST /api/p/{p}/trust`。テストは `test_api_phase2.py`: `test_post_deps_reports_the_transitive_packages_and_a_conflict_is_409_with_both_chains`、`test_update_and_remove_of_a_placed_name_are_400_not_direct_naming_the_placer`、`test_the_new_refusals_have_their_codes_and_keep_the_message_as_detail`、`test_trust_needs_a_fresh_trust_token_bound_to_the_key_and_the_installed_tree`、`test_a_pdk_tokens_messages_are_phase_1s`、`test_post_pdk_to_a_pdk_a_locked_package_does_not_accept_is_409_with_the_packages`。`test_http_sim_refusal_is_403_untrusted_with_findings`(`test_trust_run.py`) |
| エージェント | 済み | `agents/agent_tools.py`: `run_simulation`、`check_specs`、`sweep`、`lint_rtl`、`run_rtl_sim` は信頼による拒否をそのまま結果として返す。どのツール、`undine call` のコマンド、クライアントのメソッドも、信頼のルートや確認には届かない。`agents/agent.py` のプロンプトと skill の `verification.md` は、信頼するのは人の行いだと伝える。テストは `test_agent_phase2.py`: `test_no_tool_client_method_or_call_reaches_trust_or_a_confirmation`、`test_the_prompt_says_trusting_a_dependency_is_a_persons_action`、`test_run_simulation_on_an_untrusted_dependency_returns_the_refusal_unchanged`、`test_check_specs_and_sweep_pass_the_refusal_on`、`test_run_rtl_sim_and_lint_rtl_on_an_untrusted_dependency_return_the_refusal` |
| Web の UI | 済み | `App.tsx`: パッケージ、バージョン、`via`、別名の印を持つ依存の行。更新 / 削除は直接の行だけ。lock のダイアログのパッケージと信頼の列。拒否された追加はすべての問題を、拒否された PDK の変更はパッケージを一覧にする。`TrustDialog.tsx`(ファイルごとの指摘、トークン、信頼の順。信頼の取り消し)。`UntrustedNotice.tsx` と、シミュレーション、LVS、意図、ネットリスト、RTL のビューのための `panels/SimPanel.tsx` の `Refused`。テスト: `App.phase2.test.tsx`、`TrustDialog.test.tsx`、`panels/UntrustedPanels.test.tsx` |
| テスト: ダイヤモンドを一度だけ入れる | 済み | `test_a_diamond_reads_the_shared_package_once_and_makes_one_node`(`test_resolve.py`)、`test_diamond_installs_c_once_hard_linked_and_fetches_it_once`(`test_deps_phase2.py`)、`test_packages_of_one_repository_and_commit_are_fetched_once`(`test_store_sync_phase2.py`) |
| テスト: 両立しない要求が両方の経路を示す | 済み | `test_conflicting_requirements_name_both_chains`(`test_resolve.py`)、`test_conflicting_requirements_name_both_chains_and_nothing_is_written`、`test_a_transitive_name_becomes_direct_for_the_same_package_and_conflicts_for_another_version`(`test_deps_phase2.py`)、`test_post_deps_reports_the_transitive_packages_and_a_conflict_is_409_with_both_chains`(`test_api_phase2.py`) |
| テスト: `comparator` を持つ 2 つのパッケージを、両方を示して拒否する | 済み | `test_check_disjoint`(`test_resolve.py`)、`test_cell_names_clash_between_packages_but_not_with_one_package_under_two_names_or_a_layout_copy`(`test_deps_phase2.py`): `comparator: analog_ip 1.2.0 and sar_common 1.3.1`。何も書かない |
| テスト: ローカルな別名で解決でき、Xschem のネットリスタのバックエンドでも解決できる | 済み | `test_a_local_alias_resolves_with_undines_netlister`、`test_a_local_alias_resolves_with_the_xschem_netlister`(`test_alias_netlist.py`)、`test_an_alias_installs_the_package_under_both_names_sharing_inodes`(`test_deps_phase2.py`) |
| テスト: 2 つのライブラリが 1 つのローカル名に別々のパッケージを当てれば拒否する | 済み | `test_one_local_name_for_two_packages_is_refused_naming_both_requirers`、`test_two_local_names_differing_only_in_case_are_one_name`(`test_resolve.py`)、`test_one_local_name_for_two_packages_is_refused_naming_both`(`test_deps_phase2.py`) |
| テスト: 互換でない PDK を拒否する | 済み | `test_an_incompatible_pdk_is_refused_directly_and_through_a_chain`(`test_deps_phase2.py`)、`test_set_pdk_is_refused_listing_every_locked_package_that_does_not_accept_it`(`test_project_phase2.py`)、`test_set_pdk_to_a_pdk_locked_packages_refuse_lists_each_and_changes_nothing`(`test_cli_phase2.py`)、`test_post_pdk_to_a_pdk_a_locked_package_does_not_accept_is_409_with_the_packages`(`test_api_phase2.py`)、ダイアログは `App.phase2.test.tsx` |
| テスト: `.control` か絶対パスの `.include` を持つ依存のセルを、信頼するまで拒否する | 済み | `test_trust_run.py`: `test_a_dependency_cell_with_control_or_an_absolute_include_is_refused_until_trusted`、`test_a_project_symbols_value_set_in_a_dependency_cell_is_refused`、`test_a_dependency_symbols_template_default_is_refused_in_its_own_cell_and_in_a_project_cell`、`test_an_absolute_include_of_a_file_in_the_dependencys_own_install_is_refused`、`test_star_hash_lines_and_control_in_a_dependency_subcircuit_are_refused`、`test_includes_under_the_pdk_root_and_the_generated_corner_line_pass`、`test_a_project_cells_own_control_runs_and_display_netlists_only_warn`、`test_lvs_is_refused_only_at_launch_and_only_for_paths`、`test_the_xschem_route_refuses_untrusted_tcl_whatever_the_prepass_makes_of_it`、`test_rtl_of_an_untrusted_dependency_is_refused_by_simulation_and_lint_until_trusted`、`test_a_changeset_worktree_sees_trust_given_in_the_project_after_it_opened`。`test_trust_goes_with_an_update_to_another_commit_and_start_sim_refuses_again`(`test_deps_phase2.py`)。パネルでの拒否は `panels/UntrustedPanels.test.tsx` |

段階 2 を作る間に決めたこと(段階 2 の PR で提案。レビューで変えられます):

- **index より前の git の要求。** manifest は依存を `{ git, rev, subdir?, package?, version? }`(git と rev は必須。公開したデータが
  動く HEAD を指さないように。`package` の既定はキー)か、`{ subdir }` だけ(同じリポジトリと commit の sibling。subdir はリポジトリの
  ルートからの相対)で書きます。index の形は解析しますが、解決のときに git の形を示して拒否します。
- **semver。** `Version` と、`^`、`~`、`=`、素の形、`*`。比較演算子とカンマの並びは、index が選ぶべきバージョンを与える段階 3 まで
  待ちます。
- **lock のどこにパッケージを置くか。** 直接の依存は `libraries` に残し、manifest を通してだけ届くパッケージは `index` のキーなしで
  `packages` に置きます。1 つのパッケージ名は 2 つの表を合わせて一度だけ現れます。
- **git のパッケージのバージョンとは:**(パッケージ名、木)。識別は、段階 1 のエントリでも木の manifest から取り、`git:<local>` は
  manifest のない木にだけ使います。`files` のモードが違えば(すべてのファイルの段階 1 のエントリと、段階 2 の include の組)、
  (git、commit、subdir)が等しいかで決め、固定したエントリをそのまま残します。
- **木のファイルの組**は明示的な `files: "include"` で示し、書くのは `add`、`update`、`lib lock` だけで、`sync` は書きません。
  `[files]` のない manifest は段階 1 の木の id になります。
- **固定したパッケージ**は、ストアから、なければ確かめた install から読み、どちらもなければ「run lib sync first」で拒否します
  (`store/locked.py`。HTTP では 409 `not_synced`)。`add`、`update`、`lib lock`、`set-pdk` で同じです。どれも、解決し直さないものは
  取得しません。
- **段階 2 の前に固定したエントリ**(format 1、または段階 1 の format 2 のエントリ。`package` を記録していない)は、ほかの依存に
  ついてのコマンドでは、その素のエントリのまま保ちます。識別は manifest から取りますが、その依存、自身の名前、`[pdk]` は取得も
  確認も書き込みもせず、エントリと install はバイト単位でそのままです。ただし、コマンドが今解決するパッケージがそれを要るときは
  `package`、`version`、`names` を得ます(解決されるまで `deps` はなし)。解決するのは `lib update NAME` か `lib lock`(format 2、
  または `--format 2`)だけです。`lib status` は、自身の名前の下に入っていないものと、format 1 の lock で依存を固定していない
  ものを注記します。format 2 の lock では、固定していない依存は問題のままです。
- **1 つのパッケージに `project.json` の名前は 1 つ。** パッケージの項目つきで固定したパッケージに 2 つ目の直接の名前を付けるのは
  拒否します(`y: a 1.0.0 is a dependency of this project already, as x`)。1 つのパッケージの 2 つ目のエントリになるからです。
  大文字小文字だけが違う既存のライブラリの名前も拒否し(`library IP exists (as ip)`)、1 つのパッケージを 2 通りの綴りで置くことも
  拒否します。大文字小文字を区別しないファイルシステムでは 1 つのディレクトリだからです。
- **自身の名前。** manifest のあるパッケージは、自身の `[package]` の名前の下にも入れます。自身の回路図が自身のセルを
  `<name>/<cell>.sym` として置くからです。
- **予約されたローカル名**: プロジェクトと PDK のライブラリ、`devices`、Xschem のライブラリと `project.json` の `symbol_paths` の
  サブディレクトリすべて。大文字小文字を区別せずに比べます。`project.json` にすでにある名前は注記だけにします。
- **素の git 依存を書けるのは `project.json` だけ**: manifest の依存先には自身の manifest が要ります(識別、バージョン、`[pdk]` は
  そこから来る)。
- **format 1。** format 1 の lock でも manifest は読んで確かめます。`[dependencies]` や、キーと違う名前は `lib lock --format 2` の
  案内とともに拒否します。`lib lock` は段階 1 の順(relock、次に upgrade)を保ち、relock が format 2 を要るときだけ、メモリの中で
  変換して解決し、1 回で書きます。
- **組み込みの互換性タグはなし。** `compat.tag_commits` は段階 6 のための継ぎ目です。確かめるのは family、variant、commit 全体、
  `built_with` です。`[pdk]` のないパッケージは注記し、確かめません。
- **PDK がない、バージョンのない PDK、format 1 の id**: 理由とすべきことを添えて拒否します。12 桁の 16 進の id は接頭辞として
  照らします。何も取得しません。
- **動いた固定**(`pdk verify --pin`、`lib lock --format 2`)が、固定済みのパッケージの受け付けない PDK になれば、`!` の警告と
  status の問題として報告し、拒否はしません。固定は入っているものを記録するからです。
- **セルの重なり**を拒否するのは、片方が manifest のあるパッケージのときだけです。プロジェクトのライブラリは論理のビューだけを出し、
  パッケージのレイアウトだけのセルは自身の閉包と PDK に対しては問いません。残りは status の注記です。
- **信頼。** キーは `name@version`、manifest がなければ `local@commit12` で、commit か木が変わればいつでも外れます。規則は
  [セキュリティ](#security)のとおりで、許す絶対パスのルートは `pdk.root` だけ、1 行はセルの本体とシンボルの両方に結び付けます。
- **ゲートの場所**: シミュレーションは `start_sim`(実行を再利用する前)、LVS は `Run.launch`(パスだけ)、Xschem のバックエンドは
  そのルート(Tcl だけ。前処理の結果にかかわらない)、Verilator は `rtl.py`、LibreLane は `librelane.submit`(トップのライブラリに
  Verilator の規則すべて)。git の依存を 1 つも宣言しないプロジェクトは調べないので、入れるもののないリンクの `.undine` は何も
  拒否しません。
- **changeset の worktree** は本当のプロジェクトだけから信頼を読みます。自身の `project.json` は開いたときの写しなので、
  プロジェクトで取り消したキーは worktree でも取り消されます。
- **人が信頼する**のは `lib trust`、1 回だけ使える `trust` のトークンつきの HTTP のルート、ダイアログを通してです。エージェント、
  クライアント、`undine call` の経路はどれにも届きません。
- **依存のセルと同じ名前のプロジェクトのセル**は、作るときには拒否しません。`status` が報告し(パッケージとなら問題、素の依存と
  なら注記)、段階 0 のネットリストのときの確認が最後の守りのままです。
- **取得した manifest の中のローカルの URL** は、要求元がローカルでなければ拒否します。推移的な URL はどれも `_check_spec` を、
  HTTP では `_source` も通ります。
- **`[exports]`**: 内部用のセルはピッカーと `list_symbols` から隠し、内部用と印を付けます。それでも解決でき、ネットリストに入り、
  置くこともできます(それに印を付けるのは段階 5 の `pkg check`)。
- **`add` と `update` は、名前を指定した要求の下だけを解決し直します**。`main` のような git の rev は動くので、ほかの固定済みの
  パッケージはそのまま保ちます。
- **Python は 3.10 以上のまま**で、`tomli` に代えて読みます。
- **段階 0 から 2 は一緒にリリースします。** 段階 1 を先に出すなら、その `read_lock` は、`packages` を持つ format 2 の lock や、
  `files`、`package`、`version`、`names`、`deps` を持つ `libraries` のエントリを拒否しなければなりません。そうしないと段階 1 の
  Undine は、自身の名前や別名のディレクトリを残りものとみなして消してしまいます。
- **コマンド。** `lib` を広げ、加えるのは `lib trust` だけです。経路は `lib status` に出ます。`pkg` のコマンドは段階 3 からです。
- **manifest の知らないキー**は、そのキーと知っているキーを示して拒否します。`revision =` のような書き間違いが黙って HEAD を
  意味しないようにするためです。

上の本文との違いと、加えたもの:

- `[dependencies]` は、[パッケージ](#packages)にない git の形 `{ git, rev, subdir?, package?, version? }` と sibling の形 `{ subdir }`
  を取ります。index の形は段階 3 まで拒否します。
- lock のエントリには `files` のキーがあります。木が manifest の include の組を含むときは `"include"`、サブディレクトリのすべての
  ファイルを含むときはキーがありません([lockfile](#lockfile) の注記)。
- manifest のある直接の依存は、`package`、`version`、`names`、`deps` を持って `libraries` に残り、`packages` には manifest が届く
  ものだけを置きます。[lockfile](#lockfile) の例は、パッケージをすべて `packages` に置いています。
- すべてのファイルのエントリ(段階 1)と、同じ(git、commit、subdir)の include の組への要求は 1 つのパッケージで、固定した
  エントリが残ります。識別は木の manifest から取り、`git:<local>` は manifest がないときだけです。
- 互換性タグは組み込んでいません。`compat` のタグだけを書くパッケージは、プロファイルのパッケージがタグを定義する(段階 6)まで、
  commit を `commits` に書くよう案内して拒否します。
- パッケージは、要求元が与える名前のほか、自身の名前(自身の名前の install)の下にも入れます。
- ローカル名には、プロジェクトと PDK のライブラリ名のほか、`devices` と、Xschem のライブラリや `symbol_paths` の項目の
  サブディレクトリの名前も使えません([解決](#resolution)の規則 5 が挙げるのは前の 2 つだけ)。
- 信頼: manifest のない依存のキーは `<local>@<commit12>`。HTTP には確認の種類 `trust` があります。changeset の worktree は本当の
  プロジェクトだけから信頼を読みます。[セキュリティ](#security)はエージェントが信頼を提案できるとしていますが、段階 2 ではできません
  (提案は段階 4)。
- Xschem のネットリスタのバックエンドは Tcl だけ(`tcleval(` と、`[` か `$` を持つ `expr(`)でゲートします。そのネットリストは
  表示のためのもので、Xschem はほかのものを評価しないからです。
- LVS と netgen は、実行が始まるときに、パスの規則だけでゲートします(`.control` の規則はない。それを実行しないから)。
- 信頼の規則は、[セキュリティ](#security)が挙げるより多くを拒否します: `*#` の行、XSPICE のファイルのパラメータ、`$` や `~` が
  残るパス、依存自身の install への絶対パス、RTL の `$system`、DPI、ファイルのタスク、`` `include `` のパス、`` `define `` の本体。
- 比較演算子のある semver の範囲(`>=`、`<`、カンマの並び)は段階 3 に回し、案内とともに拒否します。
- `tomllib` には代わりがあります。Python 3.10 では `tomli` で manifest を読み(`server/pyproject.toml` に `tomli>=1.1;
  python_version < '3.11'`)、文書にある pixi なしのインストールもそのまま動きます。
- 素の git 依存を持つプロジェクトについての、3 つの動作の変更。それぞれに status の注記と、[トラブルシューティング](../troubleshooting.ja.md)の
  項目があります: 信頼のゲートはすべての依存にかかる。`status` は、依存のものと同じ名前のプロジェクトのライブラリの論理のセルを
  報告する。新しいローカル名は、Xschem のライブラリや `symbol_paths` の項目のサブディレクトリの名前にできない。段階 1 で動いて
  いたほかのものは拒否せず、format 1 の lock も段階 1 の format 2 の lock もそのまま動きます。

後の段階に残したもの: パッケージの index、index の clone、比較演算子のあるバージョン範囲、バックトラック、yank、lock のエントリの
`index`、`pkg` のコマンド(`search`、`info`、`add`、`update`、`lock`、`outdated`、`tree`、`why`)、`pkg migrate`、`--dep-host` と取得の
間の大きさの監視(段階 3)。changeset での依存の操作とそのレビュー(`trust.scan_dir` を使う)、エージェントによる信頼や依存の提案、
一般の確認のトークン、依存の取得のジョブ、`undine.lock` のマージの規則(段階 4)。`pkg init / check / pack / publish`、`[checks]` と
`[renames]` の意味、SPDX の解析、公開のときの git 依存の拒否、パッケージが宣言したローカル名だけを置いているかの確認、レイアウトの
コピーの更新(段階 5)。プロファイルのデータとしての互換性タグ、`set-pdk` の拒否で示すモデルのファイルが違う commit、期待する PDK の
木のハッシュ(段階 6)。まだどの段階にも入れていないもの: 1 つのパッケージの複数のメジャーバージョンと、ライブラリごとのローカル名の
表([決定事項](#decisions)の 3 と 5)、PDK によらないパッケージの印、パッケージの相対の `.include` のパスを install のディレクトリに
書き換えること、ライブラリをまたいだ RTL、依存のセルと同じ名前のプロジェクトのセルを作る、改名する、コピーする、取り込むときの
拒否、Undine の外で開いた Xschem と、シェルを持つプロセスが書き換える `project.json` の `trusted` の一覧を守ること、信頼のキーを
木に結び付けること、`lib lock --format 2` と `pdk verify --pin` の HTTP のルートやボタン(段階 1 と同じくコマンドラインだけ)。

<a id="decisions"></a>
<a id="open-questions"></a>

## 決定事項

2026-11-05 にユーザーと決めました。項目ごとに、決めたことと理由を書きます。

1. **manifest は TOML。** 人が手で書き、TOML はコメントを書けます。Python の `tomllib` で読めます。
2. **既定の index は GitHub の kadomoto-lab の organisation に置くリポジトリ**(`kadomoto-lab/undine-index`。名前は仮のもので、これから
   作る)。当面、その PR は kadomoto がレビューします。公開する人が少ないうちは、Undine のそばの 1 か所で足ります。
3. **1 つのプロジェクトに、1 つのパッケージの複数のメジャーバージョンは入れない(拒否する)。** サブサーキットとレイアウトの
   セルはセル名で名付けられるので、1 つのセルの 2 つのバージョンが 1 つのネットリストやレイアウトで衝突します。
4. **パッケージの間のセル名の重なりは解決のエラー**にし、セルと両方のパッケージを示します。セル名は作った人が書いたとおりに残り、
   重なりは何かを置く前に見つかります。
5. **ローカル名はプロジェクト全体で共通**にし、パッケージは置かれている名前ごとに install します。Xschem のネットリスタの
   バックエンドと Undine の外の Xschem は 1 つのパスの一覧で探すので、ライブラリごとの表を表せません。
6. **人が自分で追加するときも changeset を通し、1 クリックで採用する。** 人とエージェントの経路が 1 つになり、レビュー、採用時の
   マージ、evidence が同じように働きます。
7. **ストアからはハードリンクで install する**(別のファイルシステムのときや `UNDINE_STORE_LINK=copy` のときはコピー)。2 つ目の
   ローカル名や changeset の作業コピーにディスクを使いません。
8. **PDK の木のハッシュはライブラリごとに 1 つ**(と `libs.tech` に 1 つ)。より多くのライブラリと一緒に入れた PDK も検証に通り、
   変更はそれを含むライブラリについて報告されます。
9. **公開は、公開する人の `pkg check` と、index の PR に載せたその報告へのリンクにもとづく。** CI で DRC と LVS を実行するには
   CI に PDK(sky130 で variant ごとに 1.1 GB)が要ります。報告は commit と木に結びつくので、レビューする人は何が検証されたかを
   見られます。index の CI での DRC / LVS は後で行います。
10. **互換性タグは PDK プロファイルのパッケージが定義し、1 段(レイアウトとモデル)にする。** プロファイルはすでに PDK の commit
    ごとに木のハッシュを列挙しており、レイアウトだけの段があると、公開したセルの下でスペックの結果が変わりえます。
11. **当面、内容を固定するのは git のハッシュ(commit と木のハッシュ)だけ。** 内容の変更はどれも検出できます。署名は後で行います。
12. **ツールは、ツールごとのバージョン番号と出どころ、Undine のバージョンとその `pixi.lock` のハッシュとして記録し、食い違いは
    警告だけにする。** ツールはすでに pixi が固定しており、拒否すると、パッチリリースだけ違うツールを使う人が止まってしまいます。
13. **時間がたってからの入手: 選択肢 (b)。** `pkg vendor` が固定した内容を、`pkg licenses` と一緒にテープアウトのバンドルに
    書きます([取得とストア](#fetch-and-store))。テープアウトの evidence が、公開する人がリポジトリを保ち続けることに頼らなくなり、
    写しのためのサービスを運用する必要もありません。

<a id="later"></a>

### 後で行うこと

- 署名: index の commit やパッケージのタグへの署名。
- index の CI での DRC / LVS(と、そこでの PCell の variant の生成し直し)。CI に PDK を置く。
- 1 つのプロジェクトに 1 つのパッケージの複数のメジャーバージョンを入れること。サブサーキットとレイアウトのセルの名前に
  ライブラリの接頭辞を付けて行う。
