# AI エージェントをつなぐ

Undine は AI から操作できます。回路の説明を頼んだり、トランジスタの寸法を変えてもらったり、新しいセルを作ってもらったりできます。
AI が変更した内容はすべて **changeset**(変更セット)に入ります。changeset は作業コピーで、あなたが様子を見て、確認し、最後に自分で
**Adopt**(採用)または破棄します。採用するまで、あなたの設計は一切変わりません。

AI のつなぎ方は 2 通りあります。どちらか一方で構いません。両方を設定することもできます。

| | **A. MCP** — 普段使っている AI アプリから | **B. API** — Undine の中でチャット |
|---|---|---|
| チャットする場所 | Claude Desktop、Claude Code(またはほかの MCP クライアント) | Undine の **Agent** パネル |
| 必要なもの | AI アプリ(自分のアカウントでサインイン済み) | API キー(Anthropic、OpenAI など)、またはローカル LLM(Ollama など) |
| 費用 | AI アプリのサブスクリプション | API キーの従量課金(ローカル LLM なら無料) |
| 設計データの送り先 | AI アプリの提供元 | キーの提供元。ローカル LLM ならどこにも送られない |
| 準備 | Undine の拡張をインストール(約 5 分) | キーを貼り付けるだけ(約 2 分) |

すでに Claude Desktop を使っているなら **A** から始めてください。データをすべて手元に置いておきたいなら、**B** をローカル LLM で使います。

<a id="before-you-start"></a>
## 始める前に

Undine がインストール済みで([インストール](install.md))、プロジェクトがあること([最初のプロジェクト](first-project.md))が前提です。
以下の例では、チェックアウト先を `~/undine-eda` としています。自分の場所に読み替えてください。

Undine を **MCP を有効にして** 起動します(A では必須、B でも害はありません):

```sh
cd ~/undine-eda
UNDINE_MCP=1 pixi run serve
```

ブラウザで **<http://127.0.0.1:8700>** を開きます。プロジェクトとセルが表示されれば成功です。このタブは開いたままにしてください。
AI が作業する様子をここで見ます。

!!! note "MCP を有効にせずに起動していた場合"
    起動しているターミナルで Ctrl+C を押して止め、上のように `UNDINE_MCP=1` を付けて起動し直します。`./scripts/restart.sh` を使っている場合は
    `UNDINE_MCP=1 ./scripts/restart.sh` です。

## A. MCP: 普段使っている AI アプリからつなぐ

!!! info "MCP とは"
    MCP(Model Context Protocol)は、AI アプリが自分の外にあるツールを使うための標準の仕組みです。考えるのは AI アプリ(Claude Desktop、
    Claude Code)の側で、Undine はツール(「セルの一覧を出す」「M2 の W を変える」「LVS を実行する」など)を用意して、頼まれたとおりに
    実行するだけです。AI アプリに Undine を一度登録しておけば、あとは Undine が必要になるたびに、アプリが Undine の小さな起動役
    (`undine mcp-stdio`)を自分で起動します。

    これは **B** とは逆向きです。B では Undine 自身が LLM を呼びます(API キー、または Ollama のような OpenAI 互換のサーバー。
    Agent パネルの *EDA サーバーの LLM* の設定)。MCP では、そうした設定は何も要りません。

<a id="a1-claude-desktop-recommended"></a>
### A1. Claude Desktop(おすすめ)

**1. Undine の拡張を作ります**(最初の 1 回だけ。あとで Undine を更新しても作り直す必要はありません):

```sh
cd ~/undine-eda
pixi run mcpb
```

`dist/undine-0.1.0.mcpb` ができます。

**2. Claude Desktop にインストールします。** Claude Desktop で **Settings** → **Extensions** を開き、`.mcpb` ファイルをそのページに
ドラッグします(またはファイルをダブルクリックします)。インストールを確認します。このバンドルは署名されていないため、
信頼するかどうかを確認される場合があります。

!!! tip "Windows で Undine を WSL の中で動かしている場合"
    ファイルは WSL の中にあります。Windows のエクスプローラーでは `\\wsl$\Ubuntu\home\<ユーザー名>\undine-eda\dist\undine-0.1.0.mcpb`
    にあります(`Ubuntu` は使っているディストリビューション名に読み替えてください)。

**3. Claude Desktop が表示する拡張の設定を入力します:**

| 設定 | 入力する内容 |
|---|---|
| **Undine checkout** | undine-eda の場所: `~/undine-eda`。Windows では *WSL の中の* パス(こちらも `~/undine-eda`) |
| **Undine server URL** | 空欄のまま(`http://127.0.0.1:8700` を意味します) |
| **Undine UI URL** | 空欄のまま。UI を Vite の開発サーバー(`./scripts/dev.sh`)で動かしている場合だけ `http://localhost:5173` |

拡張が **有効(enabled)** になっていることを確認します。

**4. 試してみます。** Claude Desktop で新しいチャットを始め、たとえば次のように頼みます:

> Undine のプロジェクト `demo` で、ライブラリ `mylib` のセルの一覧を出して。

初回は、Undine のツールを使ってよいかを Claude が尋ねるので、許可します。続けて変更を頼みます:

> `demo` の `mylib` に新しいセル `ota5t` を作り、5 トランジスタの OTA(NMOS の入力差動対、PMOS のカレントミラー負荷、
> NMOS のテール電流源)と、ポート inp、inn、vbias、out、vdd、vss を入れて。

**5. Undine で様子を見ます。** Claude が変更を始めると:

- ブラウザで changeset の **レビュー**(右側)と、提案版の図面が開きます。
- Undine をすでに開いていれば、上部に *「claude-desktop (over MCP) is editing …」* というバーが出るので、**Watch** を押します。
- 図面は、Claude が編集しているセルに自動で追従します。ほかを見たいときは別のタブを押すか **Stop following** を押します。
  **Back to latest** で追従に戻ります。レビューの **Stop** を押すと、Claude のそれ以降の編集を止められます。

**6. レビューして採用します。** Claude の作業が終わったら、レビューを確認します。**By object**(何をなぜ変えたか)、
**By file**(変わったファイルごと、オブジェクト単位)、**Timeline**(手順。**Restore to here** で途中の状態に戻せます)、
**Diff**(あなたの設計との違い)があります。**Adopt**(採用)で設計にマージします。採用しない場合はゴミ箱のアイコンで破棄します。
あとから戻って確認することもできます。右上の **Changes** に、すべての提案が並んでいます。

提案はそれぞれ、プロジェクトの git ブランチ(`undine/demo/cs-…`)です。プロジェクトがまだ git で管理されていなければ、最初の提案で
git で管理するよう設定し、現在のファイルを最初のコミットにします(Undine がそう伝えます)。採用すると、プロジェクトにほかの未コミットの変更がなければ
マージコミットを作ります。[変更セット (changeset)](../guide/changesets.md) を参照してください。

Claude Desktop でそのまま会話を続けると(「M5 をもう少し長くして」など)、同じ提案の続きとして作業します。

<a id="claude-code"></a>
### A2. Claude Code

Claude Code は、ターミナル、または Claude デスクトップアプリの **Code** タブで動く Claude です。Undine を一度登録すれば、その後は
どのフォルダで始めた Claude Code のセッションからでも、Undine のツールを使えます。

この節のコマンドは、Undine があるターミナルで実行します。Windows では **WSL のターミナル**(スタートメニューのディストリビューション、
たとえば「Ubuntu」、または Windows Terminal で `wsl`)です。PowerShell やコマンドプロンプトでは動きません。そのターミナルに `claude`
コマンドがない場合(Claude デスクトップアプリの Code タブだけを使っている場合)は、先にこの節の最後の
[`claude` コマンドがない場合](#no-claude-command) を読んでください。

**1. Undine の Python があることを確かめます。** 登録では、Undine の pixi 環境の Python を Claude Code に教えます:

```sh
ls ~/undine-eda/.pixi/envs/default/bin/python
```

*No such file or directory* と表示されたら、先に Undine をセットアップしてください([インストール](install.md))。

**2. Undine を登録します**(一度だけ):

```sh
claude mcp add --scope user undine -- ~/undine-eda/.pixi/envs/default/bin/python -m undine mcp-stdio
```

それぞれの部分の意味:

| 部分 | 意味 |
|---|---|
| `claude mcp add` | Claude Code に MCP サーバーを登録する |
| `--scope user` | 自分のすべてのフォルダで使う。付けないと、コマンドを実行した git リポジトリ(git の外なら、そのフォルダ)だけの登録になり、ほかの場所で起動した Claude Code からは見えない |
| `undine` | サーバーの名前。Claude Code では Undine のツールが `mcp__undine__…` と表示される |
| `--` | これより後ろが、Claude Code が Undine につなぐために実行するコマンド |
| `…/python -m undine mcp-stdio` | Undine の起動役。動いている Undine を見つけ(なければ MCP を有効にして起動し)、アクセス用のトークン(プロジェクトのルートの `mcp-token`。`~/undine-projects/mcp-token`、または `$UNDINE_ROOT` の下)を自分で読み、Claude のツール呼び出しを Undine に渡す |

トークンをどこかにコピーする必要はありません。Undine を先に起動しておく必要もありません。Claude Code のセッションが始まるとき
(`claude mcp list` で確かめるときも)、Undine が動いていなければ、起動役が MCP を有効にして Undine を起動し、そのまま動かしておきます。
すでに動いている Undine は、MCP を有効にして起動したものである必要があります([始める前に](#before-you-start))。起動役はそれを
再起動しません。シェルが `~` をホームディレクトリに置き換えるので、登録にはフルパスが入ります。チェックアウトを移動したら、
登録し直してください。

以前に `--scope user` なしで Undine を登録していた場合は(このページの以前の版の手順)、先にそれを削除します。登録したフォルダで
`claude mcp remove --scope local undine` を実行します。残っていると、そのフォルダではそちらが使われ、`claude mcp get undine` も
*User config* と表示しません。

**3. 登録を確かめます:**

```sh
claude mcp list
```

Undine の行の最後が `✔ Connected` になっていることを確かめます:

```text
undine: /home/you/undine-eda/.pixi/envs/default/bin/python -m undine mcp-stdio - ✔ Connected
```

`✔ Connected` は、Claude Code が Undine の起動役を起動できたという意味です。Undine が動いていなかった場合は、このとき起動役が Undine を
起動するので、数秒かかることがあります。起動役が Undine 本体につながっているかは、手順 4 でわかります。`claude mcp get undine` で
詳しい内容を見られます。*Scope: User config (available in all your projects)* と出ていれば、`--scope user` が効いています。

**4. 試してみます。** Claude Code の **新しい** セッションを始め、A1 の手順 4 のように頼みます。ターミナルなら `claude`、Code タブなら
新しいセッションです(Undine が WSL の中にある場合は、セッションの環境に WSL のディストリビューションを選びます)。登録より前から
開いていたセッションには、新しいサーバーは入りません。Claude が Undine のツールを使うとき、Claude Code が許可を求めるので、許可します。
確認を減らすには、下の注を見てください。

Claude が Undine につながらないと言う場合(たとえば *Undine at … runs without MCP*)や、Undine のツールが `open_in_undine` しかない
場合(ターミナルのセッションでは `/mcp` で Undine のツールを確かめられます)は、起動役が Undine 本体につながっていません。
[困ったときは](#troubleshooting) を見てください。

??? note "任意: Undine のツールをまとめて許可する"
    Undine のツールは、設計を読むか changeset に書き込むだけです。changeset の内容は、あなたがレビューして採用するまで設計には
    反映されません。ツールごとに確認されないようにするには、`~/.claude/settings.json` でまとめて許可します(すべてのプロジェクトに
    効きます):

    - ファイルがなければ、次の内容だけのファイルを作ります:

        ```json
        {"permissions": {"allow": ["mcp__undine__*"]}}
        ```

    - ファイルがあれば、いちばん外側の `{ }` の中に `"permissions": {"allow": ["mcp__undine__*"]}` を 1 項目として加えます
      (前の項目のあとにカンマを付けます)。
    - すでに `"permissions"` と `"allow"` のリストがあれば、そのリストに `"mcp__undine__*"` を加えます。

**5. 任意: Undine のスキルを入れます。**

```sh
cd ~/undine-eda
pixi run python -m undine skills install
```

AI エージェント向けの Undine のガイド(changeset、設計意図、レイアウト、検証の進め方)を `~/.claude/skills/undine` にコピーします。
Claude Code はそこにあるスキルを自分で見つけます。入れなくても、Claude は同じガイドを MCP 経由で読めます。入れておくと、
Claude がより早くそれを参照するようになります。Undine を更新したら、もう一度実行してください。

様子を見る、レビューする、採用する流れは A1(手順 5 と 6)と同じです。

<a id="no-claude-command"></a>

!!! tip "`claude` コマンドがない場合(Claude デスクトップアプリの Code タブ)"
    Code タブも中で Claude Code を動かしていて、ターミナルの `claude` と同じ登録ファイル `~/.claude.json` を読みます。Undine が WSL の
    中にある場合は、Code タブでセッションの環境に WSL のディストリビューションを選びます。するとセッションは WSL の中で動き、そこにある
    `~/.claude.json` を読みます(Windows 上で直接動くセッションは、Windows 側の `%USERPROFILE%\.claude.json` を読みます)。ただし、
    Code タブを使っても、ターミナルで使える `claude` コマンドは入りません。方法は 2 つあります。

    **方法 1:** WSL(または macOS / Linux)のターミナルで、Claude Code のコマンドラインツールをインストールします(ほかの方法:
    [Claude Code のセットアップ](https://code.claude.com/docs/en/setup)):

    ```sh
    curl -fsSL https://claude.ai/install.sh | bash
    ```

    インストールしたら新しいターミナルを開き、`claude --version` が動くことを確かめます(まだ見つからない場合は、インストーラーが
    表示した PATH の案内に従います)。そのあと手順 2 を実行します。

    **方法 2:** Code タブでセッションを開き(WSL の中で。上のとおり)、Claude に登録を頼みます:

    > Undine を Claude Code のユーザースコープの MCP サーバーとして `~/.claude.json` に登録して(先にファイルをバックアップして)。
    > 名前は `undine`、コマンドは `~/undine-eda/.pixi/envs/default/bin/python` をフルパスにしたもの(`~` は私のホームディレクトリに
    > 置き換えて)、引数は `-m undine mcp-stdio`。

    そのあと、新しいセッションを始めます(手順 4)。参考: 手順 2 のコマンドは、`~/.claude.json` の最上位にある `"mcpServers"` の中に
    次の内容を書き込みます。Claude が書いた内容を確かめるときに使ってください(手で貼り付けないでください):

    ```json
    "undine": {"type": "stdio", "command": "/home/<ユーザー名>/undine-eda/.pixi/envs/default/bin/python",
               "args": ["-m", "undine", "mcp-stdio"], "env": {}}
    ```

**変更や削除:** `claude mcp remove --scope user undine` で登録を消せます(消えるのは起動役の登録だけで、Undine やプロジェクトは
そのままです)。変更する場合(別のチェックアウト、[A5](#undine-on-a-server) の `--ssh HOST` など)は、削除してから新しいコマンドで
登録し直します。新しいパスで手順 2 を、`--ssh` なら A5 のコマンドを実行します。

<a id="a3-claude-desktop-without-the-extension"></a>
### A3. 拡張を使わずに Claude Desktop を設定する

Claude Desktop の設定ファイルを直接編集したい場合は(**Settings** → **Developer** → **Edit Config**)、次を追加します:

=== "macOS / Linux"

    ```json
    {"mcpServers": {"undine": {
      "command": "/home/<you>/undine-eda/.pixi/envs/default/bin/python",
      "args": ["-m", "undine", "mcp-stdio"]}}}
    ```

=== "Windows(Undine が WSL の中)"

    ```json
    {"mcpServers": {"undine": {
      "command": "wsl.exe",
      "args": ["-e", "bash", "-lc", "cd ~/undine-eda/server && exec ../.pixi/envs/default/bin/python -m undine mcp-stdio"]}}}
    ```

編集したら Claude Desktop を再起動します。

### A4. ほかの MCP クライアント

ローカル(stdio)の MCP サーバーを起動できるクライアントなら、A2 と同じコマンドが使えます。HTTP で接続するクライアントは
`http://127.0.0.1:8700/mcp` に、ヘッダー `Authorization: Bearer <トークン>` を付けて接続します。トークンは
`<プロジェクトのルート>/mcp-token` にあります(既定は `~/undine-projects/mcp-token`。`$UNDINE_ROOT` を設定していればその下):

```sh
claude mcp add --scope user --transport http undine http://127.0.0.1:8700/mcp \
  --header "Authorization: Bearer $(cat ${UNDINE_ROOT:-~/undine-projects}/mcp-token)"
```

!!! warning "ブラウザ版のチャット(claude.ai、ChatGPT)はまだ使えません"
    これらのコネクターは、提供元のクラウドから MCP サーバーに接続します。そのため Undine をインターネットから届く場所に置き、
    きちんとしたサインインを用意する必要があります。まだ作っていないので、Claude Desktop か Claude Code を使ってください。

<a id="undine-on-a-server"></a>
### A5. Undine はサーバー、AI アプリは手元のコンピューター

サーバーで MCP を有効にして Undine を起動し(`UNDINE_MCP=1 ./scripts/restart.sh`)、手元から `ssh me@eda-server true` がパスワードを
聞かれずに通ること(鍵か ssh-agent)を確かめてから、A2 か A3 のコマンドに `--ssh` を足します:

```sh
claude mcp add --scope user undine -- ~/undine-eda/.pixi/envs/default/bin/python -m undine mcp-stdio --ssh me@eda-server
```

Claude Desktop の拡張では、代わりに **SSH host** を入力します。起動役がサーバーへのポートを転送し、トークンを向こうで読み、
レビューを手元のブラウザで開きます。詳しくは [Undine が別のマシンにある場合](../guide/assistant.md#undine-on-another-machine) を参照してください。

### 仕組み(概要)

AI アプリが、あなたのコンピューター上で `undine mcp-stdio` を起動します。これが動いている Undine を見つけ(なければ起動し)、
AI のツール呼び出しを Undine に渡し、作業が始まるとブラウザでレビューを開きます。提案はすべて Undine に残るので、
チャットを閉じても何も失われません。1 つのチャットのサブエージェントはその接続を共有するので、それぞれが自分の `agent` の名前を Undine の
ツールに渡し、自分の changeset で作業します(Undine の手順書が Claude にそう伝えます)。詳しくは [設計エージェントと MCP](../guide/assistant.md) と
[1 つの接続で複数のエージェントが動くとき](../guide/assistant.md#several-agents-on-one-connection) を参照してください。

<a id="b-api-chat-inside-undine"></a>
## B. API: Undine の中でチャットする

**1. Agent を開きます。** アプリ内の Agent は既定では隠れています(主な使い方は A の MCP です)。上部バーの **Changes** のすぐ左にある Agent の
アイコン(ロボット)を押します。もう一度押すとチャットが隠れます。左の列の上部にチャットが出ます。その下の境目をドラッグすると、
チャット欄を広げられます。アドレスの末尾に `?chat=1` を付けて開くと(例: `http://127.0.0.1:8700/#/p/demo?chat=1`)最初から表示され、
レビューの **Request changes** でも開きます。

**2. 接続を追加します。** Agent のヘッダーにある設定アイコン(スライダー)を押し、**Add a connection** を押します:

| 項目 | 入力する内容 |
|---|---|
| **Provider** | **Anthropic (Claude)**、**OpenAI**、またはそれ以外のサービスやローカル LLM には **OpenAI-compatible (other URL)** |
| **Name** | 自由な名前。例: *自分の Claude キー* |
| **API key** | 提供元のコンソールで発行したキー(Anthropic の場合: console.anthropic.com → API keys) |
| **Base URL** | OpenAI-compatible の場合だけ: 接続先。例: Ollama なら `http://127.0.0.1:11434/v1` |

**Check and save** を押します。Undine は提供元にモデルの一覧を問い合わせてキーを確かめます。キーが間違っていればエラーが表示され、
何も保存されません。キーは Undine のサーバー上(`~/.undine/connections.json`、本人だけが読める)に保存されます。二度と表示されず、
プロジェクトにも入りません。いつでも削除できます(接続の横のゴミ箱アイコン)。

**3. 選びます。** 追加した接続を選び、**Model** を一覧から選んで(または入力して)、**Use this connection** を押します。
Agent のヘッダーに接続とモデルが表示されます。

**4. 頼みます。** 回路図のトランジスタやネットをクリックして質問したり、変更を頼んだりします(「新しいセル ota5t に 5 トランジスタの
OTA を作って」など)。エージェントはチャットに 1 手ずつ作業を表示します(**Stop** で止められます)。何かを変更すると、その changeset の
レビューが開き、図面が追従します。レビューと採用は A1 の手順 5 と 6 と同じです。

### ローカル LLM(手元から何も出さない)

[Ollama](https://ollama.com) をインストールし、ツールを使えるモデルを入れて、上と同じ手順で追加します。Provider は
**OpenAI-compatible**、Base URL は `http://127.0.0.1:11434/v1`、API キーは空欄です:

```sh
ollama pull qwen3:32b
```

または、同じ設定画面にある組み込みの **EDA サーバーの LLM**(英語の UI では *EDA-server LLM*)を選び、エンドポイント `http://127.0.0.1:11434/v1`、モデル `qwen3:32b` を
指定します。小さいモデルは、何手もかかる設計作業でうまくいかないことがよくあります。手元で快適に動く範囲で、なるべく大きいモデルを使ってください。

### サーバーの環境変数でキーを渡す(別の方法)

UI を使う代わりに、管理者がサーバーにキーを渡すこともできます。`~/.undine/env`(権限 600)に `ANTHROPIC_API_KEY=...` と書き、
`./scripts/restart.sh` で再起動します。設定画面には **Anthropic — server's key** として表示されます。

<a id="troubleshooting"></a>
## 困ったときは

| 表示されること | 対処 |
|---|---|
| *「Undine at … runs without MCP」* | Undine を `UNDINE_MCP=1` 付きで起動し直します([始める前に](#before-you-start)) |
| *「Undine is not running at …」* | 拡張が Undine を起動できませんでした。Undine を自分で起動し、設定の **Undine checkout** を確認します |
| *「no Undine checkout with its pixi environment at …」* | 設定の **Undine checkout** が間違っているか、そこで `./setup.sh` を実行していません |
| `claude mcp list` で undine が `✘ Failed to connect — ENOENT …` | 登録した Python のパスがありません。パスを確かめ([A2](#claude-code) の手順 1)、削除してから登録し直します |
| Undine のツールが `open_in_undine` しかない(`/mcp`)、または Claude が Undine につながらないと言う | 起動役が Undine 本体につながっていません(MCP なしで動いている、または起動できなかった)。[始める前に](#before-you-start) のとおりに Undine を起動し(起動しなければ `~/.undine/serve.log` を確認)、新しいセッションを始めます |
| Claude Code で Undine のツールが出てこない(使えない) | 登録したあとに新しいセッションを始めます(Undine が WSL の中にあるなら、Code タブでは WSL のセッション)。あるフォルダでだけ使える場合は `--scope user` なしで登録されています。使えるほうのフォルダで `claude mcp remove --scope local undine` を実行し、そのあと(どのフォルダからでも)[A2](#claude-code) の手順 2 をもう一度実行します |
| `claude` コマンドが見つからない(`command not found`、「コマンドが見つかりません」など) | このターミナルに Claude Code のコマンドラインツールが入っていません(Code タブだけを使っている、Undine が WSL にあるのに Windows 側に入れた、など)。入れた直後なら、ターミナルを開き直します。[`claude` コマンドがない場合](#no-claude-command) を見てください |
| ブラウザでレビューが開かない | Undine を自分で開きます。**Watch** のバーで作業を見られます。WSL では Windows のブラウザが使われます |
| 編集しようとすると *「an external agent (…) is editing this changeset」* | AI がまだ作業中です。待つか、レビューの **Stop** を押します。チャットを閉じればすぐに、異常終了した場合は 2 分後に解除されます |
| *「the server policy … does not allow …」* | サーバーが `--llm-policy` 付きで起動されています。サーバーを動かしている人に確認してください |
| AI が部品を置いても、つながっていない | Undine を更新します(接続には `connect_pins` ツールを使います) |
| シミュレーション / DRC / LVS が動かない | プロジェクトに PDK が必要です(作成時に `--pdk sky130A`) |

<a id="next-steps"></a>
## 次のステップ

- [AI エージェントと設計する](../tutorials/agent.ja.md): つないだエージェントに設計意図に合わせてインバータのサイズを決めさせ、変更をレビューして採用するまでを順にたどります。
- [設計エージェントと MCP](../guide/assistant.ja.md): ツール、設計意図、複数のエージェント、スクリプトの詳細。
- [トラブルシューティング](../troubleshooting.ja.md#ai-agents-and-mcp): 接続やツールで困ったとき。
