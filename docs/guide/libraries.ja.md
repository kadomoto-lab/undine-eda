# ライブラリと依存関係

プロジェクトには 3 種類のライブラリがあり、ライブラリブラウザは種類ごとにまとめて表示します:

| 種類 | 場所 | 編集 |
|---|---|---|
| **プロジェクトのライブラリ** | プロジェクトの `libs/<lib>/` | ここで編集 |
| **依存関係** | 固定したコミットの git リポジトリ。`.undine/deps/<lib>/` にインストール。[パッケージ](#packages)は自身が依存するパッケージも連れてくる | 読み取り専用。*update* で変更 |
| **PDK ライブラリ** | プロジェクトが固定している PDK([PDK](pdks.ja.md)) | 読み取り専用 |

<a id="dependencies"></a>
## 依存関係

プロジェクト間で共有するライブラリ(IP ブロック、チームの標準セル)は、リンクではなくパッケージのように取り込みます。
プロジェクトにはリポジトリと、ブランチ・タグ・コミットのいずれかを書き、`undine.lock` に解決したコミットを記録します。
こうすると、プロジェクトのどのチェックアウトでもまったく同じセルが手に入り、共有ライブラリの変更がプロジェクトに届くのは、
誰かが依存関係を更新したとき(`undine.lock` の変更として目に見える形)だけになります。

```sh
pixi run python -m undine lib add demo analog_ip https://github.com/me/analog_ip.git --rev v1.2.0 --subdir lib
pixi run python -m undine lib update demo analog_ip --rev v1.3.0
pixi run python -m undine lib sync demo          # after a checkout or a pull: install exactly what is locked
pixi run python -m undine lib sync demo --offline   # the same with no network, from the store
```

`lib sync` は、チェックアウトや pull の後に、ロックされたとおりのものをインストールします。

ブラウザでは、ライブラリブラウザのフォルダのアイコン(**新しいライブラリ**)の隣にあるパッケージのアイコンで
**依存ライブラリを追加** が開きます。依存関係の行にはバージョン(`v1.2.0 @ 2299146`)と、**更新** と **削除** があります。

- 依存関係のライブラリは、リポジトリの最上位(または `subdir`)にあるセルのディレクトリ(`<cell>.sch`、`<cell>.sym`、`<cell>.oas`)です。
  プロジェクトの回路図は、そのシンボルを `<lib>/<cell>.sym` として配置します。
- SPICE のネットリストには、セルの名前ごとにサブサーキットが 1 つあります。テストベンチが、別々のライブラリにある同じ名前の 2 つのセル
  (あなたの `inv` と依存関係の `inv`)にたどり着き、それらが違う行にネットリストされるときは、両方を示してネットリストを拒否します。
  自分のほうの名前を変えるか、両方の場所に同じセルを配置してください。同じ行にネットリストされる 2 つは一度だけ書きます
  ([トラブルシューティング](../troubleshooting.ja.md#two-cells-named-inv-are-netlisted-ainv-and-binv-and-they-differ))。
- **更新** は、変わったファイルと、それを使っているプロジェクトのセルを報告します。それらのセルのシミュレーションと LVS の結果は、
  ネットリストが変わるので[古く](verification.ja.md#outdated-results)なります。レイアウトのセルは配置したときにプロジェクトのライブラリへコピーされるので、
  配置し直すまでは古い形状のままです。報告にはそのセルの名前が出ます。
- **元に戻す:** undine.lock と project.json はプロジェクトの普通のファイルです。それらの変更を戻して(例: `git checkout HEAD~1 -- undine.lock project.json`)
  `lib sync` を実行すると、以前のバージョンが再びインストールされます。戻した project.json がもう指定しない依存関係は *leftover*(残りもの)です。
  そのシンボルはここでは解決され、新しいチェックアウトでは解決されないので、`lib status` はそれを `leftover` と報告し(終了コード 1)、
  `lib sync` はそのファイルを `.undine/deps/<lib>/` から、そのエントリを undine.lock から削除します。
- 依存関係のファイルは読み取り専用でインストールされます。それでも編集された場合は、`lib status` がそのライブラリを `modified` と報告し、
  `lib sync` で元に戻ります。
- 取得にはサーバーに `git` が必要です。リモートの URL(`https://` または `http://`、`ssh://`、`git://`、`git@host:path`)と、
  ローカルのリポジトリ(絶対パス、`~/...`、`file://`)が使えます。`rev` はブランチ、タグ、コミットのどれかです。
  `rev` も `subdir` も `-` で始められません。

<a id="the-store"></a>
### ストア

Undine は、取得した依存関係のファイルを、ユーザーごとに 1 つの、内容でアドレスする**ストア**(`~/.undine/store/`。
[ストア](../reference/data.ja.md#the-store))に一度だけ置き、取得に使ったリポジトリの写しも残します。`.undine/deps/<lib>/` はストアの
ファイルへの**ハードリンク**でできているので:

- いくつものプロジェクトや、プロジェクトの changeset の作業コピーが使うライブラリも、ディスクは一度分しか使いません。新しい
  バージョンは、変わっていないファイルを古いバージョンと共有します。
- 一度取得した依存関係は**ネットワークなし**でまたインストールできます: 別のプロジェクトで、前に取得したバージョンに戻す
  `git pull` の後で、あるいは `lib sync --offline` で。
- インストールしたファイルは読み取り専用(`-r--r--r--`)です。依存関係のファイルへの書き込みは断られます。それでも置き換えた
  ファイルは `modified` と報告され、`lib sync` がストアから入れ直します。

ストアとプロジェクトが別のファイルシステムにあるとき(またはファイルシステムがリンクを断るとき)は、何も言わずに代わりにコピーします
(これも読み取り専用)。ディスクをまた共有するには、`UNDINE_STORE` でストアをプロジェクトと同じファイルシステムに置きます
([トラブルシューティング](../troubleshooting.ja.md#dependencies-take-disk-space-in-every-project))。`python -m undine store verify` は
ストアを確かめ、`store gc` はこのマシンのどのプロジェクトも固定も install もしていないものを消します
([コマンドライン](../reference/cli.ja.md#command-line))。

**Undine を更新した後。** ストアより前の Undine が入れた依存関係は、まだどのストアにもありません。最初の `lib sync`(またはロックの
ダイアログの sync)が、ファイルが `undine.lock` と合うものをネットワークなしでストアに取り込みます。その後はオフラインでも sync
できます。

<a id="sync-installs-the-lock-as-it-is"></a>
### sync は lock をそのままインストールする

`lib sync` は `undine.lock` が固定するものをそのままインストールし、何も解決しません。ブランチやタグを見ることも、lock を変えることも
ありません。`project.json` が指定する依存関係を lock がそのとおりに固定していないとき(`git pull` が新しい `rev` を持ってきた、手で
依存関係を加えた、依存関係があって lock のないプロジェクト)は、何かを取得する前に、それぞれを示して sync を拒否します:

```
error: analog_ip: project.json asks for rev v1.3.0, undine.lock locks v1.2.0: run `python -m undine lib lock demo` to resolve it, or `python -m undine lib update demo analog_ip`; nothing was changed
```

`lib lock` は、`project.json` の指定どおりに lock が固定していないものを取得して固定し、それ以外には触れません。`lib update` は依存
関係を 1 つ動かします。その後で `lib sync` が通ります。こうして、sync がプロジェクトを確かめる対象を変えることはありません。変えるのは
`lib add`、`lib update`、`lib lock` だけで、その変更はレビューできる形で `undine.lock` に残ります。

`lib sync --offline`(または `UNDINE_OFFLINE=1`)は、ストアと、残したリポジトリからインストールし、どちらにもない依存関係を示して
拒否します: `offline, and not in the store: analog_ip @ 229914698b1f (sha e6d28176...)`。一度ネットワークありで sync してください。

!!! warning "HTTP 経由のローカルリポジトリ"

    `lib` コマンドはどのローカルリポジトリも受け付けます。ブラウザの **依存ライブラリを追加** と HTTP API(追加、更新、同期。
    クローンした `project.json` が指定するパスも)は、サーバーを `undine serve --dep-root DIR` で起動していなければローカルの
    リポジトリを拒否します。起動していれば、実際のパス(シンボリックリンクと `..` を解決し、その `.git` も含めて)がそれらの
    ディレクトリのどれかの下にあるリポジトリだけを受け付けます。`--import-root` が読み込むファイルに対してするのと同じです。
    `--dep-root` はディレクトリごとに一度ずつ指定します。これがないと、ブラウザからサーバーのディスクにある任意の git
    リポジトリをプロジェクトに読み込ませることができてしまいます。同じ確認は、ストアに残したリポジトリの写しにも及びます。
    リクエストは、それらのディレクトリの外にあるローカルリポジトリの写しからは、`lib` コマンドが取得したものであってもインストール
    できません。ストアのファイルだけからインストールする sync はリポジトリを読まないので、確かめません。

<a id="packages"></a>
## パッケージ

依存関係は、そのディレクトリ(リポジトリの最上位、またはその `subdir`)の最上位にマニフェスト `undine-package.toml` を持てます。
マニフェストがあれば、それは**パッケージ**です: 名前とバージョンを持ち、自身が依存するパッケージを示し(Undine はそれらも
インストールします)、セルがどの PDK 向けか、どのセルが公開かを示します。マニフェストのない依存関係は通常の git の依存関係で、
これまでどおり動きます。

パッケージのインデックスはまだありません。どのパッケージも、それが依存するパッケージも、git の URL と `rev` で見つけます。
`lib add` は、ほかの依存関係と同じようにパッケージを受け付けます:

```sh
pixi run python -m undine lib add demo analog_ip https://github.com/me/analog_ip.git --rev v1.2.0 --subdir lib
```

```
analog_ip: analog_ip 1.2.0 at 229914698b1f (4 cells, 1 internal), locked in undine.lock
  + sar_common 1.3.1 as sar_common, via analog_ip
  + bias_gen 0.3.2 as bias, bias_gen, via analog_ip
~ sar_common 1.3.1: 1 cell with .control (not trusted): a simulation using it needs `python -m undine lib trust demo sar_common`
```

`--package NAME`(**依存ライブラリを追加** の **パッケージ名**)は、リポジトリが持つべきパッケージを指定します。マニフェストが別の
パッケージを名乗るとき、またはマニフェストがないときは追加を拒否します。ライブラリブラウザは、パッケージの行にそのバージョン
(`analog_ip 1.2.0`)を表示し、別のパッケージが連れてきたパッケージには、それを必要とするパッケージを **経由** として表示します。

ほかのパッケージに依存するパッケージや、自身の名前以外の名前でインストールするパッケージには **lock の format 2** が必要です。
`new` で作ったプロジェクトの lock は format 1 なので、一度これを実行してください(ここに入っている PDK を固定します。
[undine.lock](#undinelock) を参照):

```sh
pixi run python -m undine lib lock demo --format 2
```

format 1 の lock では、`lib add`、`lib update`、`lib lock` はそのようなパッケージを拒否し、何も変えません:
`analog_ip: package analog_ip names dependencies (sar_common, bias): needs lock format 2: run python -m undine lib
lock demo --format 2 first; nothing was changed`。依存関係を持たず、自身の名前で追加するパッケージは、どちらの形式でも使えます。

**すべて確かめてから書く。** `lib add`、`lib update`、`lib lock`、`set-pdk` は、必要なマニフェストをまずすべて読み(解決するものは
git から、固定のままのものはストアかインストール済みのファイルから)、バージョン、名前、セル、PDK を確かめ、見つかった問題を
すべて挙げて一度に拒否します。最後は `nothing was changed` です。`project.json`、`undine.lock`、`.undine/deps/` を書くのは
その後だけです。HTTP では、問題を並べた 409 `resolution_refused` になります([HTTP API](../reference/api.ja.md#dependency-refusals))。
ブラウザは、入力したものを残したフォームの上のダイアログに問題を表示します。

<a id="the-manifest"></a>
### マニフェスト

```toml
[package]
name = "analog_ip"                 # lower-case letters, digits and _, starting with a letter (at most 64)
version = "1.2.0"                  # semver: MAJOR.MINOR.PATCH, e.g. 1.2.0 or 1.3.0-rc.1
description = "Bandgap, LDO and comparator for sky130"
license = "Apache-2.0"
authors = ["Kadomoto <k@example.org>"]
repository = "https://github.com/me/analog_ip.git"

[pdk]
family = "sky130"
variants = ["sky130A"]
commits = ["8afc8346a57fe1ab7934ba5a6056ea8b43078e71"]
built_with = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"   # the PDK its layouts were drawn and checked on

[dependencies]
sar_common = { git = "https://github.com/me/sar_common.git", rev = "v1.3.1", version = "^1.3" }
bias = { git = "https://github.com/me/bias_gen.git", rev = "v0.3.2", package = "bias_gen", version = "~0.3" }
common = { subdir = "common" }     # a sibling: this repository at this commit, subdir from the repository's top

[exports]
cells = ["bandgap", "ldo", "comparator"]

[files]
include = ["*.sch", "*.sym", "*.oas", "*.netlist.yaml", "*.intent.yaml", "rtl/**", "README.md", "LICENSE"]
```

- **`[package]`**: `name` と `version` は必須です。バージョンはパッケージの作者が付けるラベルで、インストールされるのは `rev` が
  指すコミットで、そのファイルで固定します。
- **`[dependencies]`**: 各キーは、このパッケージの回路図がその依存先のシンボルを配置するときの**ローカル名**です(`bias/ref.sym`)。
  値は `git` と `rev`(どちらも必須。タグ、ブランチ、コミット)を持つテーブルで、省略可能な `subdir`、`package`(持つべき
  [package] の名前。省略時はキー)、`version` を持てます。`{ subdir = "..." }` だけのものは**兄弟**です: 同じリポジトリの同じ
  コミットにある別のパッケージで、`subdir` はリポジトリの最上位から数えます。依存先もパッケージでなければなりません。通常の git
  の依存関係を指定できるのは `project.json` だけです。
- **`version`** は、`rev` が指すツリーの [package] のバージョンと照合します: `^1.3`(同じメジャーバージョンで 1.3.0 以上。`0.x`
  では同じマイナー)、`~0.3`(同じマイナー)、`=1.3.1`、`^` のない `1.3`(`^1.3` と同じ)、`*`(何でも)。プレリリースのある
  バージョン(`1.4.0-rc.1`)は、そのリリースのプレリリースを指定した要求にだけ合います。比較演算子(`>=1.2`、`<2`)、並び
  (`^1.2, <1.5`)、インデックスの要求(`sar_common = "^1.2"`、`{ version = "^2", index = "acme" }`)はパッケージのインデックスと
  ともに使えるようになるもので、代わりに書くものを示して拒否します
  ([トラブルシューティング](../troubleshooting.ja.md#an-index-requirement-or-a-version-range))。
- **`[pdk]`**: [PDK との互換性](#pdk-compatibility)を参照してください。省略可能で、ないときはパッケージを確かめず、`lib status` が
  そう表示します。
- **`[exports]`**: 公開するセルです。それ以外は**内部**のセルです: インストールされ、パッケージ自身のセルが配置し、いつもどおり
  ネットリストされますが、シンボルの選択と `list_symbols` には出ず、セルの一覧では `internal` と印が付きます。名前を指定すれば
  配置もできます。`[exports]` がなければ、すべてのセルが公開です。公開するセルはどれも、パッケージのファイルの中に `.sym`、
  `.sch`、`.oas` のどれかを持つ必要があります。
- **`[files]`**: `include` はパッケージを構成するファイルを並べます。[include のファイル集合と lock](#the-include-file-set-and-the-lock)
  を参照してください。ないときは、ディレクトリのすべてのファイルです。
- **`[checks]`** と **`[renames]`** はテーブルとして受け付けますが、まだ使いません(後で公開のために使います)。
- ほかのテーブルやキーは、その名前と既知のものを示して拒否します。`revision =` のような書き間違いが、黙って別の意味になるのを
  防ぐためです。

誤りのあるマニフェストは、場所(URL、コミット、subdir)、キー、すべきこと、そこに至った経路を示して拒否します:
`https://github.com/me/analog_ip.git @ 229914698b1f:lib: undine-package.toml: dependencies.bias.rev: missing: name a
tag, branch or commit, e.g. rev = "v1.2.0" (required by demo -> analog_ip (https://github.com/me/analog_ip.git v1.2.0
subdir lib))`([トラブルシューティング](../troubleshooting.ja.md#undine-packagetoml-is-refused))。マニフェストは 256 KiB 以下の
UTF-8 の通常のファイルです。シンボリックリンクは拒否します。

<a id="a-dependencys-own-dependencies"></a>
### 依存関係の依存関係

パッケージが依存関係を示すと、Undine はそれらを、さらにその依存関係も解決し、すべてをインストールします。プロジェクトが指定する
のは、自分で配置するパッケージだけです。`lib add` と `lib update` が解決するのは、指定した依存関係とそれが必要とするものだけで、
ほかの固定済みのパッケージは固定どおりのままです(その間に動いた `main` のような `rev` は追いません)。

- **1 つのパッケージに 1 つのバージョン。** ネットリストはサブサーキットを、レイアウトはセルを、セルの名前だけで呼ぶので、1 つの
  パッケージの 2 つのバージョンを 1 つのプロジェクトに入れることはできません。1 つのパッケージに 2 つの違うツリーを求める 2 つの
  要求は、両方の経路を示して拒否します: `sar_common: two versions are required, demo -> analog_ip 1.2.0 (v1.2.0 @
  229914698b1f) -> sar_common 1.3.1 (v1.3.1 @ 5b1c0e2a9d11) and demo -> sar_common 1.2.0 (v1.2.0 @ 0c4e2b7a1f30):
  align the revs (one version per package)`。ひし形(2 つのパッケージが 3 つ目の同じバージョンを必要とする)では、それを一度だけ
  インストールします。
- **要求に合わないバージョン** は、経路と `rev` のバージョンを示して拒否します。
- **ローカルのリポジトリ。** リモートの URL から来たパッケージは、マニフェストでローカルのリポジトリ(`file://`、`/path`、`~/...`)
  を指定できません。HTTP では、マニフェストが指定する URL もすべて、リクエスト自身の URL と同じく `--dep-root` で確かめます。
- **どのパッケージを誰が配置するか。** 別のパッケージが連れてきたパッケージは、プロジェクトの依存関係ではありません: `lib update`
  と `lib remove` はそれを拒否し、配置している依存関係を示します(`sar_common: placed by analog_ip; update or remove that
  instead`)。`lib remove analog_ip` は、もう何も必要としないパッケージも一緒に削除し、ほかの依存関係がまだ配置しているものは残して
  名前を示します。`lib update analog_ip` は、連れてきたパッケージ、動いたパッケージ、もう要らないパッケージを示します。
- **元に戻す** のは通常の依存関係と同じです: `project.json` と `undine.lock` を戻し、`lib sync` します。

<a id="local-names-and-aliases"></a>
### ローカル名と別名

**ローカル名** は、ライブラリのシンボルを配置するときの名前です: `C {bias/ref.sym}` は、`bias` としてインストールされたもののセル
`ref` を配置します。ローカル名は**プロジェクト全体で共通**です: 各パッケージは、それを配置するローカル名ごとに
`.undine/deps/<ローカル名>/` としてインストールされ(同じファイルへのハードリンクなので、2 つ目の名前はディスクを使いません)、
シンボルの検索パスはディレクトリの 1 つのリストのままです。これで、Xschem 自身のネットリスタや、プロジェクトで開いた Xschem も、
パッケージの依存関係を見つけられます。

- **パッケージ自身の名前。** パッケージの回路図は自身のセルを `<名前>/<cell>.sym` として配置するので、パッケージは [package] の
  名前でもインストールされます。`--package bias_gen` を付けて `bias` として追加すると、`bias` と `bias_gen` の両方でインストール
  されます(`installed as bias and bias_gen`)。ブラウザは `bias` の行に `→ bias_gen`、`bias_gen` の行に `= bias` と表示します。
- **1 つの名前に 1 つのパッケージ。** 1 つのローカル名を別々のパッケージに与える 2 つのライブラリは、両方を示して拒否します
  (`bias: one local name for two packages, ...: give one of them another local name in its requirer's
  [dependencies]`)。名前は大文字と小文字を区別せずに比べます(大文字小文字を区別しないファイルシステムでは `IP` と `ip` は 1 つの
  ディレクトリです)。
- **予約された名前。** `.undine/deps/` はシンボルの検索パスの先頭にあるので、ローカル名は、プロジェクトのライブラリの名前、PDK の
  ライブラリの名前、`devices`、Xschem のライブラリや `project.json` の `symbol_paths` の中のフォルダの名前にはできません。
  そのライブラリを隠してしまうからです。新しいローカル名は拒否します(`lib add ... sky130_fd_pr`: `sky130_fd_pr: the PDK library
  sky130_fd_pr has that name, and .undine/deps would shadow it: choose another local name; nothing was changed`)。
  `project.json` にすでにある名前はそのまま使え、`lib status` がほかのライブラリを隠していることを注記します。
- 新しいプロジェクトのライブラリにも、依存関係がインストールされている名前は付けられません。

<a id="cell-names-are-disjoint"></a>
### セルの名前は重ならない

ネットリストではセルの名前ごとにサブサーキットが 1 つ、レイアウトでは名前ごとにセルが 1 つなので、すべてのパッケージ、
プロジェクトのライブラリ、PDK のライブラリのセルは、互いに違う名前でなければなりません。解決は重なりを拒否し、セルと両側を
示します: `comparator: analog_ip 1.2.0 and sar_common 1.3.1`。

- 拒否するのは、片側がマニフェストのあるパッケージのときだけです。2 つの通常の git の依存関係や、通常の依存関係とプロジェクトの
  ライブラリは(これまでどおり)拒否しません。`lib status` は、通常の依存関係のセルと同じ名前のプロジェクトのセルを注記し、両方を
  配置するテストベンチはネットリストのときに両方を示して拒否されます
  ([トラブルシューティング](../troubleshooting.ja.md#two-cells-named-inv-are-netlisted-ainv-and-binv-and-they-differ))。
- プロジェクトについて数えるのは**論理セル**だけです: `.sch`、`.sym`、`.sv`、`.v` を持つセル。配置したセルのレイアウトの
  コピー(パッケージのセルを配置するとあなたのライブラリにできる、`.oas` だけのもの)は重なりではありません。
- パッケージ自身のレイアウトだけのセル(配置したセルのコピー、サブセル)が重なるのは、そのパッケージが依存していないパッケージの
  論理セルとだけです。
- 2 つのローカル名でインストールした 1 つのパッケージは 1 つのパッケージで、重なりではありません。
- `lib status` は、パッケージのセルと同じ名前のプロジェクトのセルを問題として報告します: 自分のほうの名前を変えてください。

<a id="pdk-compatibility"></a>
### PDK との互換性

パッケージの `[pdk]` は、そのセルがどの PDK 向けかを示し、すべてのパッケージを(依存関係が連れてきたものも)、プロジェクトの PDK
(`undine.lock` が固定するもの)と照合します:

- `family` はプロジェクトの PDK のファミリ(ciel のもの: `sky130`、`gf180mcu`、`ihp-sg13`)でなければならず、プロジェクトの
  バリアントは `variants` に含まれていなければなりません(sky130A と sky130B はメタルの積層が違います)。
- PDK のコミットは `commits` に含まれていなければなりません(コミット全体。PDK のコミットには順序がないので、範囲はありません)。
  `commits` も `compat` もなければ、`built_with` だけが受け入れるコミットです。3 つのどれもなければ、そのファミリとバリアントの
  どのコミットでも受け入れます。
- `compat` は互換性のタグ(`sky130/2025`)を示します。タグは PDK プロファイルが定義するもので、この Undine はまだどのタグも
  知りません。タグだけを示すパッケージは拒否し、コミットを `commits` に並べるよう示します。
- `built_with` は、レイアウトを描いて確かめた PDK です。受け入れる別のコミットで使っていると、`lib status` が注記します:
  `drawn on 8afc8346a57f, used at 0c4e2b7a1f30`(レイアウトをもう一度確かめてください)。
- PDK のないプロジェクトは、`[pdk]` を持つパッケージに合いません。バージョンのない PDK(ciel や volare が入れたのではない、
  ただのディレクトリ)は、コミットを確かめられないので、`commits` か `built_with` を示すパッケージに合いません: `pdk fetch` で
  入れてください。format 1 の id の 12 文字のコミットは、パッケージのコミット全体と前方一致で照合します。ネットワークでは何も
  調べません。

合わないパッケージは、追加、更新、固定のときに拒否します: `sar_common 1.3.1 does not fit this project's PDK:
sky130A@0c4e2b7a1f30 is not among commits 8afc8346a57f (required by demo -> analog_ip 1.2.0 (...) -> sar_common 1.3.1
(...))`。固定済みのパッケージが受け入れない PDK へプロジェクトを移すことも拒否します
([PDK](pdks.ja.md#moving-a-project-to-another-pdk-version))。`pdk verify --pin` と `lib lock --format 2` は、パッケージが何を
受け入れるかにかかわらず、入っている PDK を固定します(固定はそこにあるものの記録です)。`lib lock --format 2` は合わない
パッケージごとに `!` の行を表示します。その後は、パッケージか PDK が動くまで、`lib status` がそれぞれを問題として残し、`lib sync`
は `~` の警告を付けてインストールします。

<a id="trusting-a-dependencys-code"></a>
### 依存関係のコードを信頼する

セルをシミュレーションすると、そのネットリストが ngspice に指示することが実行されます。そして依存関係は誰かほかの人の成果物です:
`.control` ブロックは `shell` を実行でき、`.include` はサーバーのどのファイルでも読めます。そこで Undine は、ジョブを実行する直前に、
誰も見ていないコマンドを実行したりファイルを読んだりする依存関係の内容を、人がその依存関係をプロジェクトで信頼していない限り
拒否します。これは通常の git の依存関係も含め、**すべての**依存関係に当てはまります。

| ツール | 依存関係の中で拒否するもの |
|---|---|
| ngspice(シミュレーション、スイープ、仕様の確認) | `.control` ... `.endc` ブロック(ngspice と同じく `.control` で始まるカードすべて)、`*#` の行、そして PDK の外の絶対パス、`..` を含むパス、`$` や `~` が残るパスを持つ `.include`、`.lib`、XSPICE の `file=` |
| LVS(KLayout、Magic + Netgen) | 同じパス(そこでは制御コードは実行されません) |
| Verilator(RTL のシミュレーション) | どこに現れても `$c` と `$system`(マクロの引数としても)、`` `systemc_* ``、DPI の import、絶対パスや `..` や `~` を含む `` `include `` / `$fopen` / `$dumpfile` / `$readmem*` / `$writemem*` のパス、それらのコードを組み立てる `` `define `` の本体 |
| LibreLane(トップのライブラリが依存関係であるフロー) | Verilator のシミュレーションが拒否するもの。実行に何かをコピーする前に確かめます |
| Verilator の lint | `` `include `` のパスだけ |
| Xschem のネットリスタ(netlist ビューの Xschem バックエンド) | 依存関係の `.sch` や `.sym` の属性にある `tcleval(...)`(`tcl\eval(` のようにバックスラッシュを交えて書いたものも)(と `[` や `$` を含む `expr(`) |

ある行が依存関係のものとみなされるのは、それが依存関係のセルから来たとき、またはそのシンボルの 1 つから作られたとき(あなたの
回路図に配置したシンボルの `format` や `template`)です。`..` のない相対パスは通り、PDK のディレクトリの下の絶対パスも通ります:
PDK のモデルファイルは PDK として信頼されます。Undine 自身のバックエンドでの netlist ビューは警告するだけです(`not trusted: would
be refused in a simulation: ...`)。拒否は、見つかったものすべて、セル、依存関係、すべきことを示します:

```
ip/opamp (dependency analog_ip 1.2.0, not trusted): code block s1 line 1: .control; a person can trust analog_ip@1.2.0 in this project (undine lib trust demo ip, or Trust... in Undine); an agent cannot
```

**信頼する** のは人の判断で、1 つのプロジェクトで 1 つのバージョンに対して行います:

- コマンドラインから: `lib trust PROJECT NAME` は見つかったものを表示して尋ねます(`trust analog_ip@1.2.0 in demo? [y/N]`)。
  `--yes` を付けると尋ねません。端末がなく `--yes` もなければ拒否します。`lib trust PROJECT NAME --untrust` で信頼を取り消します。
- ブラウザで: 拒否の中の **信頼...**(シミュレーション、LVS、RTL のパネル)か、ライブラリブラウザの依存関係の行の盾のボタンで、
  見つかったものをファイルと行ごとに並べたダイアログが開きます。**信頼する** は、表示しているバージョンちょうどを信頼します
  (その間に sync や update があると、もう一度尋ねます)。
- エージェントにはできません: MCP のツール、`undine call` のコマンド、クライアントのメソッドのどれも信頼できず、エージェントは
  あなたに尋ねるよう指示されています([設計エージェントと MCP](assistant.ja.md))。

信頼は、バージョンごとのキーとして `project.json` に残ります: `"trusted": ["analog_ip@1.2.0", "ip@229914698b1f"]`。パッケージの
名前とバージョン、マニフェストのない依存関係ではそのローカル名とコミットです。`lib update`、`lib remove`、`lib lock` は、依存関係を
変えたり外したりするとそのキーを取り除くので、新しいバージョンはもう一度信頼されるまで拒否されます。`lib sync` は取り除くものの
キーを取り除き、ロックになかった内容のパッケージを加えると、以前から残っているその名前のキーを取り除きます(名前とバージョンは
どのリポジトリでも名乗れるからです)。changeset の作業コピーはプロジェクトの今の信頼だけを使うので、人が与えた信頼も取り消した
信頼も、エージェントの開いている changeset に届きます。信頼はプロジェクトで与えるもので、changeset のプレビューでは与えられません
(そこでの拒否は **信頼...** の代わりにそのことを示します)。

`lib status` は、そのような内容を持ち信頼されていない、インストール済みの依存関係をそれぞれ注記し、その行に `trusted` か
`not trusted` を表示します。`lib add` と `lib update` は、それぞれについて `~` の行を表示します。

!!! warning "信頼が及ばないもの"

    この規則は、Undine がジョブを実行するときに当てはめます。Undine の外でプロジェクトを開いた Xschem や、シェルを持つプロセスが
    `project.json` を書き換えることは確かめません。信頼はパッケージの名前とバージョンに結び付き、コミットには結び付きません: 手で
    同じバージョンの別のコミットに書き換えた `undine.lock` を sync しても、信頼は残ります。`undine.lock` の変更は、コードと同じように
    レビューしてください。

<a id="the-include-file-set-and-the-lock"></a>
### include のファイル集合と lock

`[files] include` は、パッケージを構成するものを並べます。format 2 の lock では、`lib add`、`lib update`、`lib lock` はそのファイル
(とマニフェスト)だけをインストールし、エントリに `"files": "include"` と記録します。その `tree` は含めたファイルだけのハッシュで、
どのマシンでも同じです。`include` の外のファイル(テスト、CI のスクリプト、大きな特性評価の結果)は、ストアに取得もインストールも
しません。

- パターンは、パッケージのディレクトリの下のパス全体に `/` 区切りで合わせます: `*`、`?`、`[...]` は 1 つの部分の中だけに合うので、
  `*.sch` は最上位のファイルだけに合います。`**` はスラッシュの間に単独で書き、いくつの部分にも合います(`rtl/**`)。先頭の `/`、
  `..`、空の部分は使えません(ディレクトリは `dir/**` と書きます)。
- 選んだファイルには、最上位のセル(`.sch`、`.sym`、`.oas`、`.sv`、`.v`)が少なくとも 1 つと、公開するセルがすべて含まれて
  いなければなりません。
- 以前に固定した依存関係(`files` のキーのないエントリ)は、`lib update` か `lib lock` が書き直すまで、ディレクトリのすべての
  ファイルを持ちます。`lib sync` はエントリを変えません。format 1 の lock では、すべてのファイルをインストールします。

format 2 の lock は、直接の依存関係を `libraries` に、マニフェストを通してだけ届くパッケージを `packages` に、そのバージョン、
ローカル名、依存関係とともに保ちます([undine.lock](../reference/data.ja.md#undinelock))。

## undine.lock

ロックファイルには、依存関係のほかに、設計が依存しているものも記録します: PDK のバージョン(`project.json` が固定する id。
[プロジェクトが PDK を固定するしくみ](pdks.ja.md#how-a-project-pins-its-pdk)を参照)と、KLayout、Xschem(回路図が使う `devices/` のシンボルを含む)、
ngspice、Magic、Netgen のバージョンです。新しいプロジェクトは、ロックされた状態で始まります。
ツールのバージョンは、Undine が最初に見つけた実行ファイルのものです。`$UNDINE_TOOLS` が設定されているとき(`pixi run` が設定します)は、
`$UNDINE_TOOLS/bin`、次に Undine が動いている Python 環境の bin ディレクトリ、次に `PATH` の順に探し、`~/undine-tools` は探しません。
`$UNDINE_TOOLS` が設定されていないときは、Undine が動いている Python 環境(pixi、conda、仮想環境。システムの Python は除く)、
次に `~/undine-tools/bin`、次に `PATH` の順に探します。

lock には 2 つの形式があります。lock がすでに format 2 でなければ、どのコマンドも **format 1** で書きます。依存関係はコミットと、
`git archive` が出したファイルに対する `sha` で、PDK は id で、ツールはバナーで固定します。

```json
{
  "format": 1,
  "pdk": "sky130A@8afc8346a57f",
  "tools": {"klayout": "KLayout 0.30.12", "xschem": "XSCHEM V3.4.5", "ngspice": "ngspice-47 : ...", "magic": "8.3.678", "netgen": "Netgen 1.5.272"},
  "libraries": {
    "analog_ip": {"git": "https://github.com/me/analog_ip.git", "rev": "v1.2.0", "subdir": "lib",
                  "commit": "229914698b1fb16a629bb73109b39eb1aba40eae", "sha": "e6d28176…"}
  }
}
```

**format 2** はより多くを固定し、書くのは `pdk verify PROJECT --pin` と `lib lock PROJECT --format 2` の 2 つのコマンドだけです
(古い Undine は format 2 の lock を書き換えずに拒否するので、先にプロジェクトの全員がこのバージョンを使っていることを確かめて
ください):

```json
{
  "format": 2,
  "pdk": {"id": "sky130A@8afc8346a57f", "family": "sky130", "variant": "sky130A",
          "commit": "8afc8346a57fe1ab7934ba5a6056ea8b43078e71",
          "source": "ciel static-web:https://fossi-foundation.github.io/ciel-releases",
          "libraries": ["sky130_fd_pr", "sky130_fd_sc_hd"],
          "tree": {"libs.tech": "sha256:41c0…", "libs.ref/sky130_fd_pr": "sha256:9a7e…", "libs.ref/sky130_fd_sc_hd": "sha256:0f3b…"}},
  "tools": {"undine": "0.1.0 (3f9c2e1)", "pixi_lock": "sha256:13be…",
            "klayout": {"version": "0.30.12", "from": "pixi"}, "xschem": {"version": "3.4.5", "from": "pixi"},
            "ngspice": {"version": "47", "from": "pixi"}, "magic": {"version": "8.3.678", "from": "pixi"},
            "netgen": {"version": "1.5.272", "from": "pixi"}},
  "libraries": {
    "analog_ip": {"git": "https://github.com/me/analog_ip.git", "rev": "v1.2.0", "subdir": "lib",
                  "commit": "229914698b1fb16a629bb73109b39eb1aba40eae", "tree": "sha256:e6d28176…"}
  }
}
```

- **依存関係。** format 2 の lock では、`lib add`、`lib update`、`lib lock` は依存関係を `tree` で固定します。git のオブジェクトから
  直接読んだファイルのハッシュで、`.gitattributes` や `core.autocrlf` がどうであれ、どのマシンでも同じになります
  ([正確な規則](../design/packages.ja.md#index))。すでに固定したエントリは、それらのコマンドが書き直すまで `sha` のままです。
  `lib sync` はエントリを書き換えません。
- **パッケージ。** それらのコマンドが解決した[パッケージ](#packages)のエントリは、`package`、`version`、ローカル名の `names`、
  `deps`(`{ローカル名: パッケージ}`)、`files: "include"` も記録します。ほかのパッケージだけが依存するパッケージは、パッケージ名を
  キーにした別の表 `packages` に入ります([データ配置](../reference/data.ja.md#undinelock))。`lib sync` は各エントリを、その
  すべての名前でインストールします。
- **PDK。** commit 全体、どこから来たか、ライブラリ、部分ごとのハッシュ。これで、入れた後に変わった PDK に気づけます
  ([PDK をファイルで固定する](pdks.ja.md#pinning-the-pdk-by-its-files))。
- **ツール。** Undine 自身のバージョンと commit、その `pixi.lock` のハッシュ(Undine が pixi の環境の外で動いていれば null)、
  ツールごとのバージョン番号と実行ファイルの出どころ(`pixi`、`UNDINE_TOOLS`、`~/undine-tools/bin` なら `undine-tools`、ほかの conda や
  仮想環境なら `env`、`PATH`)。

`lib status`(とウィンドウ上部の **バージョン固定済み** / **固定バージョンとの不一致 N 件** のバッジ)は、ここと食い違うものをすべて
表示します。行は 3 種類です:

- **問題**(`!`。バッジに件数が出て、`lib status` は終了コード 1): project.json が指定するとおりにロックされていない依存関係
  (`unlocked`)、ない依存関係、別のコミットや変更された依存関係、project.json がもう指定しないのに、まだインストールかロックされて
  いる依存関係(`leftover`。`lib sync` が削除します)、PDK がない、または lock の固定と違う、undine.lock がない、プロジェクト外の
  ディレクトリからリンクしたライブラリ、追加の `symbol_paths`(後の 2 つはプロジェクトと一緒にバージョン管理されません)。
  パッケージについてはさらに: 固定した PDK(または project.json が求める PDK)に合わないパッケージ、パッケージのセルと同じ名前の
  プロジェクトのセル、パッケージが必要とするものをインストールしない lock(依存するパッケージが lock にない、1 つのローカル名が
  2 度使われている: `lib lock` で解決します)、`name@version` のキーでない project.json の `trusted` の項目。
- **警告**(`~`): バージョンや出どころの違うツール。ここで見つかった実行ファイルの場所も表示します。例:
  `klayout: locked 0.28.17, here 0.30.12 from PATH (/usr/bin/klayout)`。これは更新ではなく、別の場所にある同じツールが先に見つかった
  だけのことがよくあります(CLI とサーバーが別の環境から起動された場合、`$UNDINE_TOOLS` があるかないかが違う場合など)。ツールは
  pixi が固定していて、パッチリリースの違いで誰かを止めるべきではないので、ツールで lock が通らなくなることはありません。ほかに、
  Undine のバージョンの違いと、`pixi_lock` が null の format 2 の lock(ツールが固定されていなかった)も警告です。
- **注記**: PDK が未検証、未確認、未固定であること([PDK の状態](pdks.ja.md#pdk-states))、同じバージョンの Undine の別の commit や
  別の `pixi.lock`、format 1 では出どころを記録していないこと。依存関係については: シミュレーションが拒否する内容を持ち
  [信頼](#trusting-a-dependencys-code)されていない依存関係、`[pdk]` のないパッケージ(または通常の依存関係: PDK との互換性を
  確かめていない)、`built_with` と別のコミットで使っているパッケージ、通常の依存関係のセルと同じ名前のプロジェクトのセル、
  `project.json` にすでにあって Xschem のライブラリや `symbol_paths` のフォルダを隠している依存関係の名前。

依存関係の行には、パッケージとバージョン、プロジェクトが信頼しているかどうかを表示します(別のローカル名のパッケージは
`-> bias_gen 0.3.2`)。別のパッケージが連れてきたパッケージは、次の行にその経路を表示します:

```
  PDK                  pdk        unverified     sky130A@8afc8346a57f
  analog_ip            dependency ok             https://github.com/me/analog_ip.git v1.2.0 @ 229914698b1f  analog_ip 1.2.0, trusted
  sar_common           dependency ok             https://github.com/me/sar_common.git v1.3.1 @ 5b1c0e2a9d11  sar_common 1.3.1, not trusted
                       via demo -> analog_ip 1.2.0 (v1.2.0 @ 229914698b1f) -> sar_common 1.3.1 (v1.3.1 @ 5b1c0e2a9d11)
~ klayout: locked 0.30.12 from pixi, here 0.30.12 from PATH (/usr/bin/klayout)
  note: PDK sky130A@8afc8346a57f: unverified (trust on first use): it matches the hashes pinned in undine.lock, which no PDK profile publishes yet
  note: library sar_common: 1 cell with .control (not trusted): a simulation using it needs `python -m undine lib trust demo sar_common`
as locked
```

ロックはバージョンだけを記録し、マシンごとに違うパスは記録しません。ツールを意図して更新した後は、`lib lock` で使用中のバージョンを
記録します。format 2 の lock では、ツールだけを記録し、PDK の固定には触れません。`project.json` の依存関係を変えた `git pull` の後も
`lib lock` を実行し、それから `lib sync` します([sync は lock をそのままインストールする](#sync-installs-the-lock-as-it-is))。
プロジェクトを別の PDK バージョンへ移すときは、`lib lock` ではなく `set-pdk` を使います。`set-pdk` は `project.json` も書き換え、
古いバージョンを `pdk_history` に残します([プロジェクトを別の PDK バージョンへ移す](pdks.ja.md#moving-a-project-to-another-pdk-version))。
lock を作ったコマンドはそう表示します: `created undine.lock (format 1)`。

lock を読むコマンド(`lib`、`pdk`、`set-pdk`、ロックのダイアログ)はどれも、より新しい Undine が作った、より新しい形式(3 以上)の
lock を拒否し、何も変えません: Undine を更新してください(`git pull` の後に `./setup.sh`)。正しい JSON でない lock や、どの Undine も
書かない `format` を持つ lock も同じように拒否します: 直すか、`git checkout undine.lock` で元に戻してください
([トラブルシューティング](../troubleshooting.ja.md#undinelock-has-format-3-made-by-a-newer-undine))。

`undine.lock` はバージョン管理に入れてください。`.undine/`(インストールした依存関係)は
[`git-setup`](../reference/data.ja.md#version-control-with-git) が除外します。
