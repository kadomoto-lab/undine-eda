# HTTP API

UI が行うことはすべて Design API への HTTP 呼び出しです。スクリプトやエージェントも同じ呼び出しを使います。動いているサーバー自身が、その説明を提供しています:

| URL | |
|---|---|
| `http://127.0.0.1:8700/docs` | 対話的な API ドキュメント(Swagger UI) |
| `http://127.0.0.1:8700/redoc` | 同じものの ReDoc 版 |
| `http://127.0.0.1:8700/openapi.json` | OpenAPI スキーマ |

決まりごと:

- プロジェクト、ライブラリ、セルはパスで指定します: `/api/p/{project}/l/{lib}/...`。changeset の作業コピーは、プロジェクト
  `{project}~{changeset}` です([変更セット](../guide/changesets.md)を参照)。
- `X-Undine-Actor: user | agent:<name>` で編集した主体を示し、ボディの `reason` で理由を記録します([操作ログ](data.md#operation-log)を参照)。
- エラーは `{"detail": "...", "code": "...", "params": {}}` で、400(不正なリクエスト)、403(許可されていない。例: 読み取り専用のライブラリ、人が信頼していない依存関係のコード)、
  404(そのオブジェクトがない)、409(ほかの誰かがその changeset を編集中、送り返したテキストがその後に変わったファイルから読んだものだった(下記参照)、
  または changeset のマージが衝突した([changeset](#changesets) を参照))、502(LLM の接続に失敗)のいずれかです。`code` は種類を示します
  (`invalid_request`、`busy`、`stale`、`merge_conflict`、`not_set_up`、`unsupported_library`、`untrusted`、`resolution_refused` など)。`detail` は英語で、UI は知っている種類を
  自分の言語で表示します。種類に固有の値は、ボディの最上位と `params` の両方に入ります。
- テキスト全体を送り返すビュー、RTL(`POST .../c/{cell}/rtl` の `op: save`)と設計の意図の YAML(`POST .../c/{cell}/intent/edit` の `op: replace`)は、
  `"base"` を受け取ります。編集のもとにしたテキストと一緒に `GET` が返した `"sha"` です。その後にファイルが変わっていれば(git の merge、checkout、pull)、
  その変更を元に戻してしまう代わりに 409 になります。`base` がなければ、これまでどおり保存します。
- `GET /api/projects` は、`project.json` を読めないプロジェクト(git のマージが衝突の印を残した)を `{"name", "error"}` として一覧に入れます。開くと、その理由を示す 400 になります。
- エージェントは [MCP エンドポイント](../guide/assistant.md#mcp-external-llm-clients)も使えます。

<a id="projects-pdks-and-library-dependencies"></a>
## プロジェクト、PDK、ライブラリの依存関係

| 呼び出し | |
|---|---|
| `GET /api/pdks` | サーバーが見つけた PDK: `id`(`name@` とバージョンの先頭 12 文字)、`name`、`family`、バージョン全体の `version`、Undine がそれをどこまでサポートするか(`profile`) |
| `POST /api/pdks/rescan` | サーバーの PDK のルートの下で PDK を探し直す(サーバーの起動後に入れたものや取得したもの)。再起動は要らない。`GET /api/pdks` と同じ形の `{pdks}` を返す。PDK の解決が別の install に変わった(またはやっと見つかった)プロジェクトの、開いているレイアウトとエディタは、次に使うときに読み直す |
| `GET /api/projects`、`POST /api/projects` `{name, pdk?}` | プロジェクトの一覧。PDK に固定したプロジェクトを作成(id、または名前だけ。名前だけなら Undine が PCell を動かせる版を選び、テストした版が先) |
| `GET /api/p/{project}` | プロジェクトと、その PDK、ライブラリ(自身のもの、依存関係、PDK のもの)。各ライブラリは `package`(依存関係のパッケージ名。なければ null)、`direct`(ほかのパッケージだけが配置するパッケージなら false)、`via`(それを配置するパッケージ)を持つ。パッケージの `[exports]` に入らない依存関係のセルは、セルの一覧で `internal: true` を持ち、配置に出すシンボルからは外れる |
| `POST /api/p/{project}/libs` `{name}` | プロジェクト自身のライブラリを作成(`libs/<name>`。project.json に記録) |
| `POST /api/p/{project}/pdk` `{pdk}` | プロジェクトを別の PDK バージョンに固定し直す(`pdk_history` と undine.lock に記録。undine.lock がなければ作る。format 2 の lock には、先に木のハッシュを取った新しい PDK の固定を書く)。`{from, to}` を返す。changeset のプレビューでは使えない |
| `GET /api/p/{project}/deps` | ロックしたプロジェクトと、ここにインストールされたものとの比較(`lib status`。下記)。project.json がもう指定しないのに、まだインストールかロックされている依存関係は `state: "leftover"` と問題とともに一覧に入る(なので `ok` は false) |
| `POST /api/p/{project}/deps` `{name, git, rev?, subdir?, package?}` | [ライブラリの依存関係](../guide/libraries.ja.md)を追加(`lib add`)。それが依存するパッケージも一緒に、取得し、読み取り専用でインストールし、ロックする。lock のエントリに `name`、`cells`、`public_cells`、`internal_cells`、`created`(undine.lock を作ったか)、`package` と `version`(通常の依存関係では null)、`resolved`(解決した各エントリ: `{name, ...entry}`。パッケージなら `package`、`version`、`names`、`via`、`flagged`、`trusted` も)、`flagged`(`{spice, rtl, tcl}`: 信頼の規則がその中に見つけるもの)、`trusted` を加えて返す。`package`: マニフェストが名乗るべき [package] の名前。ローカルの `git`(`file://`、`/path`、`~/path`)は、サーバーを `--dep-root` で起動し、リポジトリがそのディレクトリのどれかの下にあるときだけ受け付ける。同期と更新でも、マニフェストが指定するどの URL でも同じ。拒否は[下記](#dependency-refusals) |
| `POST /api/p/{project}/deps/sync` `{offline?}` | 依存関係をすべてロックどおりにインストールし(`lib sync`)、残りものをそれぞれ削除する(そのファイルと undine.lock のエントリ)。`{libraries: [{name, action, commit}]}` を返す。`action` は `ok`(すでにロックどおり)、`installed`、`removed` のどれか(`removed` では `commit` が null のことがある)。パッケージの行には `package` もあり、固定した PDK が合わないパッケージの行には `warning`(`CHAIN: does not fit the PDK ID: REASON`)がある。そのパッケージもインストールし、`GET /deps` は問題として報告する。本文はなくてもよい: `{"offline": true}` は取得せず、`false` は取得してよく、本文なし(または `null`)ならサーバーの `UNDINE_OFFLINE` に従う。固定どおり: project.json の指定どおりに依存関係を固定していない lock は、何かを読む前に拒否する(`lib lock` で解決する)。全部か無か: 1 つでも失敗すると(リモートに届かない、ファイルがロックと合わない、オフラインでストアにない)、どれもインストールせず、どれも削除しない。[ストア](data.ja.md#the-store)にある依存関係はそこから入れ、リポジトリを読まない。ローカルのリポジトリは、project.json が指定するものも、git がそれを読む前(取得、またはストアに残したリポジトリの commit)に `--dep-root` と照らし合わせる |
| `POST /api/p/{project}/deps/lock` | `lib lock`: project.json の指定どおりに undine.lock が固定していないものを取得し、インストールして固定し(add と同じく `--dep-root` の規則のもとで)、今使っているツールのバージョンを記録する(format 1 の lock では PDK の id も)。状態に `resolved: [{name, git, rev?, subdir?, commit, sha または tree}]` を加えて返す。lock はその形式のまま書く。format 2 にするのはコマンドラインの役目(`lib lock --format 2`、`pdk verify --pin`) |
| `POST /api/p/{project}/deps/{name}/update` `{rev?}` | 依存関係を `rev` に移す(既定はもう一度同じ rev)。`{name, from, to, rev, changed, used_by, packages_changed}` を返す: 変わったファイルと、それを使うセル、そして動いた、連れてきた(`from` が null)、もう要らない(`to` が null)エントリごとの `packages_changed: [{package, names, from, to, changed}]`。動いたものの信頼は project.json から取り除く |
| `POST /api/p/{project}/deps/{name}/remove` | 依存関係を、もう何も必要としないパッケージと一緒に削除。`{ok, name, removed_packages: [{package, version, commit, names}], kept: [{name, placed_by}]}` を返す(通常の依存関係では `package` は null。`kept`: ほかの依存関係がまだ配置しているパッケージ) |
| `GET /api/p/{project}/trust` | プロジェクトが信頼しているものと、インストールされた各依存関係が持つもの: `{trusted: [keys], dependencies: [{name, package, version, key, trusted, findings}]}`。ローカル名ごとに 1 行。changeset のプレビューでは `trusted` にプロジェクトのキーも入る。見つかったものは `{content, cell, file, where, kind, line, text}`([下記](#trusting-a-dependency)) |
| `POST /api/p/{project}/trust` `{name, token}` または `{name, untrust: true}` | `name` としてインストールされた依存関係を、種類 `trust` の確認のトークンで信頼する。または(トークンなしで)その信頼を取り消す: `{key, trusted}`。[下記](#trusting-a-dependency) |

<a id="the-lock-status"></a>
### ロックの状態

`GET /api/p/{project}/deps`(と `/deps/lock`)の答え:

| フィールド | |
|---|---|
| `format` | lock の形式、1 か 2。undine.lock がなければ null |
| `libraries` | `[{name, kind, state, ...}]`: 自身のライブラリ(`kind: "own"`。プロジェクトの外からなら `"linked"`)と依存関係(`kind: "dependency"`)。依存関係には `git`、`rev`、`subdir`、`locked` と `installed`(commit)、`state`(`ok`、`unlocked`、`missing`、`other version`、`modified`、`leftover`)、`hash`(format 1 のエントリなら `"sha"`、format 2 なら `"tree"`、固定していなければ null)、`in_store`(ファイルがすべてストアにあり、sync にネットワークが要らないか)。ローカル名ごとに 1 行: project.json が指定する依存関係をその順に、次にパッケージが配置する名前、最後に残りもの。依存関係の行にはさらに `package` と `version`(通常の依存関係では null)、`names`(そのエントリのすべてのローカル名)、`direct`、`via`(それを配置するパッケージ)、`chain`(`demo -> analog_ip 1.2.0 (v1.2.0 @ 229914698b1f) -> ...`、または null)、`trusted` と `flagged`(`{spice, rtl, tcl}`。インストールされていなければどちらも null)、`internal_cells`、`pdk`(`{compatible, reason, note, built_with}`。確かめていなければ `compatible` は null で、`reason` は `no manifest` か `no [pdk]`。固定していない行では null) |
| `pdk` | `{requested, locked, commit, installed, state, changed, verified_by, ok}`: project.json の PDK、lock の id と commit 全体(format 2)、ここで見つかった id、そして `state`。`state` は `none`、`missing`、`not locked`、`other version`、`not pinned`、`mismatch`(project.json と lock が食い違う)、`error`(木のハッシュのない固定)、`unchecked`(前回のハッシュ以降にファイルが変わった: `pdk verify` を実行)、`modified`、`unverified`(固定と一致。初回を信頼)、`verified`(PDK プロファイルがハッシュを公開するまでは出ない)のどれか。`changed`: 部分ごとに `added`、`removed`、`modified` のファイル。状態は PDK そのものを読まず、ストアに保ったハッシュだけを読む |
| `tools` | `[{name, locked, now, now_path, ok}]`。`locked` と `now` はそれぞれ `{version, from, text?}` か null(入っていない、または記録がない): バージョン番号、実行ファイルの出どころ(`pixi`、`UNDINE_TOOLS`、`undine-tools`、`env`、`PATH`。format 1 の lock では null)、番号を読み取れなかったときのバナー |
| `issues` | `ok` を false にするもの: ロックどおりでない依存関係、PDK がない・変更されている・project.json の指定と違う、undine.lock がない、外からリンクしたライブラリ。PDK に合わないパッケージ、パッケージのセルと同じ名前のプロジェクトのセル、パッケージが必要とするものをインストールしない lock、キーでない `trusted` の項目 |
| `warnings` | 見ておくべきだが問題ではないもの: ツールのバージョンや出どころの違い、Undine のバージョンの違い、ツールが固定されていない(`pixi.lock` の記録なし) |
| `notes` | PDK の未検証・未確認・未固定(format 1)、Undine の別の commit や別の `pixi.lock`、format 1 では出どころを記録していないこと。信頼の規則が拒否するものを持つ信頼されていない依存関係、PDK との互換性を確かめていないこと、`built_with` と別のコミットで使っているパッケージ、通常の依存関係のセルと同じ名前のプロジェクトのセル、ほかのライブラリを隠す project.json の名前 |
| `ok` | 問題がない |

依存関係の呼び出しは、changeset のプレビューでは断ります。1 つのプロジェクトでは 1 つずつ実行し、それぞれ前の呼び出しが残した project.json から始めます。
取得(git)の間はほかのリクエストを止めず、サーバーのロックを取るのは、インストールしたファイルを入れ替えて project.json と undine.lock を書くときだけです。
取得したものはユーザーの[ストア](data.ja.md#the-store)に入り、どのプロジェクトとも共有して、ハードリンクでインストールします。format 2 の lock では
`add`、`update`、`lock` がエントリを `tree` で、format 1 の lock では `sha` で固定します。
この 2 つのファイルを書く Undine のもの(サーバーのリクエスト、CLI の `lib` コマンド、changeset の採用と取り消し)はロックファイル
`.undine/project.lock` を取るので、互いの書き込みを上書きすることはありません。プロジェクトで実行する git のコマンド(checkout、pull、merge)は
このロックを取りません。`lib` コマンドや依存関係の呼び出しが動いていないときに実行してください。

<a id="dependency-refusals"></a>
### 依存関係の拒否

拒否は HTTP 400 で、すべきことを書いた `detail` を返します:

- **読めない undine.lock。** `/deps` の呼び出しと `POST /pdk` はどれも、何かを変える前に lock を読み、より新しい形式のもの
  (`undine.lock has format 3, made by a newer Undine (this one reads formats 1 and 2): update Undine ...`)、どの Undine も
  書かない `format` を持つもの、正しい JSON でないものや JSON のオブジェクトでないものを拒否します。プロジェクトは何も変わりません。
  `GET /api/p/{project}` はそれでもプロジェクトを開きます。
- **project.json の指定を固定していない lock。** `/deps/sync` は、依存関係ごとに示して(`analog_ip: project.json asks for rev v2,
  undine.lock locks v1`、または `not in undine.lock`)、`lib lock` と `lib update` のコマンドを伝えて拒否します。`/deps/lock` が
  それを解決します。
- **オフラインで、ストアにない。** `offline`(または `UNDINE_OFFLINE=1`)の `/deps/sync` は、固定した依存関係がストアにも残した
  リポジトリにもなければ拒否します: `offline, and not in the store: analog_ip @ 229914698b1f (sha e6d28176...): run the sync with
  the network ...`。
- **`--dep-root` の外のローカルリポジトリ。** `POST /deps`、`/deps/sync`、`/deps/lock`、`/deps/{name}/update` は、`file://`、絶対パス、`~/...` の
  `git` を、その実際のパス(シンボリックリンクと `..` を解決したもの)と git ディレクトリが `serve --dep-root` で指定したディレクトリの
  下になければ拒否します。メッセージは `--dep-root` と、同じことをコマンドラインで行う `python -m undine lib ...` のコマンドを示します。
  `localhost` 以外のホスト、クエリ、フラグメントを持つ `file://` の URL は `not a plain file:// URL (file:///path/to/repo)` として
  拒否します。リモートの URL(`https://`、`ssh://`、`git://`、`git@host:path`)には関係しません。

[パッケージ](../guide/libraries.ja.md#packages)についての拒否には、それぞれのコードがあります。`detail` はメッセージ全体で、
どれも何も変えません:

| コード | HTTP | いつ | 追加の値 |
|---|---|---|---|
| `resolution_refused` | 409 | `POST /deps`、`/deps/lock`、`/deps/{name}/update`: 要求が解決しない(1 つのパッケージの 2 つのバージョン、合わないバージョン、2 つのパッケージに 1 つのローカル名、予約された名前、セルの名前の重なり、PDK に合わないパッケージ、誤りのあるマニフェスト、インデックスの要求、リモートのパッケージのローカル URL) | `problems: [{code, message, chains}]`。`code` は `conflict`、`version`、`local_name`、`reserved_name`、`cell_clash`、`pdk`、`manifest`、`index_dependency`、`local_url`、`package_mismatch`、`no_manifest` のどれか |
| `needs_lock_format_2` | 409 | format 1 の lock で、依存関係を持つパッケージや別のローカル名のパッケージ。lock を変換する呼び出しはない: `lib lock PROJECT --format 2` | |
| `not_direct` | 400 | パッケージだけが配置する名前の `/deps/{name}/update` や `/remove` | `placed_by: [...]` |
| `pdk_incompatible` | 409 | 固定済みのパッケージが受け入れない PDK への `POST /pdk` | `packages: [{name, package, version, chain, reason, family, variants, commits, compat, built_with}]` |
| `not_synced` | 409 | 固定済みのパッケージがストアにもプロジェクトのインストールにもない(クローンした直後): 先に `lib sync` | |
| `untrusted` | 403 | 人が信頼していない依存関係の内容を実行するジョブ([下記](#trusting-a-dependency)) | `name`、`package`、`key`、`cell`、`findings` |

<a id="trusting-a-dependency"></a>
### 依存関係を信頼する

シミュレーション(`POST /api/p/{project}/sim`。エージェントのスイープと仕様の確認も)、LVS の実行(`/lvs`、`/netgen-lvs`。始める
とき)、RTL のリントとシミュレーション(`.../c/{cell}/rtl/lint`、`/rtl-sim`)、Xschem のネットリスタでの netlist ビュー
(`GET .../c/{cell}/netlist?backend=xschem`)は、プロジェクトが信頼していない依存関係の中で
[信頼の規則](../guide/libraries.ja.md#trusting-a-dependencys-code)が拒否するものを使うとき、コード `untrusted` の **403** で拒否
されます。ボディは `name`(依存関係のローカル名)、`package`(マニフェストがなければ null)、`key`(`analog_ip@1.2.0` または
`ip@229914698b1f`)、`cell`(`<local>/<cell>`、RTL と Tcl では `<local>/<file>`)、`findings: [{file, kind, line, text}]` を持ちます。
いくつもの依存関係を一度に拒否したときは、これらは最初のものを示し、`findings` にはすべてが入ります。`kind` は `control`、
`control_line`、`path`、`unparsed`、`rtl_c`、`systemc`、`rtl_system`、`dpi`、`rtl_path`、`rtl_define`、`tcl` のどれかです。
`backend=undine` の netlist ビューは拒否されず、`warnings` に `not trusted: would be refused in a simulation: ...` が加わります。

人はブラウザの信頼のダイアログで依存関係を信頼します:

| 呼び出し | |
|---|---|
| `POST /api/confirmations` `{kind: "trust", project, name}` | 人のクリックの後で: `{token, expires, key, tree, findings}`。トークンは、プロジェクト、ローカル名、キー、インストールしたファイルのハッシュ(`tree`)に結び付く。`name` で何もインストールされていなければ 404、changeset のプレビューや記録のないインストールなら 400 |
| `POST /api/p/{project}/trust` `{name, token}` | トークンを使い切り、キーを project.json の `trusted` に加える: `{key, trusted: true}`。トークンがない、未知・使用済み・期限切れ、別の種類・プロジェクト・名前のもの、確認の後にインストールされたものが変わった(`... changed since it was confirmed ...: confirm the trust again`)ときは 403。changeset のプレビューでは 400 |
| `POST /api/p/{project}/trust` `{name, untrust: true}` | トークンなしで信頼を取り消す: `{key, trusted: false}` |

一覧と確認の見つかったものは `{content, cell, file, where, kind, line, text}` です: `content` は `spice`、`rtl`、`tcl`。`cell` は
`<local>/<cell>`(RTL では null)。`file` は `<local>/<path>`。`where` はテキストがあるファイル、インスタンス、属性。PDK の取得と
同じく、MCP のツール、`undine call` のコマンド、Python のクライアントのメソッド、エージェントのツールのどれも、`trust` の確認を
求めたり何かを信頼したりしません。

<a id="pdk-fetch"></a>
## PDK の取得

サーバーにない PDK は ciel で入れます。入れるのは、人がブラウザの **PDK を取得** のダイアログで確かめた後だけです
([プロジェクトの PDK を取得する](../guide/pdks.ja.md#fetching-a-projects-pdk)):

| 呼び出し | |
|---|---|
| `GET /api/p/{project}/pdk/fetch-plan?allow_network=false` | 取得で動かすもの: `{family, variant, commit, libraries, source, root}`。`libraries` は ciel の既定のセットなら null、`source` は ciel の取得元、`root` は ciel が入れる PDK のルート(サーバーが探すルートのうち、`ciel/` があるか、まだないか、空の最初のもの)。commit は undine.lock の固定から、なければ 12 文字の id を、入っている PDK から、または `allow_network=true` なら `ciel ls-remote` で広げて得る。読むだけ |
| `POST /api/confirmations` `{kind: "pdk", project, commit, variant, libraries, allow_network?}` | その取得だけに使えるトークン: `{token, expires, plan}`。リクエストはサーバーがいま作る計画と合わなければならない(`commit` は先頭 7 文字以上でよい。`libraries` は ciel の既定のセットなら null)。合わなければ 400(`... show the fetch dialog again`)。トークンは 1 回だけ使え、5 分で切れ(`expires`、Unix 時刻)、プロジェクトと計画の全体に結び付く。もう 1 つの種類は `trust`([依存関係を信頼する](#trusting-a-dependency))で、それ以外は 400。changeset のプレビューと、ciel の既定でない取得元は拒否する |
| `POST /api/p/{project}/pdk/fetch` `{token}` | トークンを使い切って、`ciel fetch` をジョブとして始める: `{job}`(下記)。トークンがない、知らない、使用済み、期限切れ、別のプロジェクトのものなら 403。changeset のプレビュー、または確かめた後に計画が変わった(undine.lock か project.json が変わった: 確かめ直す)ときは 400。1 つの PDK の取得は一度に 1 つで、2 つ目は `queued` で待つ |
| `GET /api/p/{project}/pdk/fetch/{job}` | ジョブ: `{id, project, state, log, result, error, created, ended}`。`state` は `queued`、`running`、`done`、`failed` のどれか。`log` は ciel の出力の最後の 200 行。`result` は終わった後の `{path, verify}` で、`verify` は undine.lock と照らした PDK(`{state, changed, missing_libraries, extra_libraries, warnings}`。`state` は `pdk verify` と同じ)。`error` は失敗した理由。サーバーが知らないジョブなら 404 |

ジョブは ciel が動く間、サーバーのロックもプロジェクトのロックも持たず、プロジェクトには(undine.lock も含めて)何も書かず、ciel が
終わると PDK を探し直すので、プロジェクトは再起動なしに PDK を見つけます。ジョブはサーバーのメモリの中にしかなく、再起動で失われます
(ciel の実行はサーバーとともに終わるか、最後まで動きます。入れたものは、取得し直すか探し直せば見つかります)。終わったジョブは最後の
50 個を残します。

MCP のツール、`undine call` のコマンド、Python クライアントのメソッド、エージェントのツールは、どれも確認のトークンを求めず、取得も
始めません。それをするのは人がクリックした後のダイアログだけです。Undine にはまだ認証がないので、これが止めるのは Undine のツールを
通して動くエージェントと、うっかりした操作です。人として動くプロセスへの守りではありません。

<a id="changesets"></a>
## changeset

changeset は、プロジェクトの git ブランチ `undine/<project>/<id>` と、その作業コピーであるプロジェクト `{project}~{id}` です(元の設計、
つまり読み取り専用でチェックアウトした起点のコミットは `{project}~{id}~base`)。[変更セット](../guide/changesets.md)と、設計の
[git ブランチ上の changeset](../design/changeset-branches.md) を参照してください。これらの呼び出しの `{project}` にはプレビューの名前も
使えます(開く場合を除く)。その場合も実際のプロジェクトに対して働きます。

| 呼び出し | |
|---|---|
| `POST /api/p/{project}/changesets` | 開きます(`{"title", "reason"}`、`X-Undine-Actor`)。記録を返し、プロジェクトがどの git リポジトリにも入っておらず `git-setup` でリポジトリにしたときは `"git_setup": true` が付き、`"first_commit"` はそのときに作ったプロジェクトの最初のコミットです(そのときのファイル。保存していないレイアウトの編集は起点と同じくログのジャーナルのまま含み、Undine 自身の出力と changeset の記録は含みません。作らなかったとき、つまり人のコミットが先にできたときは null)。これで採用したものがコミットされます。`"first_commit_note"` はその後にすること(git のインデックスがそのコミットに合っていない: `git reset` を実行する)、または作らなかった理由で、なければ null です。Undine 用に設定されていないリポジトリにあるプロジェクトでは 400 の `not_set_up`(下記)です。プロジェクト自身のライブラリが worktree の持てないものであるときは 400 の `unsupported_library`(下記)です: ライブラリのフォルダ、その上、その中、または途中のリンクの先にある、プロジェクトの外か存在しない場所を指すシンボリックリンク、文字列が絶対パスかプロジェクトの外へ上がるシンボリックリンク、リポジトリが保存しないシンボリックリンク(またはそのリンク先)、それ自身が git リポジトリであるもの(クローン、サブモジュール)。これは何かを設定する前に確かめるので、拒否された場合はリポジトリを作りません([設計](../design/changeset-branches.md#the-repository)) |
| `GET /api/p/{project}/changesets` | changeset の一覧: `id`、`title`、`reason`、`actor`、`created`、`status`、`preview`、`git`。採用・破棄・取り消しの処理中のものには `closing`(下記)が付きます。新しい順(`created`、同じなら id の順)です |
| `GET /api/p/{project}/changesets/{id}` | 記録(下記)。ライブラリごとの変更(`changes`: 操作。Undo と Redo は差し引き済み)、鮮度付きのチェック(`verify`)、開いている間は書き込み中の主体(`writer`)とビューごとの最後の変更(`activity`)も付きます。閉じた後は、保存した変更とチェック、`retained`、今のサイズ付きの証拠(`evidence.size`)を返します。閉じる処理の間は `closing` が付きます |
| `GET /api/p/{project}/changesets/evidence` | 閉じた changeset の証拠の今のサイズ: `{"bytes", "raw"(そのうち波形の削除で消える分), "changesets": {id: {"bytes", "raw", "files", "shared", "raw_shared", "pruned"}}}`(下の `size`) |
| `GET /api/p/{project}/changesets/{id}/evidence[?check=true]` | 閉じた changeset の証拠: `{"path", "captured", "commits"?, "size", "bytes"(保存時), "linked", "pruned"(マニフェストの記録), "jobs"(それぞれに `"pruned"`: 削除したファイル)}`。`check=true` は保存した全ファイルをハッシュと照合します: `"check": {"ok": 数, "pruned", "missing", "changed": [証拠の中のパス]}`(削除したファイルは `missing` ではなく `pruned`)。開いている changeset には 400 |
| `POST /api/p/{project}/changesets/{id}/evidence/prune` | 閉じた changeset の保存した実行から波形(`*.raw`、`*.vcd`、`*.fst`)を削除します。ステータス、ログ、結果、マニフェストのハッシュは残し、削除したことをマニフェストと記録に書きます(`X-Undine-Actor`): `{"pruned": {"t", "by", "files", "bytes", "freed"}, "size"}`(`"freed"`: そのバイト数のうち、他の名前が持っていなかった分。閉じる処理が残した作業コピーがある場合、残りはその作業コピーを削除したときに解放されます)。人が行う操作です: エージェントのアクター(`agent:...`)には 403、開いている changeset には 400、閉じる処理や取り消しの間は 409 の `busy` |
| `GET /api/p/{project}/changesets/{id}/diff[?lib=L&cell=C]` | ブランチの[意味的な差分](#git-history)。形は `/git/diff` と同じです。開いている間は起点から作業コピーまで(操作として記録されていない変更も含む。採用でマージされる内容)、閉じた後は閉じたときの先端までです。以前の changeset(`git` なし)には 400 |
| `POST /api/p/{project}/changesets/{id}/verify[?fresh=true]` | 作業コピーと元の設計で、必要な検証を実行します(先に作業コピーをブランチにコミットします): KLayout のデッキと、PDK プロファイルが指定すれば(sky130)Magic の DRC(`checker`)、LVS、その ERC(`kind: "erc"`、`findings` 付きの警告)。同じ入力で実行済みのジョブは再利用します(`reused`)。`fresh` なら新しく実行します |
| `POST /api/p/{project}/changesets/{id}/apply` | ブランチをプロジェクトにマージします(全か無か)。`"applied"`(下記)付きの記録を返します。衝突は 409 の `merge_conflict`(下記)で、何も書き込まず、changeset は開いたままです。以前の changeset(新しい changeset として作り直します)、書き込み中の主体がいるとき、その changeset の中断したマージがまだ終わっていないとき(下記)は 400。changeset は一度に一つずつ閉じます: 別の changeset の採用・破棄・取り消しの間は待ちます(その `closing` は `waiting`)。この changeset を閉じる処理がすでに動いているときは 409 の `busy` です(破棄と取り消しも同じ) |
| `POST /api/p/{project}/changesets/{id}/revert` | 採用した changeset を逆向きのマージで取り消します(追加したファイルは残します)。`"reverted"` 付きの記録を返します。変えたものへの後からの編集は 409 の `merge_conflict` です。changeset が git ブランチになる前に採用したものと、中断したマージがまだ終わっていないときは 400 |
| `POST /api/p/{project}/changesets/{id}/discard` | 証拠を保存し、worktree を削除し、ブランチを残します。`"discarded"` 付きの記録を返します(`X-Undine-Actor`)。中断したマージがまだ終わっていないときは 400 |
| `GET .../changesets/{id}/timeline`、`POST .../restore`、`GET .../activity`、`POST .../writer/stop` | タイムライン、チェックポイントの復元、書き込み中の主体の追跡、その停止([ライブ changeset](../design/live-changesets.md)) |

```text
record       {"id", "project", "title", "reason", "actor", "created", "status": open | applied | discarded | reverted,
              "preview": "{project}~{id}", "baseline": "{project}~{id}~base", "libs": [lib],
              "git": Git (changeset が git ブランチになる前の changeset にはない: 確認と破棄だけ),
              "verify": [check], "evidence"?: Evidence, "cleanup_warning"?: text, "closing"?: Closing,
              "interrupted"?: text (止まったサーバーが残したマージを元に戻した: その注記。cleanup_warning にも入る。
              採用、取り消し、破棄、復旧が成功すると消える),
              "retained": 閉じていて、閉じる処理が残した作業コピー (後から来た編集) がまだあり、開ける,
              "applied": Merge | null, "reverted"?: Merge, "discarded"?: {"t", "by"}}
              ("base": {lib: {}} はライブラリで、古い読み手のために残します)
Evidence     {"path", "captured", "jobs": 実行の数, "bytes": 保存時のサイズ,
              "pruned"?: {"t", "by", "files", "bytes"} (波形を削除した: 最後の削除と、全部のファイル数とバイト数),
              "size"?: {"bytes", "raw", "files", "shared", "raw_shared"} (閉じた changeset の GET: 今のサイズ。
              "raw": 波形。"shared"、"raw_shared": 全体と波形のうち、他の名前(閉じる処理が残した作業コピー)が
              まだ持っているファイルの分。証拠から削除しても、その作業コピーを削除するまで解放されません)}
Closing      {"action": apply | discard | revert, "by", "since",
              "phase": waiting | committing | evidence | merging | cleanup,
              "behind"?: {"id", "title", "action"} | null (waiting: 処理中の changeset。null: 別の操作),
              "files"?, "files_total"?, "bytes"?, "bytes_total"? (evidence: 済んだ量と全体)}
              (メモリ上のもので、このサーバーが閉じている間だけあります。GET /api/p/{project}/live にも出ます)
Git          {"branch": "undine/<project>/<id>", "base": sha (開いたときのプロジェクトのファイル),
              "head": sha | null (そのときのプロジェクトの HEAD), "prefix": リポジトリの中でのプロジェクトのディレクトリ,
              "base_seq": {lib: 起点でのログの最後の seq}, "tip": sha | null (ブランチの最後のコミット。null: まだない),
              "ref": "refs/undine/changesets/<project>/<id>" | null (閉じた後に残したブランチ),
              "commits": 起点から先端までのコミット数}
Merge        {"t", "by", "commit": sha | null (ユーザーのブランチ上のマージコミット、または取り消しのコミット。null: コミットしていない),
              "uncommitted": マージを作業ツリーに残した, "why": その理由 ("no_commit": リポジトリにまだコミットがない。
              POST .../git/first-commit で作れます。"changes": プロジェクトにほかの未コミットの変更があった。"failed": コミットに
              失敗した、またはその前に Undine が止まった。コミットしたときは null、以前の記録にはありません),
              "paths": [書いたファイル], "tip": 採用したコミット (apply),
              "kept": [残した、changeset が追加したファイル] (revert)}
409          {"detail", "code": "merge_conflict", "params": {},
              "files": [{"path" (プロジェクトのディレクトリから), "kind": schematic | symbol | layout | netlist |
                         intent | log | text, "conflicts": [Conflict]}]}
400          {"detail", "code": "not_set_up", "top": リポジトリの最上位 (パス), "project": プロジェクトの名前,
              "command": セットアップするコマンド行, "params": {"top", "project", "command"}}
              (Undine 用に設定されていないリポジトリで changeset を開こうとした。"command" は
              `undine git-setup <project>`。コマンドの既定ではないストアでは先頭に `--root <root>` を付ける)
400          {"detail", "code": "unsupported_library", "library", "path": リンクまたはリポジトリ (プロジェクトの
              ディレクトリから), "kind": link_out | link_nowhere | own_repository, "target": リンクの文字列 |
              null (リポジトリ), "params": {"library", "path", "kind", "target"}}
              (worktree が持てないライブラリのあるプロジェクトで changeset を開こうとした。link_out: リンクが
              プロジェクトの外を指す、文字列が絶対パス、またはプロジェクトの外へ上がる。link_nowhere: 何もない場所を
              指す、またはリポジトリがリンクやリンク先を保存しない。own_repository: クローンやサブモジュール。
              detail はどうすればよいかを示す)
```

`Conflict` は `/git/verify`(下記)と同じ `{"id", "what", "base", "ours", "theirs", "taken", "resolved"}` です。採用では `ours` が今の設計、
`theirs` が提案、`base` が提案を開いたときの値です。取り消しでは `theirs` が採用前の値、`base` が採用時の値です。ファイル全体の衝突の
id は `file`(片方で変更し、もう片方で削除した: `what` は「deleted on ours, changed on the other side」。片方をそのビューとして読めない:
`what` は「the <kind> file cannot be read (<エラー>)」で、たとえば YAML が壊れた設計の意図。両側は null。ファイルがあった場所が
ディレクトリになっている: `what` は「not a file in the project」)、`lines`(ビューでないファイルを行単位でマージした)、`error`
(マージに失敗した: `what` は「cannot merge: ...」。読めない側やバイナリのファイル)、`changed`(マージの最中に、それを待たなかったもの、
たとえば別のプロセスがプロジェクトでファイルを変えた: 何も書き込まず、そのプロセスが書いたものは残す。もう一度採用または取り消しをすると、
その編集とマージされます)で、値として両側の状態(`changed`、`deleted`、`added`)を持ちます。ファイルをまたぐ検査も衝突になります:
その後に変わったシンボルのトランザクションのメンバー(`symbol:<id>`)、その後に配置されたシンボルを使う回路図
(`symbol-consumer:<lib>/<cell>`)、提案のログに残った保存していないレイアウトの編集
(`layout-journal`)。`what` は英語で、レビューはこれらの id と状態から、UI の言語で自分で文を作ります(`file` の衝突は `what` の形から
文を作り、読めないファイルにはそのエラーを添えます。それ以外の `what` はそのまま表示します)。

採用と取り消しは、プロジェクトのファイルを書く前に `<project>/changesets/<id>/merging.json` を書き、記録が applied(reverted)になったら
消します。その間にサーバーが止まるとこれが残ります。`undine serve` は起動時に、netlist の監視役と API より先にそれを片付けます。次にその
changeset を読むリクエスト(または起動後に初めてプロジェクトの changeset を一覧にするとき)も同じです。すべて書き終えていればマージを
終えます(コミットは、Undine がこのマージのために作ったものだけを見つけます。なければマージを未コミットとして記録します)。そうでなければ、
その後に行われたもの(ログに追加された記録、マージがまだ手を付けていなかったファイル。マージが書いた後に変わったファイルはそのまま)を
残してファイルを元に戻し、changeset をそのままにして、`interrupted` に注記を入れます。それまでは、採用、取り消し、破棄は 400 です。

<a id="git-history"></a>
## git の履歴

git で管理しているプロジェクト([git でのバージョン管理](data.md#version-control-with-git)を参照)は、履歴をセル単位で見られます。
パスはプロジェクトのディレクトリからの相対パスです。プロジェクトがリポジトリのサブディレクトリでも同じです(`prefix`)。
ただし `/git/verify` は例外で、`undine merge-check --json` の出力をそのまま返すので、パスは git や `undine resolve` と同じく
リポジトリの最上位からです(ほかの呼び出しのパスと比べるときは、前に `prefix` を付けます)。
ライブラリのディレクトリ直下のファイルはセルのビューです: `<cell>.sch` 回路図、`.sym` シンボル、`.oas` レイアウト、
`.netlist.yaml` ネットリスト、`.intent.yaml` 設計意図、`.sv` / `.v` RTL。それ以外のファイル(`project.json`、`undine.lock`、
`history/<lib>.jsonl` など)は「その他」(other)で、状態と差分にはパスの順に最初の 500 個までを載せます。Undine 自身の出力
(`runs/`、`tapeout/`、`llm/`、`.undine/`、`*.pre-split`)は、リポジトリが無視しているかどうかにかかわらず、載せません。
状態は `added`(追加)、`deleted`(削除)、`modified`(変更)、`untracked`(未追跡。無視するファイルは除く)、`conflicted`(競合)で、
名前を変えたファイルは削除と追加になります。changeset の作業コピー(`{project}~{changeset}`)はそのブランチの worktree で、ここでは
自分の履歴を持ちません。状態は changeset を示し、差分は [changeset の差分](#changesets)を使います。log、diff、verify、setup は 400 です。

| 呼び出し | |
|---|---|
| `GET /api/p/{project}/git` | 状態: 作業ツリーとインデックスの HEAD からの変更(セルごと)、ブランチ、HEAD のコミット、進行中のマージ、このクローンで `git-setup` が設定したもの、セルのファイルに保存していない編集がある開いているレイアウト(`unsaved`。そのログのコミットは pre-commit フックが断ります。セルが空のライブラリは、編集が打ち消し合っているものです。フックはこれも断ります)、そうした編集があるあいだにセルのファイルがディスク上で変わった開いているレイアウト(`stale`。git の checkout、merge、pull など。編集を捨てるまで保存を断ります。下の `reload` を参照)。リポジトリの外なら `{"repo": false, ...}`(`setup` と `prefix` は null、`stale` 以外のリストは空)で、エラーにはしません。changeset の作業コピーでは `"preview": true` と `"changeset"`、`"branch"`、`"base"`、それ自身の `unsaved`、`stale`、`reads` が付きます |
| `POST /api/p/{project}/git/setup` | [`undine git-setup`](cli.md) と同じ: プロジェクトがリポジトリの外なら `git init`、属性、差分とマージのドライバ、pre-commit フック。`git init` をしたときは、続けてプロジェクトの最初のコミットも作ります(そのときのファイル。Undine 自身の出力と changeset の記録は含まず、author は `user`。採用した changeset は、コミットのあるブランチにだけコミットされるためです)。状態に `"hook_note"` を付けて返します。pre-commit フックを入れなかった理由と、そこにあるフックに加える行です(リポジトリに独自の pre-commit フックがある、または `core.hooksPath` が設定されている場合。`git-setup` はそこには書き込みません)。入れたときは null です。`"init"` は `git init` をしたかどうか、`"gitignore_added"` はプロジェクトの `.gitignore` に加えた Undine 自身の出力の行です(状態の `setup.gitignore` が足りない行を示します。これ以外に加えるものはありません)。`"commit"` は最初のコミットです(null: `git init` をしなかった、または拒否した)。`"commit_refused"` は拒否した理由で、`first-commit` が答えるのと同じ形です(`{"code": "unsaved", "detail", "logs"}` または `{"code": "unsupported_library", "detail", "library", "path", "kind", "target"}`。なければ null)。`"index_note"` は git のインデックスをそのコミットに合わせられなかったことで、なければ null です。changeset の作業コピーには 400 |
| `POST /api/p/{project}/git/first-commit` | まだコミットのないリポジトリ(手で作った、または以前の Undine が設定した)で、プロジェクトの最初のコミットを作ります: そのときのファイルを、インデックスが追跡しているプロジェクトの項目から。Undine 自身の出力と changeset の記録は含まず(ステージされていれば追跡しない状態に戻します)、author は `user` です。プロジェクトがリポジトリのサブディレクトリなら、プロジェクトのディレクトリだけです。状態に `"commit"` と `"index_note"`(別の git がインデックスを使っていた: コミットは作ったので `git reset` を実行する。なければ null)を付けて返します。ブランチにすでにコミットがあれば(その間に作られた場合も) 409 の `has_commits`、セルファイルに保存されていないレイアウトの編集をログが持つあいだは、pre-commit フックがそのコミットを拒否するのと同じく 409 の `unsaved`(`"logs"`: `[{"path", "lib", "cells"}]`)、プロジェクトがどのリポジトリにも入っていなければ 400、Undine 用に設定されていないリポジトリでは 400 の `not_set_up`(下記)、changeset の worktree が持てないライブラリがあれば 400 の `unsupported_library`(下記)です。履歴パネルの **最初のコミットを作成** のためのもので、エージェントのツールはありません |
| `GET /api/p/{project}/git/log?limit=50&skip=0[&lib=L&cell=C]` | HEAD のコミットのうちプロジェクトを変えたもの(`lib` と `cell` を付けるとそのセルのファイルを変えたもの)を新しい順に。それぞれ最初の親から変えたセルが付きます。`limit` は 1〜500、`more` は次のページがあるかどうか |
| `GET /api/p/{project}/git/diff?rev=REV[&lib=L&cell=C]` | コミット `REV` の最初の親からの[意味的な差分](../design/merge.md)(ルートコミットは空からの差分)。`rev=worktree` なら作業ツリーの HEAD からの差分。セルとビューごと |
| `GET /api/p/{project}/git/verify[?rev=REV]` | マージコミットの `undine merge-check --commit REV --json`: DRC / LVS で確かめるセルとその理由、警告、注記、ここにレポートがないままマージされたファイル。マージではないコミットには 400。`rev` を省くと進行中のマージ、チェリーピック、リベースの `undine merge-check --json`: 未解決の競合(それぞれ base、ours、theirs の値)も付きます(進行中のものがなければ 400)。パスはリポジトリの最上位から |
| `POST /api/p/{project}/l/{lib}/reload` | ライブラリの保存していないレイアウトの編集を捨て、セルのファイルを読み直します。パネルの **編集を破棄して読み直す** が `stale` のライブラリに対して行うことです: `{"version", "dirty", "result": {"dropped": 保存していない編集があったか}, "undo"}`。操作ログは捨てた編集を履歴として残し、その後ろに `"args": {"reload": true}` を付けたレイアウトの `save` の記録を書きます。そのため、捨てた編集が再び適用されることはなく、Undo もそこで止まります。それまでは、`stale` のライブラリの `POST /api/p/{project}/l/{lib}/save` は 400 です |

リポジトリでない、リビジョンが不明、親が 1 つのコミットや進行中のマージがないときのマージチェックはいずれも 400、プロジェクトがなければ 404 です。
`project.json` にマージの競合マーカーが残っている間も、状態、log、diff の呼び出しは使えます。そのときライブラリは HEAD の `project.json` から読みます。

```text
Commit       {"sha", "short", "parents": [sha], "subject", "body", "author", "email", "date" (ISO 8601),
              "refs": [ブランチとタグの名前]}
CellChange   {"lib", "cell", "views": [{"view": schematic | symbol | layout | netlist | intent | rtl,
                                        "path", "status"}]}
状態         {"repo", "preview", "setup": {"merge", "diff", "hook", "gitignore"} | null, "prefix": "" | "designs/prj/" | null,
              "branch": 名前 | null (detached HEAD), "head": Commit | null (まだコミットがない),
              "operation": null | "merge" | "cherry-pick" | "rebase", "conflicted": [path],
              "cells": [CellChange], "other": [{"path", "status"}], "unsaved": [{"lib", "cells": [cell]}],
              "stale": [{"lib", "cells": [cell]}], "reads": {lib: n}}
              (reads: 開いているレイアウトが、未保存の編集がないときにディスク上の変化を受けてセルのファイルを読み直した回数。
              増えたら、そのライブラリを表示しているエディタが描き直します)
              (setup と prefix は、リポジトリの外と changeset の作業コピーでは null。setup.gitignore: プロジェクトの
              .gitignore に足りない Undine 自身の出力の行。加えるのは git-setup だけ)
setup        状態 + {"hook_note": 文 | null, "gitignore_added": [行]}
log          {"commits": [Commit + {"cells": [CellChange], "other": その他のファイルの数}], "more"}
diff         {"base": sha | null, "rev": sha | null (作業ツリー),
              "cells": [CellChange。各ビューに "diff" も], "other": [{"path", "status"}]}
verify       {"ours": sha, "theirs": sha | null (theirs に当たるコミットが分からない: git merge --squash、cherry-pick -n),
              "commit": sha | null (進行中), "parents": [sha] | null (3 つ以上: octopus マージ),
              "operation": null (コミット) | "merge" | "cherry-pick" | "rebase",
              "unmerged": [{"path", "kind", "conflicts": [Conflict] | null (git 自身の競合マーカー), "error"?}],
              "warnings": [{"path", "warning"}], "merged": [path], "no_report": [path],
              "verify": [{"lib", "cell", "why", "reason"}], "notes": [文],
              "note_codes": [{"code": no_theirs | octopus | earlier, "operation"?: rebase | cherry-pick}]}
Conflict     {"id", "what", "base", "ours", "theirs", "taken": ours | theirs (ファイルにいま入っている側), "resolved"}
reason       {"kind": "merged"} (セル自身のファイルをマージした)
             | {"kind": "side", "side", "below": [{"lib", "cell", "side": side | null (マージしたもの)}]}
               (ファイルはその側のものだが、下のセルがその側と違う)
             | {"kind": "resolves", "side"} (配置しているものが別のセルを指すようになった)
             side: "ours" | "theirs" | "parent N" (octopus マージの N 番目の親)
```

ビューの `diff` は、そのファイルについて `undine diff --json` が出力するものです: `{"kind", "changes": [{"do": add | del | mod,
"id", "what", "before", "after"}]}`。レイアウトには `"geometry": [{"cell", "layer", "xor_um2", "geometry_unchanged"}]`
(変わったレイヤーごとの XOR の面積)と `"note"`(その XOR が何を比べたか: 各セル自身の図形で、配置したセルは展開しない)も付き、
RTL は `{"kind": "text", "do"}`、どちらかの版が読めないときは `{"kind", "error"}` です。`id` はマージと同じ名前でオブジェクトを指します
(`inst:M2:prop:W`、`wire:<端点>`、`shape:<cell>:<layer>`、`item:<id>` など)。`what` は `undine diff` が出力する英語で、
パネルは知っている形の `id` から、UI の言語で自分で文を作ります。同じく `verify` の `why` と `notes` は merge-check の英語で、
`reason` と `note_codes` が同じ内容をデータとして持ちます。
