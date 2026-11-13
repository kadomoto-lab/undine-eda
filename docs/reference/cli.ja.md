# コマンドラインと設定

<a id="command-line"></a>
## コマンドライン

| コマンド | |
|---|---|
| `python -m undine serve [--host 127.0.0.1] [--port 8700] [--llm-policy ...] [--mcp] [--import-root DIR] [--dep-root DIR] [--stop-jobs]` | サーバーを起動。`--import-root`(複数指定可、既定はホーム)は、UI が GDS / OASIS を取り込める場所を制限します。`--dep-root`(複数指定可、既定はなし)は、ブラウザと HTTP API がローカルのリポジトリ(`file://`、`/path`、`~/path`)から[ライブラリの依存関係](../guide/libraries.ja.md)を追加、更新、同期できるディレクトリを指定します。指定しなければローカルのリポジトリはすべて拒否します(`lib` コマンドはどれでも受け付けます)。再起動しても作業は失われません([サーバーの再起動](#restarting-the-server)): シミュレーションは走り続け、次のサーバーが引き継ぎます。`--stop-jobs` を付けると、このサーバーが止まるときにシミュレーションも止めます |
| `python -m undine new PROJECT [--pdk sky130A]` | PDK に固定したプロジェクトを作成(名前だけなら、Undine が PCell を動かせる版を選ぶ。テストした版が先)。undine.lock も作る(`created undine.lock (format 1)`) |
| `python -m undine set-pdk PROJECT PDK` | プロジェクトを別の PDK バージョンに固定し直す。例: `sky130A@8afc8346a57f`(`pdk_history` と undine.lock に記録。undine.lock がなければ作る。プロジェクトがすでに持つバージョンを指定すると `already ...` と表示し、undine.lock の固定がないか別のものであるときだけ書く。[プロジェクトを別の PDK バージョンへ移す](../guide/pdks.ja.md#moving-a-project-to-another-pdk-version)を参照)。format 2 の lock は format 2 のままで、新しい PDK の固定(commit 全体と木のハッシュ。PDK を読んで作るので、初めてなら数秒かかる)を得る。すでに固定している PDK なら、固定はそのまま。固定済みの[パッケージ](../guide/libraries.ja.md#pdk-compatibility)が新しい PDK を受け入れないときは、何も変えずに拒否する: `error: the PDK ... is not accepted by N locked packages:` に続いて、パッケージごとにその経路と理由を 1 行ずつ(終了コード 1)。ストアにもインストールにもない固定済みのパッケージは `run lib sync first` として拒否する。プロジェクトがすでに指定している PDK なら拒否しない |
| `python -m undine import PROJECT LIB FILE`(`.gds` または `.oas`) | レイアウトを取り込む(ライブラリがなければ作成) |
| `python -m undine pdk list`(`pdks` でも同じ) | 見つかった PDK の一覧。commit 全体、どこから来たか(`ciel`、`volare`、素のディレクトリなら `-`)、ディレクトリ、入っているライブラリを示す([PDK の確認](../guide/pdks.ja.md#checking-your-pdks)を参照) |
| `python -m undine pdk verify PROJECT [--pin] [--network]` | 入っている PDK のハッシュを取り(初回は数秒。その後はスタンプの変わったファイルだけ)、undine.lock の固定と比べる。違うものを部分とファイルごとに示し、最後に `PDK <状態>`(`verified`、`unverified`、`modified`、`missing`、`not pinned`、`other version`)。PDK が変更されている、ない、別のバージョンである、または固定したライブラリが入っていなければ終了コード 1。`--pin` なしでは undine.lock を書かない。`--pin` は、ここに入っているとおりに PDK を固定し(commit 全体、ciel の取得元、ライブラリ、部分ごとの木のハッシュ)、undine.lock を format 2 で書く(ほかのエントリはそのまま)。format 2 の lock では固定し直し、前の固定に対してどうだったかを示す。新しい固定が合わない固定済みのパッケージは `! CHAIN: does not fit the PDK ...` の行になり、拒否にはならない(終了コードは PDK のもの)。`--network`: commit がここに入っていない 12 文字の id を `ciel ls-remote` で調べてよい。[PDK をファイルで固定する](../guide/pdks.ja.md#pinning-the-pdk-by-its-files)を参照 |
| `python -m undine pdk fetch PROJECT [--yes] [--network]` | プロジェクトの PDK を ciel で入れる。ファミリー、variant、commit、ライブラリ、ciel の取得元、入れる先のルートを示して尋ね(`--yes` なら尋ねない。答えがないか `y` でなければ `not fetched`、終了コード 1)、`ciel fetch` を動かし、PDK を lock と比べる。ルートは、探す PDK のルートのうち ciel が入れられる最初のもの。`~/.volare` しかなければ拒否する。動いているサーバーは **PDK を再検出**(または `POST /api/pdks/rescan`)の後に見つける。[プロジェクトの PDK を取得する](../guide/pdks.ja.md#fetching-a-projects-pdk)を参照 |
| `python -m undine store verify [--dry-run]` | [ストア](data.ja.md#the-store)のすべてのファイルと木の記録をハッシュし直し、名前と合わないものを消し(`--dry-run` なら消すものを示すだけ)、書き込めるオブジェクトを読み取り専用に戻す。何か問題があれば終了コード 1。消したものを使っていたプロジェクトでは `lib sync` を実行する。入っているファイルから取り込み直すか、取得する |
| `python -m undine store gc [--dry-run]` | ここのどのプロジェクトも固定も install もしていない、ストアの木の記録とファイルを消す。プロジェクトは、`--root` の下のもの、その changeset の作業コピー、ほかの場所で依存のコマンドを実行したもの(ストアが一覧を持つ)。`removed N tree records and M objects, <大きさ>` と表示する(`--dry-run` なら `would remove`)。読めない lock があれば、何も消す前に止まる(`nothing was removed: make these locks readable (or remove them), then run store gc again`、終了コード 1)。残したリポジトリ、PDK の記録、コンパイルしたモデルは残す |
| `python -m undine doctor` | 前提条件をチェック(`pixi run doctor`) |
| `python -m undine lib add PROJECT NAME GIT [--rev REV] [--subdir DIR] [--package P]` | [ライブラリの依存関係](../guide/libraries.ja.md)を追加し、undine.lock に固定: `NAME: COMMIT12 (N cells), locked in undine.lock`。[パッケージ](../guide/libraries.ja.md#packages)では `NAME: P VERSION at COMMIT12 (N cells, M internal), locked in undine.lock`、別のローカル名なら `  installed as NAME and P`、連れてきたパッケージごとに `  + P VERSION as NAMES, via REQUIRER`、[信頼の規則](../guide/libraries.ja.md#trusting-a-dependencys-code)が拒否するものを持つものごとに `~ LABEL: WHAT (not trusted): a simulation using it needs python -m undine lib trust PROJECT NAME` と表示する。`--package P`: リポジトリのマニフェストがパッケージ `P` を名乗っていなければならない(project.json に書く)。すべてを先に確かめ、すべての問題を示して一度に拒否し(`N problems with the dependencies:` と、問題ごとに `- ` の行)、何も変えない。依存関係を持つパッケージや別の名前のパッケージには lock の format 2 が必要(`needs lock format 2: run python -m undine lib lock PROJECT --format 2 first`)。新しい名前は、プロジェクトや PDK のライブラリ、`devices`、Xschem のライブラリや `symbol_paths` のフォルダの名前にはできない |
| `python -m undine lib update PROJECT NAME [--rev REV]` | 依存関係を別のバージョンに移す。変わったファイルと、それを使うセルを表示し、パッケージについては `  + P VERSION as NAMES`(連れてきた)、`  P: OLD @ C12 -> NEW @ C12`(動いた)、`  - P VERSION as NAMES: no longer needed` を表示。動いたものの信頼を project.json から取り除く。パッケージだけが配置している名前は拒否する: `NAME: placed by X; update or remove that instead` |
| `python -m undine lib remove PROJECT NAME` | 依存関係を、もう何も必要としないパッケージと一緒に削除: それぞれ `removed P VERSION (installed as NAMES)` か `removed NAME (git C12)`。ほかの依存関係がまだ配置しているパッケージは残す(`NAME: still placed by X`)。それらの信頼を project.json から取り除く。パッケージだけが配置している名前は update と同じく拒否する |
| `python -m undine lib trust PROJECT NAME [--untrust] [--yes]` | `NAME` としてインストールされた依存関係の中で[信頼の規則](../guide/libraries.ja.md#trusting-a-dependencys-code)が見つけるものを表示し(`NAME: P VERSION, trust key P@VERSION`、続いて見つかったものごとに、ファイル、属性、行、種類、テキストを 1 行)、`trust P@VERSION in PROJECT? [y/N] ` と尋ねて、project.json の `trusted` にキーを加える(`P@VERSION: trusted in PROJECT (...)`)。`--yes`: 尋ねない。端末がなく `--yes` もないとき、または `y` 以外の答えでは、何も信頼しない(終了コード 1)。`--untrust`: 尋ねずに信頼を取り消す。マニフェストのない依存関係のキーは `NAME@COMMIT12`。インストールされていない名前や、記録のないインストール(Undine が入れたものではない: `lib sync`)は拒否する。エージェントのツールや `undine call` のコマンドからは届かない |
| `python -m undine lib sync PROJECT [--offline]` | 依存関係をロックどおりにインストールし、project.json がもう指定しないものを削除(その `.undine/deps/<lib>/` と undine.lock のエントリ。`<lib>: removed <commit>` と表示)。全部か無か: 1 つでも失敗すると(リモートに届かない、ファイルがロックと合わない)、どれもインストールせず、どれも削除しない。各パッケージは lock が与えるすべてのローカル名でインストールし、固定した PDK に合わないものも `~ CHAIN: does not fit the PDK ...` の行を付けてインストールする。固定どおり: 何も解決し直さず、project.json の指定どおりに依存関係を固定していない lock は、何かを読む前に拒否する(project.json を変えた pull の後なら `lib lock` を実行)。ストアにある依存関係はそこから、ネットワークなしで入れる。`--offline`(または `UNDINE_OFFLINE=1`)では取得しない: ストアにも残したリポジトリにもないものを示し、何も変えない |
| `python -m undine lib lock PROJECT [--format 2]` | project.json の指定どおりに undine.lock が固定していないものを固定し(取得してインストールする。`<lib>: <commit> (sha ...)` と表示し、パッケージなら `, P VERSION` を付け、連れてきたパッケージは `  + P VERSION as NAMES, via REQUIRER`)、今使っている PDK とツールのバージョンを記録。パッケージの依存関係を lock がインストールしないエントリも解決し直す。`--format 2` は、その後に lock を format 2 で書く: PDK を木のハッシュで固定し(`pdk verify --pin` と同じ)、ツールを出どころと一緒に記録し、エントリはそのまま(次に `lib update` か `lib lock` が書くときに `tree` を得る)。format 2 の lock では PDK の固定に触れない。解決するものに format 2 が必要なとき(パッケージ自身の依存関係、別の名前のパッケージ)、`--format 2` は lock を変換して一度の書き込みで解決し、全部か無か: 拒否されれば format 1 の lock はそのまま。`--format 2` がなければそのヒントを示して拒否する。固定する PDK を受け入れないパッケージは拒否ではなく `! CHAIN: does not fit the PDK ...` の行 |
| `python -m undine lib status PROJECT` | ここで undine.lock と食い違うもの: PDK の行(その状態。`pdk verify` と同じ名前)、依存関係、続いて `! 問題`、`~ 警告`、`note:` の行、問題がなければ `as locked`。終了コード 1 は問題があるときだけ: ツールのバージョンや出どころの違い、Undine のバージョンの違い、ツールが固定されていないこと(`pixi.lock` の記録なし)は `~` の警告で、ここで見つかった実行ファイルと一緒に示す。PDK の未検証・未確認、Undine の別の commit、別の `pixi.lock` は注記。project.json がもう指定しないのに、まだインストールかロックされている依存関係は `leftover`。ローカル名ごとに 1 行: 依存関係の行の最後にパッケージとバージョン(別のローカル名なら `-> P VERSION`)と `trusted` か `not trusted`。別のパッケージが連れてきたパッケージは次の行に `via CHAIN`、別の名前で直接加えたパッケージの自身の名前の行は次の行に `= NAME`(同じ install) |
| `python -m undine git-setup PROJECT` | プロジェクトを、レイアウトのセルをテキストで差分表示し([git でのバージョン管理](data.md#version-control-with-git))、セルのファイル(`.sch`、`.sym`、`.oas`、`.netlist.yaml`、`.intent.yaml`)と操作ログをオブジェクト単位でマージする git リポジトリにする([意味的な差分とマージ](../design/merge.md))。criss-cross マージのマージベースも同じようにマージする(`merge-driver --virtual-base`)。保存していないレイアウトの編集を断る pre-commit フック(`precommit`)も入れる(リポジトリ独自のフックがあるとき、`core.hooksPath` が設定されているときは入れず、加えるべきコマンドを表示する)。以前に設定したプロジェクトも、もう一度実行すれば更新される(自分で入れたフックでは `undine precommit` を実行する行だけを置き換え、フックに加えた行は残す。`.gitignore` に足りない Undine 自身の出力の行(あとから加わった `/changesets/requests/` など)は末尾に加えて表示し、ほかの行はそのまま残す)。クローンごとに実行する。何もコミットせず、リポジトリにまだコミットがないときはそう表示する(採用した changeset は、コミットのあるブランチにだけコミットされる。プロジェクトのファイルをコミットするか、履歴パネルの **最初のコミットを作成** を使う。履歴パネルの **git をセットアップ** は、`git init` をしたときにそのコミットも作る) |
| `python -m undine textconv FILE.oas` | レイアウトファイルを並べ替えたテキストとして出力(`git-setup` が登録する差分ドライバ) |
| `python -m undine merge-driver [--virtual-base] BASE OURS THEIRS PATH` | `git-setup` が登録するマージドライバ(`python -P -m undine merge-driver %O %A %B %P` として。`-P` のない Python 3.10 では、今のディレクトリをモジュールの探索パスから外す前置きを付けた `python -c '<前置き>' merge-driver ...` として。git はこれをリポジトリの最上位のディレクトリで実行するが、そこにあるファイルが読み込まれることはない)。両方のブランチが変えたファイルに対して git が実行する。マージした結果を `OURS` に書き、次に報告を `.git/undine-merge/PATH.json` に書き(報告はそのクローンの中にだけある)、マージした内容か、残った衝突(1 つだけなら、ファイルがそれをどうしたかを言うその衝突自身の説明)と次に実行するコマンドを 1 行で表示する。衝突が残るとき(そのときファイルは衝突ごとに ours の値を持つ)と、マージに失敗したとき(ファイルは ours のままで、報告にエラーが入る)は終了コード 1。`--virtual-base` を付けると(`merge.undine.recursive` が指す `merge.undine-base.driver` として登録される)、criss-cross マージの複数のマージベースを 1 つにマージするために git が実行するドライバになる: きれいにマージできればマージした結果を、そうでなければ(衝突が残る、またはエラー)マージベースに共通のベース `BASE` をそのまま(ないときは空)書く。こうしてマージ自体が両方のブランチの変更を見て、一方のブランチの解決を取るのではなく衝突で止まる。終了コードはどちらでも 0 で、報告は書かず、ベースを取ったときだけ 1 行表示する |
| `python -m undine resolve PATH [--theirs ID ...] [--ours ID ...] [--all-theirs] [--all-ours]` | 衝突したファイルを git のインデックスからもう一度マージし、指定した衝突は指定した側を取る(`--all-theirs` / `--all-ours` は残っている衝突すべて)。決めた内容は実行のたびに積み重なる。`PATH` は今のディレクトリからでも、ドライバが表示するとおりリポジトリの最上位からでもよい(`:/PATH` は最上位から)。解決済みにするのは `git add :/PATH`。衝突が残る間は終了コード 1 |
| `python -m undine merge-check [DIR] [--commit C] [--json]` | プロジェクトのディレクトリ `DIR`(既定は今のディレクトリ)で、マージ、cherry-pick、rebase の途中、またはマージコミット `C` について: まだ残っている衝突、警告、DRC / LVS で確認すべきセル(階層がどちらのブランチにもなかった形になったセル)。警告はドライバの報告から取り、報告はマージしたクローンの中にだけある。マージコミットでは、まとめたファイルのうちここに報告のないものを示す(その警告は分からない)。octopus マージ(`git merge A B`)では、すべてのブランチをそれぞれ一方の側として扱う。octopus マージはドライバを実行しないので、コミットには報告がなく、衝突で止まっている間は `resolve` でマージしたファイルにだけ報告がある。そのようなファイルが行単位でマージされたことも伝える。rebase や複数のコミットの cherry-pick の途中では、git が止まったコミットだけを扱い、それより前に適用したコミットがあればそう伝える(その警告はマージしたときに表示済み)。theirs にあたるコミットが残らない `git merge --squash`、`git cherry-pick -n`、`git stash pop` の後は、衝突したファイルとその衝突・警告を示し、確認すべきセルは示さない。衝突したファイルがある間は終了コード 1 |
| `python -m undine precommit` | `git-setup` が入れる pre-commit フック。インデックスの操作ログ(ステージした `history/<lib>.jsonl`)に、セルのファイルへ保存していないレイアウトの編集があれば、ライブラリとセルを示してコミットを断る(終了コード 1)。Undine でレイアウトを保存してファイルをステージし直すか、ジャーナルのままコミットするなら `--no-verify` を付ける([理由](data.md#save-layouts-before-committing))。マージや cherry-pick を締めくくるコミットでは、それを締めくくる間に加えた保存していない編集(どちらのブランチのログにもないもの)だけを断る。ブランチの編集のうちどれを保留にしておくかは、マージが決めている。`git merge --squash`(相手のブランチは `SQUASH_MSG` が最初に挙げるコミット)と `git cherry-pick -n` でも同じで、ここでマージがコピーしてまだコミットしていないジャーナルの記録(`merged_seq` があり、HEAD のログにないもの)は断らない |
| `python -m undine diff OLD NEW [--kind K] [--json]` | 2 つのファイルの意味的な差分。マージと同じ同一性を使う(ビューはファイル名から、または `--kind schematic`、`symbol`、`layout`、`netlist`、`intent`、`log`) |
| `python -m undine diff --rev A [B] [--dir DIR] [--json]` | リビジョン `A` から `B`(既定は作業ツリー。git が無視しない、まだ追跡していないファイルも含む)までに変わった、プロジェクトの各ファイルの意味的な差分。レイアウトではレイヤごとの XOR の面積も示す |
| `python -m undine mcp-stdio [--url URL] [--ui-url URL] [--ssh HOST] [--no-start] [--no-open]` | デスクトップの AI アプリ向けのローカル MCP の入口。サーバーを起動または再利用し、レビューを開く([設計エージェントと MCP](../guide/assistant.md#desktop-clients-the-local-launcher))。`--ssh` では `HOST` 上のサーバーに SSH のポート転送でつなぐ([Undine が別のマシンにある場合](../guide/assistant.md#undine-on-another-machine)) |
| `python -m undine mcpb [--out FILE]` | Claude Desktop の拡張を作る(`pixi run mcpb`。`dist/` に出力) |

共通のオプション: `--root DIR`(プロジェクトのルート。既定は `$UNDINE_ROOT` または `~/undine-projects`)と `--pdk-root DIR`(複数指定可。
既定の PDK のルート `$PDK_ROOT`、`~/.ciel`、`~/.volare` の代わりに使います。[Undine が PDK を探す場所](../guide/pdks.ja.md#where-undine-looks-for-pdks)を参照)。

`lib` コマンド、`pdk verify`、`pdk fetch`、`set-pdk` は、何かを変える前に undine.lock を読みます。読めない lock は 1 行(`undine: ...`、
終了コード 1)で拒否し、project.json、undine.lock、`.undine/deps/` はそのまま残します: より新しい形式の lock(`undine.lock has format 3,
made by a newer Undine (this one reads formats 1 and 2) ...`。Undine を更新してください)、または正しい JSON でないか、どの Undine も
書かない `format` を持つ lock(直すか、`git checkout undine.lock`)。`format` のない lock は format 1 として読みます。
[トラブルシューティング](../troubleshooting.ja.md#undinelock-has-format-3-made-by-a-newer-undine)を参照してください。

**lock の形式。** 今のコマンドは format 1 で書き、format 2 の lock は format 2 のまま書きます。lock を format 2 にするのは
`pdk verify PROJECT --pin` と `lib lock PROJECT --format 2` の 2 つだけです([undine.lock](../guide/libraries.ja.md#undinelock))。
undine.lock を作ったコマンドは `created undine.lock (format N)` と表示します。依存関係を示す[パッケージ](../guide/libraries.ja.md#packages)や、
自身の名前以外で追加するパッケージには format 2 が必要です: format 1 の lock では、`lib add`、`lib update`、`lib lock` はそれを
`needs lock format 2: run python -m undine lib lock PROJECT --format 2 first; nothing was changed` として拒否します。

<a id="restarting-the-server"></a>
## サーバーの再起動

Undine を再起動しても(変更を反映するための `./scripts/restart.sh`、Ctrl+C して起動し直す、`kill -9` でも)作業は失われません。
サーバーの端末や SSH のセッションを閉じたとき(SIGHUP)も、Ctrl+C や SIGTERM と同じように止まります。

- **走っているシミュレーションは続きます。** ngspice は自分のセッション(プロセスグループ)で走るので、サーバーと一緒には止まりません(サーバーの
  端末での Ctrl+C も届きません)。同じプロジェクトルートで次に起動したサーバーが、ngspice がまだ走っている実行を引き継ぎます。
  その実行は `UNDINE_SIM_JOBS` の枠を 1 つ使い、時間の上限は開始時から数え、`cancel_job` で(それが起動したものと一緒に)止められ、終われば通常の実行と同じく
  仕上げられます(ログ、raw ファイル、測定値を読み、`done` か `failed`。`wait_for job:<id>` などで待っている側にも伝わります)。
  サーバーが動いていない間に ngspice が終わった実行は、次に読まれたときに結果を回収します。引き継ぎには Linux(`/proc`)が
  必要です。それ以外の環境では、これまでどおりサーバーの ngspice はサーバーが止まるときに一緒に終わります(`kill -9` の後は終わらず、
  結果を回収するサーバーもありません)。
- **待っているシミュレーションも順番どおり続きます**(`<root>/sim-queue.jsonl`)。
- **実行中の呼び出しは切られません。** サーバーに停止が求められると、待つ呼び出し(`wait_for`、`run_simulation` の `wait_s`、
  `check_specs`、`sweep`、`run_checks`、`extract_parasitics`)はその時点の内容ですぐに答えます(`wait_for` は `"stopping": true` と、
  実行が続くのか `--stop-jobs` で止められたのかを書いた note。待っていた DRC、LVS、抽出、RTL、LibreLane のジョブはサーバーと一緒に
  終わり、`ended` に入ります)。そのためサーバーは数秒で止まります。止まった後でもう一度待ち、終わったものは実行し直してください。
  `run_checks` と `extract_parasitics` も `"stopping": true` を返します。これらのチェックはサーバーと一緒に終わる(止まる間は新しく
  始めない)ので、サーバーが戻ってからもう一度呼んでください。
- **どのサーバーも終わらせられない実行**(ngspice が強制終了された、異常終了した、マシンが再起動した)は、理由とともに
  `interrupted` になります。実行し直してください。止まったサーバーが終えられなかった DRC、LVS、抽出、RTL、LibreLane の実行も
  同じです。引き継ぐのはシミュレーションだけです。それ以外の実行は、サーバーに停止が求められた時点で終わらせます(すぐに
  `interrupted` になるので、もう応答している次のサーバーは、そのチェックを使い回さずに新しく実行します)。新しい実行も始めません
  (サーバーが止まるところだと答えます。戻ってから実行し直してください)。強制終了されたサーバー
  (`kill -9`、異常終了)はそれができないので、次のサーバーがその実行を見た時点で残っているツールを止めます(実行のステータスに
  ツールのプロセスを記録しています)。`cancel_job` でも止められます。
- **エージェントのセッションは保たれます。** MCP のセッションは再起動の後も、同じ変更セット、同じ書き込みロックの持ち主、自分の
  ジョブのまま続きます(`<root>/mcp-sessions.json`。30 日使われなかったセッションは忘れます)。人が止めたエージェントは止まったまま
  です。サーバーがセッションをもう知らないときは、シェルと Python のクライアントが新しいセッションを始め、開いていた変更セットを
  そこで続けます。サーバーの再起動中に断られた呼び出し(接続拒否、プロキシの 503)は、これらのクライアントと `mcp-stdio` が
  `UNDINE_RESTART_WAIT_S`(120 秒)まで同じリクエスト ID で送り直すので、書き込みが二重になることはありません。`--ssh` でも
  同じです(転送が接続を受けてから閉じるので、二度送っても害のない呼び出しは、転送越しにサーバーが応答するまで待ちます)。
  サーバーが止まっている間に実行した `undine call` も、その `--session` にそのサーバーとのセッションがあれば戻るまで待ちます
  (新しいセッション名なら、Undine が動いていないとすぐに伝えます)。1 つの `--session` を共有するエージェント(それぞれ
  `agent=NAME`)は、セッションファイルの中で互いの変更セットを消し合いません。名前付きの `--session` はその名前のエージェントです。
  この版より前に(名前なしで)変更セットを開いたセッションは、次の呼び出しでその名前のまま続け、書き込みのロックも引き継ぎます。
  これは一度だけで、そのセッションが後で `agent=""` で開いた変更セットは、名前なしのエージェントのものです。
  このバージョンより前の Undine からの最初の再起動では、変更セットの中でシミュレーションがまだ走っているセッションを、そのジョブから、その変更セットを開いた状態で
  復元します。それ以外のエージェントは変更セットを開き直してください(`open_changeset id=...`。`no job` の答えには、その
  ジョブを持つ変更セットが書かれます)。

1 つのプロジェクトルートのシミュレーションを扱い、セッションを保存するのは一度に 1 つのサーバーです(`<root>/sim-jobs.lock` を
持つサーバー)。その間に同じルートで起動した 2 つ目のサーバー(前のサーバーがまだ止まりきっていないときの次のサーバー、または
余分なサーバー)は自分の実行を走らせ(`<root>/sim-queue-<id>.jsonl` に記録)、ほかの実行は 1 つ目がいなくなってから引き継ぎます。
2 つ目が先に止まった場合は、残した実行を 1 つ目が数秒のうちに引き継ぎます。止まるサーバーは、ちょうど順番が来た実行を先に
始めさせてから手放すので、次のサーバーが同じ実行を二度始めることはありません。このバージョンより前の Undine が始めた実行はプロセスを記録していないので、
ディレクトリとコマンドラインで見つけ、そのサーバーがいなくなってから引き継ぎます(それまではそのサーバー自身が仕上げます)。
そのようなサーバーで待っていた実行は `interrupted` になります。

シミュレーションも止めるには、サーバーを `--stop-jobs` 付きで起動します。止まるときに、待っている実行は取り消され、走っている
ngspice はそれが起動したものと一緒に終了させます。1 つの実行は `cancel_job` か、シミュレーションパネルの **キャンセル** で止めます。サーバーが動いていない間に
走り続けているシミュレーションは、サーバーが引き継ぐまで時間の上限がかかりません。

<a id="configuration-environment"></a>
## 設定(環境変数)

| 変数 | 既定値 | 用途 |
|---|---|---|
| `UNDINE_ROOT` | `~/undine-projects` | プロジェクトのルート(サンプルのスクリプトも読みます) |
| `PDK_ROOT` | | 追加の PDK のルート。`~/.ciel` と `~/.volare` より先に探します。`ciel` のインストール先でもあります([PDK](../guide/pdks.ja.md#where-undine-looks-for-pdks)) |
| `UNDINE_TOOLS` | `pixi run` の中と、pixi 環境の `mcp-stdio` が起動したサーバーでは pixi 環境。未設定ならサーバー自身の環境(pixi、conda、venv)、次に `~/undine-tools` | ngspice、Xschem、Netgen、Magic を `PATH` より先に探す場所(`bin/`) |
| `UNDINE_PCELL_PYTHON` | `.pixi/envs/pcell/bin/python` | sky130 と IHP の PCell 生成を動かす Python(kfactory ベースの gdsfactory 8.x) |
| `UNDINE_PCELL_GF180_PYTHON` | `.pixi/envs/pcell-gf180/bin/python` | GF180MCU の PCell 生成を動かす Python(gdsfactory 7.9) |
| `ANTHROPIC_API_KEY`(または `ANTHROPIC_AUTH_TOKEN`) | | クラウドのエージェント |
| `UNDINE_LLM_SERVER_URL` / `UNDINE_LLM_SERVER_KEY` | `http://127.0.0.1:11434/v1`(Ollama) | EDA-server のエージェント |
| `UNDINE_LLM_POLICY` | `both` | `--llm-policy` と同じ |
| `UNDINE_MCP` | オフ | `--mcp` と同じ |
| `UNDINE_ENV`、`UNDINE_LOG`、`PORT` | `~/.undine/env`、`/tmp/undine.log`、`8700` | `scripts/restart.sh`: 読み込む秘密情報のファイル、ログファイル、ポート |
| `UNDINE_AGENT_MAX_WRITES` | なし | エージェントが 1 つの changeset で行える編集の上限(任意) |
| `UNDINE_SIM_JOBS` | コア数から 2 を引いた数 | サーバーが同時に走らせるシミュレーション(ngspice)の数。それ以外は順番に待ちます(`queued`、待ち順は `queue_position`) |
| `UNDINE_SIM_THREADS` | `1` | シミュレーション 1 つあたりの ngspice のスレッド数(`num_threads`、`OMP_NUM_THREADS`)。`0` で ngspice 自身の設定のまま(8 に設定したビルドがあり、共有のマシンを遅くします) |
| `UNDINE_SIM_FULL_MODELS` | 無効 | `1` にすると、シミュレーションは PDK のモデルライブラリを、ネットリストが使うものだけに縮小したコピーではなく全体で読みます([PDK のモデル](../guide/verification.md#pdk-models-in-a-simulation)) |
| `UNDINE_CACHE` | `$XDG_CACHE_HOME/undine`(`~/.cache/undine`) | 縮小したモデルライブラリ(`models/`)の置き場所。ライブラリのファイルが変わると作り直し、30 日使われなければ消します。コンパイルした Verilog-A モデルにはもう使いません。それはストア(`build/`。`UNDINE_STORE` を参照)に置きます。以前の Undine がここの `osdi/<PDK の id>/` にコンパイルしたモデルは、`./setup.sh osdi` がストアに移すまで、ここでも見つけます |
| `UNDINE_SIM_TIMEOUT`、`UNDINE_SIM_TIMEOUT_MAX` | `3600`、`43200` | 呼び出し側が `time_limit_s` を指定しないときにシミュレーションが走れる秒数と、指定できる最大の秒数。超えると ngspice を止めます |
| `UNDINE_LIBRELANE` | PATH 上の `librelane`、なければ `nix-shell ~/librelane --run` | LibreLane の実行方法。実行ファイル、または `--run` で終わるラッパー(例: `nix-shell /path/to/librelane --run`) |
| `UNDINE_HOME` | `~/.undine` | ユーザーごとのファイル: UI で追加した接続(`connections.json`、キーを含む)、起動役が起動したサーバーのログ、[ストア](data.ja.md#the-store) |
| `UNDINE_STORE` | `$UNDINE_HOME/store` | ストアだけの場所: 依存関係のファイル、残した git リポジトリ、PDK のハッシュ、コンパイルした Verilog-A モデル([ストア](data.ja.md#the-store)) |
| `UNDINE_STORE_LINK` | `link` | `copy`: 依存関係をストアへのハードリンクではなく読み取り専用のコピーで入れる |
| `UNDINE_OFFLINE` | オフ | `1`: `lib sync` と HTTP API の sync は取得しない(`lib sync --offline` と同じ) |
| `UNDINE_CIEL` | Undine の Python の隣の `ciel`、なければ `PATH` | `pdk fetch` が動かす ciel(ファイルでないパスなら、どれも動かさない) |
| `UNDINE_FETCH_MAX` | `1073741824`(1 GiB) | 1 つの依存関係のファイルの合計の上限(バイト。ファイルは 100 000 個まで) |
| `UNDINE_URL`、`UNDINE_UI_URL`、`UNDINE_OPEN` | `http://127.0.0.1:8700`、サーバー自身、既定のブラウザ | `mcp-stdio`: 使う(または起動する)サーバー、レビューを開く場所、それを開くコマンド(`{url}` が置き換わる) |
| `UNDINE_SSH`、`UNDINE_MCP_TOKEN` | なし、`<プロジェクトのルート>/mcp-token` から読む | `mcp-stdio`: Undine が動くホスト(`--ssh` と同じ)、読む代わりに使うトークン |
| `UNDINE_RESTART_WAIT_S` | `120` | `undine call`、Python クライアント、`mcp-stdio`: サーバーの再起動中に断られた呼び出し(接続拒否、プロキシの 503)を送り直し続ける秒数([サーバーの再起動](#restarting-the-server)) |
| `UNDINE_CLI_SESSIONS` | `~/.undine/cli-sessions.json` | `undine call`、`undine session`: シェルのセッション(サーバーと `--session` の名前ごと)を呼び出しの間で保つファイル。並んで動くエージェントは、それぞれ別に持てます([スクリプトとシェル](../guide/assistant.ja.md#scripts-and-the-shell)) |
| `UNDINE_MAX_CHARS` | `25000` | `undine call`: 表示する結果の文字数。中を縮め、正しい JSON のまま(`--max-chars` と同じ。0 で全部。[長い結果](../guide/assistant.md#long-results)) |
