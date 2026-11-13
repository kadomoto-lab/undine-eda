# データ配置と操作ログ

<a id="data-layout-server-side"></a>
## データの配置(サーバー側)

```
~/undine-projects/                          projects root ($UNDINE_ROOT)
    <project>/project.json                  name, format, pinned PDK, libraries (own and dependencies), symbol paths,
                                            the dependency versions trusted (below)
    <project>/undine.lock                   the commits of the dependencies, PDK and tool versions (see Libraries)
    <project>/.undine/deps/<local name>/    dependencies as installed from undine.lock, one directory per local name:
                                            read-only hard links into the store (below), and .undine-dep.json, what
                                            was installed (below)
    <project>/libs/<lib>/
        <cell>.sch, <cell>.sym              Xschem format (Xschem can open the same directory)
        <cell>.netlist.yaml                 the cell's netlist: its connectivity (design/netlist.md)
        <cell>.oas                          the cell's layout (OASIS), one file per cell
    <project>/history/<lib>.jsonl           operation log (who, what, why) for schematic and layout edits
    <project>/changesets/<id>.json          changeset records
    <project>/changesets/<id>/evidence/    archived proposal changes and verification evidence (on apply/discard)
    <project>/changesets/<id>/merging.json  an apply or revert being written (each file's digest before and after);
                                            left only by a server stopped meanwhile: undine serve finishes it or
                                            takes it back at its start, before anything else writes the project
    <project>/runs/<job>/                   simulation, DRC and LVS jobs (status.json with inputs and output hashes,
                                            netlists, results, logs)
    <project>/tapeout/<target>/             export bundles (+ .zip)
    <project>/llm/                          agent audit log (audit.jsonl), sent pictures, conversations (chats/)
                                            and agent turns with their progress (turns/<id>.json)
    .changesets/<project>/<id>/work         a changeset's working copy: a git worktree of its branch
                                            undine/<project>/<id> (the project <project>~<id>)
    .changesets/<project>/<id>/base         its baseline: the base commit checked out, detached (read-only)
    .changesets/<project>/<id>/tip.json     the branch's last commit while it is open; gseq.floor and the snapshots
                                            of batch and restore are here too, never in the worktree
    .changesets/.gitignore                  "*": the folder ignores itself in any repository the root is kept in
                                            (written when missing, never removed)
    llm.json, mcp-token                     agent connection settings, MCP bearer token
    mcp-sessions.json                       the MCP sessions a restarted server resumes (their changesets, the
                                            stopped agents)
    sim-jobs.lock, sim-queue.jsonl          held by the server whose simulations these are; the ones not started
                                            yet, in order (a restarted server takes both up)
    sim-queue-<id>.jsonl                    a second server's unfinished simulations, while another holds the root
                                            (taken up by the holder once that server has gone)
```

主なもの: `project.json`(名前、形式、固定した PDK、ライブラリ、シンボルの探索パス)、`undine.lock`(依存関係のコミット、PDK とツールのバージョン)、
`libs/<lib>/` の Xschem 形式の回路図・シンボルと、セルごとのレイアウト(OASIS)、`history/<lib>.jsonl` の操作ログ、`changesets/` の changeset の記録と
採用・破棄時の証拠、`runs/` のジョブ、`tapeout/` の提出用バンドル、`llm/` のエージェントの監査ログ・送った画像・会話・ターン、
`.changesets/` の changeset の作業コピー(ブランチの git worktree)と元の設計(起点のコミットのチェックアウト)、そしてエージェントの接続設定(`llm.json`)と MCP の Bearer トークン(`mcp-token`)です。

PDK の Xschem ライブラリ(例: `sky130_fd_pr`)は読み取り専用のライブラリとして表示されます。追加のシンボル探索パス(例: Xschem 自身の
`xschem_library`)は `symbol_paths` に書きます。

<a id="layout-cell-files"></a>
### レイアウトのセルのファイル

セルのファイルには、そのセル自身の図形、インスタンス、メタ情報(生成した PCell なら、その PCell とパラメータ)が入ります。参照しているセルは名前だけで入っており
(KLayout のゴーストセル)、どのセルの形状もちょうど 1 回だけ保存されます。ライブラリのファイルをどの順に全部読んでも、階層全体が得られます。
保存では内容が変わったセルだけを書き込み、削除したセルのファイルは消します。ファイル名はセル名で、`A-Z a-z 0-9 _ . + $ -` 以外の文字は % でエンコードします。
大文字・小文字だけが違うセル名を使うには、大文字と小文字を区別するファイルシステムが必要です(WSL の下の Windows のドライブは不可)。

Undine の外でライブラリのレイアウトを読むには、その `.oas` ファイルをすべて 1 つのレイアウトに読み込みます(KLayout:
`for f in files: layout.read(f)`)。または `undine.layoutstore.read(lib_dir, layout)` を使います。

### プロジェクトの形式

`project.json` の `format` が、この配置のバージョンです。Undine は古いプロジェクトを最初に開いたときに変換します:

| 変換前 | 変換後 | 変わる点 |
|---|---|---|
| 2 | 3 | すべての回路図に netlist ビュー `<cell>.netlist.yaml` ができます([netlist ビュー](../design/netlist.md)) |
| 1(`format` なし) | 2 | 各ライブラリの `layout.oas`(全セル)を、セルごとの `<cell>.oas` に分けます。元のファイルは `layout.oas.pre-split` として残るので、セルが正しく見えることを確認したら削除して構いません |

<a id="version-control-with-git"></a>
### git でのバージョン管理

プロジェクトのファイルは git で管理する前提です。回路図はテキストで、レイアウトのセルは別々のファイルで、保存しても変わっていないセルには触れません。
`python -m undine git-setup PROJECT` は、プロジェクトのディレクトリを git リポジトリにし(すでにリポジトリの中ならそれを使い)、ジョブの結果、提出用バンドル、
エージェントのログ、インストールした依存関係、形式の変換が残す `layout.oas.pre-split`、changeset ごとに保存する証拠(実行結果。ハードリンクで保存)、API のリクエストのログと
再送のために残す結果を無視するよう設定し(`/runs/`、`/tapeout/`、`/llm/`、`/.undine/`、`*.pre-split`、`/changesets/*/evidence/`、`/changesets/requests.jsonl`、
`/changesets/requests/`。
プロジェクトの最上位に限るので、`runs` という名前のライブラリは無視されません。changeset の記録そのものと
[undine.lock](../guide/libraries.md#undinelock) は管理対象)、git のドライバを登録して、フックを 1 つ入れます:

- レイアウトのファイル(`*.oas`、`*.gds`: `diff=undine-layout`)の**差分ドライバ**。`git diff`、`git log -p`、`git show` が、レイアウトのセルを
  並べ替えたテキストとして表示します:

    ```diff
     cell inv  (dbu 0.001 um)
       67/20  box (790,940;1035,1140)
    +  68/20  box (-120,2100;90,2800)
       68/20  box (160,715;390,5405)
       inst sky130_fd_pr__nfet_01v8_w1p0_l0p15_nf1_451f14  r0 *1 645,975
    ```

- セルのファイルと操作ログ(`*.sch`、`*.sym`、`*.oas`、`*.netlist.yaml`、`*.intent.yaml`、`history/*.jsonl`: `merge=undine`)の
  **マージドライバ**。`git merge`(と `git pull`、`git rebase`、`git cherry-pick`)が、これらを行単位ではなくオブジェクト単位でマージします。
  これには**仮想マージベース**のドライバ(`merge.undine.recursive = undine-base`。`merge-driver --virtual-base` を実行します)が伴います:
  互いにマージし合ったブランチにはマージベースが複数あり、git はまずそれらを 1 つにマージします。それらのマージベースが衝突するところ
  (2 つのブランチが 1 つの衝突を違うように解決したところ)では、マージベースに共通のベースを取ります。こうしてマージは両方の解決を変更として見て、
  どちらかを黙って取るのではなく、衝突で止まります。

- **pre-commit フック**(`.git/hooks/pre-commit`。`python -m undine precommit` を実行します)。ステージした操作ログに、セルのファイルへ
  保存していないレイアウトの編集が残っているコミットを断ります([後述](#save-layouts-before-committing))。入れるのは、リポジトリに独自の
  pre-commit フックがなく、`core.hooksPath` も設定されていない場合だけです。そうでなければ `git-setup` はその旨と、そこのフックに加える
  コマンドを表示します(履歴パネルも同じものを示し、HTTP API は `hook_note` として返します)。`git-setup` が入れたフックでは、もう一度実行すると `undine precommit` を実行する行だけを置き換えます。
  フックに加えた行は残ります。

属性はプロジェクトの `.gitattributes` にあってコミットされますが、そこで指定するコマンドとフックは git のローカル設定です(`.git/config` の
`diff.undine-layout.textconv`、`merge.undine.driver`、`merge.undine.recursive`、`merge.undine-base.driver`、`.git/hooks/` のフック。
どれも `git-setup` を実行した Python を使います)。クローンごとに、また Undine にマージドライバや仮想マージベースのドライバができる前に設定した
プロジェクトでも、`git-setup` を実行し直してください。マージドライバがないと、git はこれらのファイルを行単位でマージし、両方のブランチで変えた
レイアウトのセルはバイナリの衝突になります。仮想マージベースのドライバがないと、criss-cross マージのマージベースが違うように解決した衝突は、
何も言われずにどちらかに決まります。git はこれらのコマンドをリポジトリの最上位のディレクトリで実行します。
コマンドは `python -P` で動くので、そこにコミットされた Python のファイルが、Undine の使うモジュールの代わりに読み込まれることはありません
(`-P` のない Python 3.10 では、そのディレクトリをモジュールの探索パスから外す前置きを付けた `python -c` で動きます。`-I` と違って、
ユーザーの site-packages と `PYTHONPATH` はそのまま使えます)。

**ブランチのマージ。** 2 人が別々のセルを編集すると別々のファイルに触れるので、git は各ファイルを、それを変えた側から取ります。
両方のブランチが同じセルを変えたときは、マージドライバがそのファイルをオブジェクト単位でマージします: インスタンスは名前で、そのプロパティは 1 つずつ、
配線、レイアウトの図形、設計意図の項目、操作ログの記録です(規則は[意味的な差分とマージ](../design/merge.md)にあります)。別々のオブジェクトへの変更は
まとまります。両方のブランチが同じオブジェクトを違うように変えたものは衝突です: ファイルにはその ours の値が残り、git はファイルを衝突として扱い、
ドライバが衝突を示します(1 つだけならその衝突自身の説明で。複数なら ID と「ours kept」で):

```
undine: libs/l/inv.sch: schematic, 1 conflict (inst:M2:placement): instance M2: placement; see `undine merge-check`, then `undine resolve libs/l/inv.sch --theirs ID`
```

`python -m undine resolve PATH --theirs ID`(または `--ours ID`、`--all-theirs`、`--all-ours`)は、git のインデックスからファイルをもう一度マージし、
指定した衝突は指定した側を取ります。PATH は、今のディレクトリからでも、ドライバが表示するとおりリポジトリの最上位からでも構いません。
解決済みにするのは、いつもどおり `git add` です(resolve がそのコマンドを表示します)。プロジェクトのディレクトリで `python -m undine merge-check` を実行すると(マージ、cherry-pick、rebase の途中。
マージコミットには `--commit C`)、まだ残っている衝突、警告(たとえば、両方のブランチが同じセルやシンボルのインスタンスを同じ位置に置いたところ。
1 つのデバイスを二重に置いた可能性が高い場所です。両方のブランチで追加した図形が同じレイヤーで接しているところは、警告ではなく衝突です)、
DRC / LVS で確認すべきセル(階層がどちらのブランチにもなかった形になったセル)を示します。衝突のないマージも、
確認済みの設計ではありません。

rebase や、複数のコミットの cherry-pick は、コミットを 1 つずつマージします。`merge-check` が扱うのは git が止まったコミットだけです。
それより前に適用したコミットはその時点で ours の一部になっているので、その警告(ドライバがマージしたときに表示しています)と、
それらのコミットだけがマージしたセルは示されません。そのようなコミットがあるときは、`merge-check` がそう伝えます。
`git merge --squash`、`git cherry-pick -n`、`git stash pop` では、theirs にあたるコミットが残りません。このとき `merge-check` は、
衝突したファイルとその衝突・警告を示し、確認すべきセルは分からないと伝えます。

ドライバの報告は `.git/undine-merge/` に、ファイルのマージ 1 回ごとに 1 つ残ります。後で同じファイルをもう一度マージしても(操作ログではほぼ毎回そうなります)、
前の報告は消えません。報告はマージしたクローンの中にだけ残り、git はこれを転送しません。別のクローンでの
`merge-check --commit C` は、確認すべきセルをコミットだけから計算し、マージでまとめたファイルのうちそこに報告のないものを示します(その警告は分かりません)。

<a id="save-layouts-before-committing"></a>
**コミットの前にレイアウトを保存する。** レイアウトの編集は、レイアウトを保存したときにセルのファイル(`.oas`)に書き込まれます。
それまでは操作ログの中のジャーナルで、Undine はライブラリを開くときにそれを再適用します。両方のブランチからまとめたセルのファイルに
ジャーナルを再適用することはできないので、両方のブランチがライブラリのレイアウトを変えていて、どちらかのログに保存していない編集があると、
ログは衝突(`layout-journal`)になります。それぞれのブランチでレイアウトを保存してコミットし、もう一度マージしてください
(この衝突を解決すると、保存していない編集は失われます)。pre-commit フックはこれを未然に防ぎます: ステージしたログに保存していない
レイアウトの編集があるコミットを、ライブラリとセルを示して断ります:

```
undine: history/l.jsonl: library l has layout edits not saved to its cell files (cells inv)
undine: save the layout in Undine, `git add` the cell files and the log, and commit again (git commit --no-verify ...)
```

`git commit --no-verify` なら、それでもログの中のジャーナルとしてコミットできます。マージや cherry-pick(`git merge --squash` と
`git cherry-pick -n` も含みます)を締めくくるコミットは、
マージが保留にした保存していない編集はそのまま通し(ブランチの編集のうちどれを保留にしておくかは、マージがすでに決めています)、
それを締めくくる間に加えた、どちらのブランチのログにもない編集だけを断ります。

- GitHub の Web 上のマージ(やサーバー上のマージ全般)はドライバを実行しません。これらのファイルを行単位でマージし、両方のブランチが変えた
  レイアウトのセルで止まります。そのようなブランチは手元でマージしてください。
- octopus マージ(`git merge A B` のように複数のブランチを一度にマージする)もドライバを実行しません。複数のブランチで変わった
  ファイルは行単位でマージされます。そのようなブランチは 1 つずつマージしてください。`merge-check --commit C` は、octopus マージが
  まとめたファイルを、報告のないものとして示します。octopus マージが衝突で止まったときは、`merge-check` はすべてのブランチを
  それぞれ一方の側として扱い、ファイルが行単位でマージされたことを伝えます。衝突したファイルは `undine resolve` でオブジェクト単位に
  マージできます。
- 開いているレイアウトのセルのファイル(またはログの中のジャーナル)を git が変えたとき、未保存の編集がなければ、Undine はレイアウトを読み直します。
  未保存の編集があれば、編集を捨てるまでそのレイアウトの保存を断ります。保存すると、git が書いたファイルの上に元のままのセルを書いてしまうためです。
  編集を捨てるには、履歴パネルの **編集を破棄して読み直す** を使います([HTTP API](api.md#git-history) の `reload`)。ログは捨てた編集を履歴として残し、
  その後ろに `"args": {"reload": true}` を付けたレイアウトの `save` の記録を書くので、捨てた編集が再び適用されることはなく、Undo もそこで止まります。
  マージ、rebase、pull、別のブランチのチェックアウトの前に、レイアウトを保存してください。回路図、シンボル、ネットリスト、設計意図は、
  編集のたびにファイルから読みます。ブラウザで開いているテキスト(RTL、YAML で編集する設計意図)はエディタが持っている内容のまま保存されるので、
  git がそのファイルを変えた後は開き直してください。

<a id="changesets-in-git"></a>
**git の中の changeset。** [changeset](../guide/changesets.md) はプロジェクトのリポジトリのブランチなので、changeset を使うプロジェクトは
git で管理されます。どのリポジトリにも入っていないプロジェクトで最初の changeset を開くと `git-setup` を実行してプロジェクトの最初のコミットを作り(下記)、
Undine のマージドライバ(`merge.undine.driver`)のないリポジトリにあるプロジェクトでは開くのを拒否します(400 `not_set_up`。セットアップする
コマンド `undine git-setup <project>` を示します)。プロジェクト自身のライブラリを worktree が持てないときも拒否します(400
`unsupported_library`)。シンボリックリンクはどれも、worktree のコピーが解決するのと同じく、リンクのフォルダから判断します: ライブラリの
フォルダ、その上、その中、または途中のリンクの先にある、プロジェクトの外か存在しない場所を指すリンク、文字列が絶対パスかプロジェクトの
外へ上がるリンク、リポジトリが保存しないリンク(またはそのリンク先。無視されているもの、Undine 自身の出力)、それ自身が git リポジトリで
あるもの(クローン、サブモジュール)です。worktree はリンクを通ってプロジェクトのファイルに書き込むか、そこに何も見つけられないか、
ライブラリを空で持つことになるためです([変更セット](../guide/changesets.md))。これは何かを設定する前に確かめるので、拒否された場合に
プロジェクトがリポジトリになることはありません。changeset がリポジトリに残すもの:

| | |
|---|---|
| 最初のコミット | どのリポジトリにも入っていなかったプロジェクトで、最初の changeset(または履歴パネルの **git をセットアップ**)の `git init` の直後に作ります。コミットのないリポジトリでは、履歴パネルの **最初のコミットを作成** でも作ります。プロジェクトのファイルをそのまま(下の起点のコミットと同じツリーを、インデックスが追跡しているプロジェクトの項目から)、親なしで、ユーザーのブランチに作ります。ブランチはコミットがないときだけそこに作り、プロジェクトのディレクトリのインデックスの項目もそれに合わせます。履歴パネル(とその **git をセットアップ**)からは、保存していないレイアウトの編集をログが持つあいだは作りません(409 `unsaved`: 先にレイアウトを保存します)。changeset を開くときは、そのようなログも起点と同じくジャーナルのまま含めます。メッセージは `Project <project>: its files when Undine set up its repository`(履歴パネルからは `Project <project>: its first commit, its files as they were`)です。コミットがないと、ユーザーのブランチにマージコミットを作れないため、採用は未コミットのまま残ります(`"why": "no_commit"`) |
| 起点のコミット | 開いたときのプロジェクトのファイル(一時的なインデックスを使い、未コミットのファイルや未追跡のファイルも含む。`runs/`、`tapeout/`、`llm/`、`.undine/`、`*.pre-split`、`changesets/` は、リポジトリが無視しているかどうかにかかわらず除く)。親はそのときのユーザーの HEAD、メッセージは `Base of <id>: <changeset title>` |
| `refs/heads/undine/<project>/<id>` | changeset が開いている間のブランチ。起点から始まり、リースの終わり、チェックの前、採用と破棄の前に作業コピーのコミットが加わります(メッセージ: タイトル、`Changeset <id> of <project>: <reason>`、主体、ターン、操作)。コミットは `commit-tree` で作ります。フックは実行せず、署名もしません。author は主体(`user`、`agent:<name>`)、committer は `Undine <undine@localhost>` です |
| 2 つの worktree | `.changesets/<project>/<id>/{work,base}`(上記)。リポジトリに登録されます(`git worktree list`)。`.changesets/` フォルダは自分自身を無視させます(その `.gitignore` は `*`。最初の worktree の前に、なければ書き、消しません)。そのため、プロジェクトのルートを管理しているリポジトリがどれであっても、worktree、`tip.json`、`gseq.floor`、退避は `git status` にも `git add -A` にも出ません。最後の worktree がなくなった後も同じです。プロジェクトのルートがプロジェクト自身のリポジトリの中にあるときは、そこにリポジトリの worktree がある間、`.git/info/exclude` の 1 行(`/<path>/.changesets/`)でもフォルダを除外します。リポジトリのほかの場所にある worktree は、そこでは自分だけを除外します |
| `refs/undine/changesets/<project>/<id>` | changeset を採用または破棄した後に残すブランチ(ブランチ自体は削除します)。証拠と取り消しのためにコミットが残ります。記録のない changeset(開いている途中でサーバーが止まった)のブランチもここに移します。作業コピーをコミットし、worktree を削除します。Undine が changeset のコミットを削除することはありません |
| `Apply <id>: <changeset title>` | 採用でユーザーのブランチに作るマージコミット(親は HEAD と先端)。ブランチにコミットがあり、ほかに未コミットの変更がなかったときだけ作ります(そうでなければ記録の `why` がどちらかを示します: `no_commit`、`changes`。コミットに失敗したときは `failed`)。取り消しのコミットは `Revert <id>: <changeset title>` です。git が無視していて追跡していないパス(プロジェクトの `.gitignore` が `history/` を外していればログ)は入れません |

changeset の記録(`<project>/changesets/`)は無視しません。コミットするかどうかはユーザーが決めます。Undine が作るコミットには入らず、
採用をコミットしない理由になる変更としても数えません。Undine のコミットはリポジトリのフックを実行しないので、pre-commit フックはそれを
検査しません。作業コピーのレイアウトは、コミットの前に毎回保存します。

<a id="dependency-files"></a>
## 依存関係のファイル

プロジェクトの[依存関係](../guide/libraries.ja.md)が何で、どう固定され、どうインストールされるか、そして
[パッケージ](../guide/libraries.ja.md#packages)が自身について何を書くか。

### project.json

`libraries` は、プロジェクトが自分で配置する依存関係を示します: `{"git": URL, "rev": REV, "subdir": DIR}`。`--package` を付けて
追加したものは `"package": NAME`(マニフェストが名乗るべき [package] の名前)も持ちます。それらが依存するパッケージはここには
書かず、lock が持ちます。

`trusted` は、人がこのプロジェクトで[信頼した](../guide/libraries.ja.md#trusting-a-dependencys-code)依存関係のバージョンを、
キーで 1 つずつ並べます: `<package>@<version>`(`analog_ip@1.2.0`)、マニフェストのない依存関係では
`<ローカル名>@<commit12>`(`ip@229914698b1f`)。書くのは `lib trust` とブラウザの信頼のダイアログで、`lib update`、`lib remove`、
`lib lock` は変えたり外したりしたもののキーを取り除きます。何も信頼していなければキーはありません。changeset の作業コピーは、
自身のキーに加えてプロジェクトのキーも数えます。

```json
"libraries": {"analog_ip": {"git": "https://github.com/me/analog_ip.git", "rev": "v1.2.0", "subdir": "lib"},
              "bias": {"git": "https://github.com/me/bias_gen.git", "rev": "v0.3.2", "package": "bias_gen"}},
"trusted": ["analog_ip@1.2.0"]
```

### undine.lock

形式は [undine.lock](../guide/libraries.ja.md#undinelock) にあります。lock の format 2 は、パッケージのために次を加えます:

- `libraries[<ローカル名>]` は、`project.json` が指定する依存関係をそれぞれ保ちます。通常のもの、またはパッケージより前に固定して
  その後解決していないものは、これまでのエントリ(`git`、`rev`、`subdir`、`commit`、`sha` または `tree`)のままです。マニフェスト
  とともに解決したものは、`files: "include"`(その `tree` は [include のファイル集合](../guide/libraries.ja.md#the-include-file-set-and-the-lock)
  を覆います)、`package`、`version`、`names`(インストールされるすべてのローカル名。キーが先頭)、`deps`(`{ローカル名: パッケージ}`。
  マニフェストが示す依存関係)を加えます。
- `packages[<package>]` は、マニフェストを通してだけ届くパッケージを持ちます: `{package, version, git, rev, subdir?, commit, tree,
  files: "include", names, deps}`。兄弟(`{ subdir = ... }`)は、それを求めたパッケージの `git` と `rev` を記録します。
- 1 つのパッケージ名は、2 つの表を通して 1 回だけ現れます。format 1 の lock はこれらのキーを持ちません: そこではパッケージも
  通常のエントリ(すべてのファイル、`sha`)として固定します。

```json
"libraries": {
  "analog_ip": {"git": "https://github.com/me/analog_ip.git", "rev": "v1.2.0", "subdir": "lib",
                "commit": "229914698b1fb16a629bb73109b39eb1aba40eae", "tree": "sha256:5d0c…", "files": "include",
                "package": "analog_ip", "version": "1.2.0", "names": ["analog_ip"],
                "deps": {"sar_common": "sar_common", "bias": "bias_gen"}}
},
"packages": {
  "sar_common": {"package": "sar_common", "version": "1.3.1", "git": "https://github.com/me/sar_common.git",
                 "rev": "v1.3.1", "commit": "5b1c0e2a9d11…", "tree": "sha256:77aa…", "files": "include",
                 "names": ["sar_common"], "deps": {}},
  "bias_gen": {"package": "bias_gen", "version": "0.3.2", "git": "https://github.com/me/bias_gen.git",
               "rev": "v0.3.2", "commit": "0c4e2b7a1f30…", "tree": "sha256:9a7e…", "files": "include",
               "names": ["bias", "bias_gen"], "deps": {}}
}
```

`lib sync` は各エントリを、その `names` のすべてでインストールします。パッケージが必要とするものをインストールしない lock
(`deps` が示すパッケージを lock が固定していない、1 つのローカル名が 2 度与えられている)は、何かを取得する前に拒否します:
`lib lock` を実行してください。

<a id="the-installed-dependencies"></a>
### インストールした依存関係

`.undine/deps/<ローカル名>/`: ローカル名ごとに 1 つのディレクトリで、[ストア](#the-store)への読み取り専用のハードリンクでできて
います(ファイルシステムをまたぐとコピー)。2 つのローカル名のパッケージは、同じファイルの 2 つのディレクトリです。それぞれが
**インストールの印** `.undine-dep.json` を持ちます: インストールのもとになった lock のエントリ、`modes`(`0444` のファイルでは
示せない、実行可能なファイル)、そしてパッケージなら `name`(このディレクトリのローカル名)、`direct`(`project.json` が指定する
か)、`package` と `version`、別のパッケージが連れてきたときは `via`(それを配置するパッケージ)。印のない依存関係のディレクトリは
Undine が入れたものではなく、`lib sync` が削除するか入れ直します。プロジェクトを開くときに `undine.lock` は読みません: ライブラリ
ブラウザは、ほかのパッケージが連れてきたパッケージをこれらの印から知ります。`.undine/` はバージョン管理しません。

### undine-package.toml

パッケージのマニフェストで、そのディレクトリの最上位に置きます: `[package]`(`name`、`version`、そして `description`、`license`、
`authors`、`repository`)、`[pdk]`(`family`、`variants`、`commits`、`compat`、`built_with`)、`[dependencies]`、`[exports]`(`cells`)、
`[files]`(`include`)、`[checks]` と `[renames]`(受け付けるが、まだ使わない)。それぞれの意味と例は[マニフェスト](../guide/libraries.ja.md#the-manifest)
にあります。Undine はこれを 256 KiB 以下の UTF-8 の TOML の通常のファイルとして読み(シンボリックリンクは拒否)、知らないテーブルや
キーを拒否します。

<a id="the-store"></a>
## ストア

依存関係のファイルは、ユーザーごとに 1 つの、内容でアドレスするストアに一度だけ置き、各プロジェクトにはハードリンクで入れます。
PDK のハッシュとコンパイルした Verilog-A モデルもそこに置きます。ストアは、どのプロジェクトのルートにあるものも含め、ユーザーの
すべてのプロジェクトで共有します:

```
~/.undine/                                  per-user files ($UNDINE_HOME): the store, connections.json
    store/                                  the store ($UNDINE_STORE moves it alone)
        objects/sha256/<2 hex>/<62 hex>     every dependency file once, named by the SHA-256 of its bytes; read-only
        trees/sha256/<hex>.json             a dependency's files as a format 2 lock's `tree` names them:
                                            {path: [mode, object hash]}
        trees/archive/<sha>.json            the same, named by a format 1 lock's `sha` (the files `git archive` gave)
        git/<sha256 of the URL>.git         a bare repository per dependency URL, kept: the next version fetches only
                                            what changed, and a commit fetched before needs no network
        pdk/<commit>-<variant>-<root>.json  a PDK's part hashes and the stamps of its files (`pdk verify`): <root> is the
                                            first 12 hex of the SHA-256 of the PDK's real path (nocommit-<root>-<name>
                                            for a PDK with no version)
        build/<commit>/<variant>/osdi/      compiled Verilog-A models (`pixi run osdi`); build/local-<root>/... for a PDK
                                            with no version
        projects/<sha256 of the path>.json  {path, synced}: the projects that added, updated, synced or locked a
                                            dependency; `store gc` keeps what their locks name
        tmp/                                every write lands here first and is then renamed into place
        .lock                               held shared by a dependency operation, exclusive by `store gc`
```

- オブジェクトのハッシュはファイルの生のバイト列の SHA-256 です。木のハッシュは、`sha256:` に、各ファイルについてパスの UTF-8 の
  バイト列の順に並べた `"{mode} {hex} {path}\0"` の SHA-256 を付けたものです(正確な規則は
  [パッケージと PDK](../design/packages.ja.md#index) にあります)。モード(`100644`、実行ファイルなら `100755`)はハッシュにだけ
  入ります。いくつものプロジェクトにリンクする 1 つのファイルはモードを 1 つしか持てないので、オブジェクトも install したファイルも
  すべて読み取り専用(`0444`)です。
- `.undine/deps/<lib>/` にはオブジェクトへのハードリンクが入るので、10 個のプロジェクトや、プロジェクトの changeset の作業コピーが
  使うライブラリも、ディスクは一度分しか使いません。ストアとプロジェクトが別のファイルシステムにあるとき、またはファイルシステムが
  リンクを断るときは、代わりにコピーします(これも読み取り専用)。`UNDINE_STORE_LINK=copy` なら常にコピーします。
- その場で書き換えたファイルは見つかります。`lib status` はその依存関係を `modified` と報告し、オブジェクトはリンクする前に
  ハッシュし直します。書き込めるようにしただけのもの(install したファイルへの `chmod` はオブジェクトも変えます)は、ハッシュが
  名前と合うので、読み取り専用に戻して残します。`python -m undine store verify` はストア全体をハッシュし直し、合わないものを
  消します。`store gc` は、ここのどのプロジェクトも固定も install もしていないものを消します([コマンドライン](cli.ja.md#command-line))。
- `.undine` と `.undine/deps` はプロジェクト自身のディレクトリでなければなりません。どちらかがシンボリックリンク(またはファイル)の
  とき、依存関係のコマンドは何かを変える前に断り、`lib status` はそれを報告します
  ([トラブルシューティング](../troubleshooting.ja.md#undinedeps-is-a-link-or-not-a-directory-or-undine-is))。
- 形式 2 の木のストアの記録がないとき(ストアを空にした、動かした、`.undine/deps` ごとプロジェクトをコピーした)、`lib status` は
  install の目印が記録したモードで、install したファイルを lock の木と照らし合わせます。次の `lib sync` がそれをストアに取り込みます。
- 残したリポジトリ(`git/`)、PDK の記録(`pdk/`)、ビルド(`build/`)は `store gc` では消しません。場所が要るときは手で消して
  ください(必要になればまた作ります)。

| 変数 | 既定値 | 用途 |
|---|---|---|
| `UNDINE_HOME` | `~/.undine` | ユーザーごとのファイル: ストア(`UNDINE_STORE` がなければ)、UI で追加した接続 |
| `UNDINE_STORE` | `$UNDINE_HOME/store` | ストアだけの場所。例えばプロジェクトのルートと同じファイルシステムに(ハードリンクは 1 つのファイルシステムの中でしか使えない) |
| `UNDINE_STORE_LINK` | `link` | `copy`: 依存関係をハードリンクではなく読み取り専用のコピーで入れる(ほかの値は拒否) |
| `UNDINE_OFFLINE` | オフ | `1`: `lib sync` と `POST .../deps/sync` は、`lib sync --offline` と同じくストアからだけ入れ、取得しない |
| `UNDINE_CIEL` | Undine の Python の隣の `ciel`、なければ `PATH` | `pdk fetch` が動かす ciel。ファイルでないパスにすると、どの ciel も動かさない |
| `UNDINE_FETCH_MAX` | `1073741824`(1 GiB) | 1 つの依存関係のファイルの合計の上限(バイト)。100 000 を超えるファイルも、どのファイルを読むより前に拒否する |

<a id="operation-log"></a>
## 操作ログ

回路図とレイアウトのすべての編集は、`<project>/history/<lib>.jsonl`(追記のみの JSON 行)に記録されます: 誰が(`actor`: `user` または `agent:<name>`)、
何を(`op` と `args`。オブジェクトは変わらない ID で指定)、なぜ(`reason`)、どの提案で(`changeset`)、その結果のファイルのハッシュ、そして構造的な `effects`
(追加・削除・変更したインスタンス、配線、テキストと、その前後の値)です。Undo と Redo はログから導くので、サーバーを再起動しても残ります。
編集のリクエストに `X-Undine-Actor: agent:<name>` と `reason` / `changeset` を付けると、変更の主体を示せます。`GET /api/p/<project>/history?lib=<lib>&cell=<cell>`
で読めます。形式は `server/undine/store/oplog.py` の先頭に書いてあります。

changeset の作業コピーでは、各記録に `gseq`(全ライブラリを通した changeset 内での位置)、`opid`(属する操作。1 回のツール呼び出しや 1 回の HTTP リクエストで、
複数の記録になることがある)、`turn`(エージェントのターン、または null)、`changeset` も付きます。worktree のログは起点でのプロジェクトのログから始まり、
changeset 自身の記録は、起点での最後の `seq`(記録の `base_seq`)より後のものです。番号付け、タイムライン、Undo と Redo はそれだけを見ます。
採用すると、それらの記録を番号を付け直して(`merged_seq` はブランチでの `seq`)プロジェクトのログに追加し、続けて
`{"op": "merge", "args": {"changeset": <id>, "action": "apply"}}` の記録を書きます。取り消しでは `"action": "revert"` のその記録だけを書きます。
Undo は `merge` の記録で止まります。[ライブ changeset](../design/live-changesets.md) を参照してください。

<a id="job-provenance"></a>
## ジョブの来歴

シミュレーション、DRC、LVS のジョブはすべて、何に対して実行し、何を書き出したかを `status.json` に記録します。そのため、結果をそれが属する設計までたどれ、
古くなった結果がわかります:

```json
"inputs": {
  "design":  [{"view": "layout",  "lib": "mylib", "cell": "inv", "sha": "5f8e…"},
              {"view": "netlist", "lib": "mylib", "cell": "inv", "top": "inv", "mode": "lvs", "flat": false, "sha": "7a52…"}],
  "pdk":     "sky130A@8afc8346a57f",
  "deck":    {"kind": "lvs", "name": "sky130.lvs", "sha": "9ac9…"},
  "tools":   {"klayout": "KLayout 0.30.12"},
  "options": {}
},
"key":   "3cb3dfc6557fbe4d217c7f48",
"files": {"result.lvsdb": "f68b…", "input.gds": "8830…", "schematic.spice": "7a52…", "extracted.cir": "7484…", "log.txt": "78ab…"},
"stamps": {"result.lvsdb": [182334, 1762149600123456789, 5374021], ...}
```

| 設計の項目 | ハッシュの対象 |
|---|---|
| `layout` | セルと、そこから参照するものすべてを、ツールから見た形で: セル名、レイヤー、図形、インスタンス。図形の順序、セルのインデックス、インスタンスのプロパティ、メタ情報は含まないので、保存して読み直してもハッシュは変わりません |
| `netlist` | 回路図から生成したネットリスト(シミュレーションならテストベンチ、LVS なら比較の基準)。コメントや、接続を変えない配線の移動では変わらず、サブセルの編集では変わります。代役を使ったシミュレーションは代役を `substitute`(`{"lib/cell": "lib/cell"}`)として記録し、代役を含むネットリストのハッシュを取ります。代役を編集すると変わり、代役が置き換えるセルを編集しても変わりません。[代役](../guide/verification.md#stand-ins)を参照 |
| `file` | LVS が比較に使った、サーバー上の SPICE ファイル |
| `text` | リクエストで送られたネットリスト(古いとは判定しません) |
| `extraction` | 抽出結果で置き換えてシミュレーションしたセル(`mode` は `c` か `rc`): `job`(要求で指定した抽出ジョブ)があればそのジョブのサブサーキットで、そのジョブが古くなると `is gone` になります。なければそのモードの最新で古くなっていない抽出結果のサブサーキットで、そのような抽出結果がなくなると(レイアウトが変わると)`is gone` になります([寄生抽出](../guide/verification.md#parasitic-extraction-and-post-layout-simulation)) |

寄生抽出のジョブ(`kind: "pex"`)は、`layout` と、セルに回路図があればその `schematic-interface`(ポートの名前と順序)の上で実行します。
回路図のほかの編集では古くなりません。

シミュレーションのジョブは `models` も記録します。PDK のモデルライブラリを読む `.lib` の行ごとに、ネットリストが指定した `library` と
`section`、そして `reduced`(読んだコピー。`cards` は残したカードの数とセクションのカードの数)か `full`(ライブラリ全体を読んだ理由)の
どちらかです。入力は生成したままのネットリストのものなので、コピーを使っても key は変わりません
([PDK のモデル](../guide/verification.md#pdk-models-in-a-simulation))。

`key` はすべての入力のハッシュで、同じ key の 2 つのジョブは同じものに対して実行されています。設計、PDK、デッキ、ツールのバージョンが今と違えば、
そのジョブは **古い(outdated)** と判定します。`GET /api/p/<project>/jobs/<job>/freshness` は
`{"fresh": true | false | null, "changed": ["layout of mylib/inv changed", ...]}` を返します(`null`: 入力を記録するようになる前のジョブ)。
形式は `server/undine/store/provenance.py` の先頭に書いてあります。

changeset のチェックも、提案が開いている間は同じ `fresh` と `changed` を返します。`ok` はジョブの元の成否を保つので、`ok: true` で `fresh: false` は
「以前は合格した」という意味で、今の提案での合格ではありません。ジョブのない必要なチェックは `state: "not_run"` です。再チェックでは、完了していて最新の
ジョブを使い回し、ない、古い、不明、または成功しなかったジョブだけを実行します。

`stamps` は各出力ファイルの、ハッシュを計算したときの `[size, mtime_ns, inode]` です(その間に変わったファイルにはありません)。
changeset の証拠は、これが今も同じファイルについては、読み直さずに `files` のハッシュを使います。

changeset を採用または破棄すると、その変更とチェックの要約を、両方のプレビュープロジェクトのジョブのディレクトリと一緒に
`<project>/changesets/<id>/evidence/` に保存します。ジョブの `status.json` は、実際の結果とログとともに `inputs`、`key`、出力ファイルのハッシュを保ち、
起動に失敗したジョブが書いたものも含みます。`manifest.json` には、保存した変更(操作の引数を含む)、検証の要約、保存したジョブ、ファイルのハッシュを記録します。
`base/` と `work/` のサブディレクトリには、それぞれのプロジェクトの設定、依存関係のロック、`runs/` のジョブのディレクトリが残ります。
git 上の changeset ではマニフェストは `"version": 2` で、起点のコミットとレビューした先端(`"commits": {"base", "tip"}`)を記録します。設計のファイルと
操作ログ全体は(プロジェクトの `.gitignore` が `history/` を git から外していなければ)、`refs/undine/changesets/<project>/<id>` に残るそれらのコミットにあります。以前の changeset のアーカイブ(`"version": 1`)には、
`history/` のログも入っています。
ジョブのディレクトリのファイルはコピーではなく、実行結果のファイル自体へのハードリンクです(証拠が別のファイルシステムにある場合やリンクが
拒否された場合はコピーします。プロジェクトの設定、依存関係のロック、以前のアーカイブのログはその場で書き換わるのでコピーします)。マニフェストには、
リンクの数(`"linked"`)、全体のサイズ(`"bytes"`)、保存したときの各ファイルの `[size, mtime_ns, inode]`(`"sources"`。閉じる処理は
ファイルを読み直す代わりにこれと比べて、途中で何も変わっていないことを確かめます。これのないマニフェストはハッシュで比べます)を記録します。
終了したジョブのファイルはその場で書き換えず(`status.json` は新しいファイルに書いて置き換えます)、ジョブを止めるとそのプロセスが起動したもの
すべて(プロセスグループ。再起動したサーバーが引き継いだシミュレーションでも同じ)が止まるので、リンクは保存した内容を保ちます。それでも
書き込まれる可能性のある実行結果はコピーします(ジョブがキャンセルされた、制限時間で止められた(ステータスの `"timed_out": true`)、または
終了しなかった実行、どのサーバーも見ていない間に ngspice が強制終了された実行(ログの内容にかかわらず `"cut": true`)と、プロセスがその中で
作業しているか、そのファイルを書き込み用に開いている実行)。再起動したサーバーが引き継いだシミュレーションは、終わった後はほかの実行と同じく
保存します(そのステータスにも出力のハッシュとスタンプが入ります)。走っている間は、このサーバーでも同じルートの別のサーバーでも、changeset を
閉じられません。ジョブが終わった後も、そのジョブが起動したプロセス(ステータスに記録したプロセスグループ、`"process"`)が走っている間も
閉じられません。`cancel_job` で止められます。
閉じた changeset の実行の波形(`*.raw`、`*.vcd`、`*.fst`)は人が削除できます(`POST .../evidence/prune`)。ハッシュは `"files"` に残り、
`"pruned"` に `[{"t", "by", "files": [証拠の中のパス], "bytes"}]` として記録します。
changeset の記録には `evidence: {path, captured, jobs, bytes, pruned?}` が入ります(`pruned`: 最後の削除の時刻と人、全部のファイル数とバイト数)。保存したチェックの要約は、保存した時点の鮮度を保ち、実際のプロジェクトのその後の編集に
対して計算し直すことはありません。サーバーの再起動で中断したジョブは `interrupted` と表示され、やり直せます。元の状態ファイルは残ります。
保存の後に編集や新しいジョブが届いた場合は、`cleanup_warning` が、作業コピーを復旧用に残した理由を説明します。閉じた changeset は、保存した時点の内容を
表し続けます。止まったサーバーが書きかけのまま残した採用や取り消しを元に戻したとき(上の `merging.json` を参照)は、その注記を記録の
`interrupted` と `cleanup_warning` に入れます。その changeset の採用、取り消し、破棄、復旧が成功すると、注記は消えます。[変更セット](../guide/changesets.md)を参照してください。
