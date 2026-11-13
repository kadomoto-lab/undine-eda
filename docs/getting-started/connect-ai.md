# Connect an AI agent

Undine can be driven by an AI: ask it to explain a circuit, change a transistor, or build a new cell. Whatever it changes goes
into a **changeset**, a working copy you watch, check and then **Adopt** (or discard) yourself. Your design is never changed
until you adopt.

There are two ways to connect an AI. Pick one; you can set up both.

| | **A. MCP** — use the AI app you already have | **B. API** — chat inside Undine |
|---|---|---|
| Where you chat | Claude Desktop, Claude Code (or another MCP client) | the **Agent** panel in Undine |
| What you need | the AI app, signed in to your account | an API key (Anthropic, OpenAI ...) or a local LLM (Ollama ...) |
| Who pays | your AI app subscription | per use of the API key (or nothing, with a local LLM) |
| Where the design data goes | to the AI app's provider | to the provider of the key, or nowhere (local LLM) |
| Setup | install the Undine extension, about 5 minutes | paste a key, about 2 minutes |

If you use Claude Desktop already, start with **A**. If you want everything to stay on your machine, use **B** with a local LLM.

## Before you start

Undine must be installed ([Install](install.md)) and have a project ([First project](first-project.md)). In the examples
below the checkout is `~/undine-eda`; replace it with yours.

Start Undine **with MCP turned on** (needed for A, harmless for B):

```sh
cd ~/undine-eda
UNDINE_MCP=1 pixi run serve
```

Open **<http://127.0.0.1:8700>** in your browser. You should see your project and its cells. Keep this tab open: this is
where you will watch the AI work.

!!! note "Already running without MCP?"
    Stop it (Ctrl+C in its terminal) and start it again with `UNDINE_MCP=1` as above. With `./scripts/restart.sh`, use
    `UNDINE_MCP=1 ./scripts/restart.sh`.

## A. MCP: connect the AI app you already use

!!! info "What MCP is"
    MCP (Model Context Protocol) is the standard way an AI app uses tools outside itself. The AI app (Claude Desktop,
    Claude Code) does the thinking; Undine only offers tools ("list the cells", "set W of M2", "run LVS") and carries
    them out. You register Undine in the AI app once; from then on the app starts Undine's small launcher
    (`undine mcp-stdio`) by itself whenever it needs Undine.

    This is the opposite direction from **B**, where Undine itself calls an LLM: an API key, or an OpenAI-compatible
    server such as Ollama (the *EDA-server LLM* setting of the Agent panel). With MCP you do not set up any of that.

### A1. Claude Desktop (recommended)

**1. Build the Undine extension** (once; updating Undine later does not need a new one):

```sh
cd ~/undine-eda
pixi run mcpb
```

This writes `dist/undine-0.1.0.mcpb`.

**2. Install it in Claude Desktop.** Open Claude Desktop → **Settings** → **Extensions**, and drag the `.mcpb` file onto
that page (or double-click the file). Confirm the installation. The bundle is not signed, so Claude Desktop may ask you to
confirm that you trust it.

!!! tip "Windows with Undine in WSL"
    The file is inside WSL. In Windows Explorer it is at `\\wsl$\Ubuntu\home\<you>\undine-eda\dist\undine-0.1.0.mcpb`
    (replace `Ubuntu` with your distribution's name).

**3. Fill in the settings** Claude Desktop shows for the extension:

| Setting | What to enter |
|---|---|
| **Undine checkout** | where undine-eda is: `~/undine-eda`. On Windows this is the path *inside WSL* (also `~/undine-eda`) |
| **Undine server URL** | leave empty (it means `http://127.0.0.1:8700`) |
| **Undine UI URL** | leave empty, unless you run the UI from the Vite dev server (`./scripts/dev.sh`): then `http://localhost:5173` |

Make sure the extension is **enabled**.

**4. Try it.** Start a new chat in Claude Desktop and ask, for example:

> In the Undine project `demo`, list the cells of library `mylib`.

Claude asks for permission to use the Undine tools the first time; allow it. Then ask for a change:

> In `demo`, create a new cell `ota5t` in `mylib` with a 5-transistor OTA (NMOS input pair, PMOS current mirror load,
> NMOS tail current source) and ports inp, inn, vbias, out, vdd, vss.

**5. Watch it in Undine.** When Claude starts changing things:

- a browser tab opens on the changeset's **review** (right side) and the proposal's drawing;
- if Undine is already open, a bar appears at the top: *"claude-desktop (over MCP) is editing …"* → click **Watch**;
- the drawing follows the cell Claude is editing. Click another tab or **Stop following** to look around; **Back to
  latest** follows again. **Stop** in the review stops Claude's further edits.

**6. Review and adopt.** When Claude is done, look through the review: **By object** (what changed and why), **By file**
(each changed file, object by object), **Timeline** (the steps; **Restore to here** goes back to an earlier step),
**Diff** (what differs from your design). Click **Adopt** to merge it into your design, or the bin icon to discard it.
You can come back later: **Changes** (top right) lists all proposals.

Each proposal is a git branch of your project (`undine/demo/cs-…`). If the project was not in git yet, the first
proposal puts it in git and commits its files as they are as the first commit (Undine tells you). Adopting makes a
merge commit when your project had nothing else uncommitted; see [Changesets](../guide/changesets.md).

You can keep chatting in Claude Desktop to continue the same proposal ("now make M5 longer"); Claude continues the open
changeset.

<a id="claude-code"></a>
### A2. Claude Code

Claude Code is Claude working in a terminal, or in the **Code** tab of the Claude desktop app. You register Undine with it
once; after that, every Claude Code session, started in any folder, can use Undine's tools.

Run the commands of this section in the terminal where Undine is: on Windows that is a **WSL terminal** (your
distribution in the Start menu, for example *Ubuntu*, or `wsl` in Windows Terminal), not PowerShell or Command Prompt.
If that terminal has no `claude` command (you use only the Code tab of the Claude desktop app), read
[No `claude` command?](#no-claude-command) at the end of this section first.

**1. Check that Undine's Python is there.** The registration points Claude Code at the Python of Undine's pixi
environment:

```sh
ls ~/undine-eda/.pixi/envs/default/bin/python
```

If this says *No such file or directory*, set up Undine first ([Install](install.md)).

**2. Register Undine** (once):

```sh
claude mcp add --scope user undine -- ~/undine-eda/.pixi/envs/default/bin/python -m undine mcp-stdio
```

What each part means:

| Part | Meaning |
|---|---|
| `claude mcp add` | register an MCP server with Claude Code |
| `--scope user` | for all your folders. Without it Undine is registered only for the git repository you run the command in (or that folder, outside a repository): Claude Code started anywhere else does not see it |
| `undine` | the server's name; Claude Code shows Undine's tools as `mcp__undine__…` |
| `--` | everything after it is the command Claude Code runs to reach Undine |
| `…/python -m undine mcp-stdio` | Undine's launcher: it finds the running Undine (or starts it, with MCP on), reads the access token (`mcp-token` in its projects root: `~/undine-projects/mcp-token`, or under `$UNDINE_ROOT`) by itself, and passes Claude's tool calls on |

You do not copy the token anywhere, and Undine does not have to be running yet: when a Claude Code session starts (and
when `claude mcp list` checks the server), the launcher starts Undine with MCP on if it is not running, and leaves it
running. An Undine that is already running must have been started with MCP on ([Before you start](#before-you-start));
the launcher does not restart it. Your shell turns `~` into your home directory, so the registration holds the full
path; if you move the checkout, register again.

If you registered Undine before without `--scope user` (earlier versions of this page did), remove that first, in the
folder where you did it: `claude mcp remove --scope local undine`. Otherwise it keeps being used there, and
`claude mcp get undine` there does not show *User config*.

**3. Check the registration:**

```sh
claude mcp list
```

Look for Undine's line ending in `✔ Connected`:

```text
undine: /home/you/undine-eda/.pixi/envs/default/bin/python -m undine mcp-stdio - ✔ Connected
```

`✔ Connected` means that Claude Code can run Undine's launcher; if Undine was not running, the launcher has just started
it, which can take a few seconds. Whether the launcher reaches Undine itself shows in step 4. `claude mcp get undine`
shows the details: *Scope: User config (available in all your projects)* means that `--scope user` took effect.

**4. Try it.** Start a **new** Claude Code session and ask as in A1 step 4: `claude` in a terminal, or a new session in
the Code tab (with Undine in WSL, with your WSL distribution as the session's environment). A session that was already
open does not pick up the new server. When Claude uses an Undine tool, Claude Code asks for your permission; allow it.
To be asked less, see the note below.

If Claude says it cannot reach Undine (for example *Undine at … runs without MCP*), or Undine's only tool is
`open_in_undine` (in a terminal session, `/mcp` lists Undine's tools), the launcher did not reach Undine: see
[Troubleshooting](#troubleshooting).

??? note "Optional: allow all of Undine's tools at once"
    Undine's tools only read the design or write into a changeset, and nothing of a changeset reaches your design until
    you review and adopt it. To stop Claude Code from asking for each tool, allow them all in `~/.claude/settings.json`
    (it applies to all your projects):

    - if the file does not exist, create it with exactly this content:

        ```json
        {"permissions": {"allow": ["mcp__undine__*"]}}
        ```

    - if it exists, add `"permissions": {"allow": ["mcp__undine__*"]}` as one more entry inside its outermost `{ }`
      (with a comma after the entry before it);
    - if it already has `"permissions"` with an `"allow"` list, add `"mcp__undine__*"` to that list.

**5. Optional: install Undine's skill.**

```sh
cd ~/undine-eda
pixi run python -m undine skills install
```

This copies Undine's guide for agents (how to work with changesets, design intent, layout, verification) to
`~/.claude/skills/undine`, where Claude Code finds it by itself. Claude can read the same guide over MCP without it; the
skill only makes Claude pick it up sooner. Run it again after updating Undine.

Watching, reviewing and adopting are the same as in A1 (steps 5 and 6).

<a id="no-claude-command"></a>

!!! tip "No `claude` command? (the Code tab of the Claude desktop app)"
    The Code tab runs Claude Code inside it and reads the same registration file as the `claude` command,
    `~/.claude.json`. With Undine in WSL, pick your WSL distribution as the session's environment in the Code tab: the
    session then runs inside WSL and reads the `~/.claude.json` there (a session on Windows itself reads the Windows
    one, `%USERPROFILE%\.claude.json`). The Code tab does not give you a `claude` command in your terminal, though.
    There are two ways:

    **Option 1:** install Claude Code's command-line tool in your WSL (or macOS / Linux) terminal (other ways:
    [Claude Code setup](https://code.claude.com/docs/en/setup)):

    ```sh
    curl -fsSL https://claude.ai/install.sh | bash
    ```

    Then open a new terminal and check that `claude --version` works (if `claude` is still not found, follow the note
    about PATH the installer printed), and run step 2.

    **Option 2:** open a session in the Code tab (in WSL, as above) and ask Claude to register it for you:

    > Register Undine as a user-scope MCP server for Claude Code in `~/.claude.json` (back the file up first): name
    > `undine`, command `~/undine-eda/.pixi/envs/default/bin/python` as a full path (with `~` replaced by my home
    > directory), arguments `-m undine mcp-stdio`.

    Then start a new session (step 4). For reference, to check what Claude wrote (do not paste it in by hand), step 2
    writes this inside the top-level `"mcpServers"` of `~/.claude.json`:

    ```json
    "undine": {"type": "stdio", "command": "/home/<you>/undine-eda/.pixi/envs/default/bin/python",
               "args": ["-m", "undine", "mcp-stdio"], "env": {}}
    ```

**To change or remove it:** `claude mcp remove --scope user undine` removes the registration (the launcher only; Undine
and your projects stay). To change it (another checkout, or `--ssh HOST` as in [A5](#undine-on-a-server)),
remove it and register again with the new command: step 2 with the new path, or the command of A5.

### A3. Claude Desktop without the extension

If you prefer to edit Claude Desktop's configuration file (**Settings** → **Developer** → **Edit Config**), add:

=== "macOS / Linux"

    ```json
    {"mcpServers": {"undine": {
      "command": "/home/<you>/undine-eda/.pixi/envs/default/bin/python",
      "args": ["-m", "undine", "mcp-stdio"]}}}
    ```

=== "Windows (Undine in WSL)"

    ```json
    {"mcpServers": {"undine": {
      "command": "wsl.exe",
      "args": ["-e", "bash", "-lc", "cd ~/undine-eda/server && exec ../.pixi/envs/default/bin/python -m undine mcp-stdio"]}}}
    ```

Restart Claude Desktop afterwards.

### A4. Other MCP clients

Any client that can start a local (stdio) MCP server can use the same command as in A2. Clients that connect over HTTP use
`http://127.0.0.1:8700/mcp` with the header `Authorization: Bearer <token>`, where the token is in
`<projects root>/mcp-token` (`~/undine-projects/mcp-token` by default, or under `$UNDINE_ROOT` if you set it):

```sh
claude mcp add --scope user --transport http undine http://127.0.0.1:8700/mcp \
  --header "Authorization: Bearer $(cat ${UNDINE_ROOT:-~/undine-projects}/mcp-token)"
```

!!! warning "Browser chats (claude.ai, ChatGPT) are not supported yet"
    Their connectors reach MCP servers from the provider's cloud, so Undine would have to be reachable from the internet
    with proper sign-in. That is not built yet; use Claude Desktop or Claude Code.

<a id="undine-on-a-server"></a>
### A5. Undine on a server, the AI app on your computer

Start Undine on the server with MCP on (`UNDINE_MCP=1 ./scripts/restart.sh`), make sure `ssh me@eda-server true` works from your
computer without a password prompt (a key or an ssh-agent), and add `--ssh` to the command of A2 or A3:

```sh
claude mcp add --scope user undine -- ~/undine-eda/.pixi/envs/default/bin/python -m undine mcp-stdio --ssh me@eda-server
```

In the Claude Desktop extension, fill in **SSH host** instead. The launcher forwards a port to the server, reads the token
there and opens the review in your browser. Details: [Undine on another machine](../guide/assistant.md#undine-on-another-machine).

### How it works (in short)

The AI app starts `undine mcp-stdio` on your computer. It finds the running Undine (or starts it), passes the AI's tool calls
to it, and opens the review in your browser when work starts. Undine keeps every proposal, so closing the chat loses
nothing. Subagents of one chat share its connection: each passes its own `agent` name to Undine's tools, so that each
works in a changeset of its own (Undine's guide tells Claude so). Details: [Agent and MCP](../guide/assistant.md) and
[Several agents on one connection](../guide/assistant.md#several-agents-on-one-connection).

## B. API: chat inside Undine

**1. Open the Agent.** The in-app Agent is hidden by default; MCP (A) is the main way. Click the Agent icon (the robot)
just left of **Changes** in the top bar; click it again to hide the chat. The chat appears at the top of the left column; drag
the line under it to make it taller. `?chat=1` at the end of the address (for example
`http://127.0.0.1:8700/#/p/demo?chat=1`) opens Undine with it shown, and **Request changes** in a review also opens it.

**2. Add your connection.** Click the settings icon (sliders) in the Agent's header, then **Add a connection**:

| Field | What to enter |
|---|---|
| **Provider** | **Anthropic (Claude)**, **OpenAI**, or **OpenAI-compatible (other URL)** for other services and local LLMs |
| **Name** | anything, e.g. *My Claude key* |
| **API key** | your key from the provider's console (Anthropic: console.anthropic.com → API keys) |
| **Base URL** | only for OpenAI-compatible: the endpoint, e.g. `http://127.0.0.1:11434/v1` for Ollama |

Click **Check and save**. Undine checks the key by asking the provider for its models; if the key is wrong you get the
error and nothing is saved. The key stays on the Undine server (`~/.undine/connections.json`, readable only by you); it is
never shown again, never put in a project, and you can remove it any time (bin icon next to the connection).

**3. Choose it.** Select the new connection, pick a **Model** from the list (or type one), and click **Use this
connection**. The Agent's header shows the connection and model.

**4. Ask.** Click a transistor or net in the schematic and ask about it, or ask for a change ("create a 5-transistor OTA in
a new cell ota5t"). The Agent works step by step in the chat (**Stop** stops it), the review opens on its changeset
when it changes something, and the drawing follows. Review and adopt as in A1 steps 5 and 6.

### Local LLM (nothing leaves your machine)

Install [Ollama](https://ollama.com), get a model that can use tools, and add it as above with provider
**OpenAI-compatible**, base URL `http://127.0.0.1:11434/v1` and no API key:

```sh
ollama pull qwen3:32b
```

Or use the built-in **EDA-server LLM** entry in the same settings: endpoint `http://127.0.0.1:11434/v1`, model `qwen3:32b`.
Small models often fail at multi-step design work; use the largest that runs well on your machine.

### Key in the server's environment (alternative)

Instead of the UI, an administrator can give the server a key: put `ANTHROPIC_API_KEY=...` in `~/.undine/env` (mode 600)
and restart with `./scripts/restart.sh`. It then shows as **Anthropic — server's key** in the settings.

## Troubleshooting

| What you see | What to do |
|---|---|
| *"Undine at … runs without MCP"* | restart Undine with `UNDINE_MCP=1` (see [Before you start](#before-you-start)) |
| *"Undine is not running at …"* | the extension could not start it: start Undine yourself and check the **Undine checkout** setting |
| *"no Undine checkout with its pixi environment at …"* | the **Undine checkout** setting is wrong, or `./setup.sh` has not been run there |
| `claude mcp list` shows `✘ Failed to connect — ENOENT …` for undine | the Python path of the registration does not exist: check it ([A2](#claude-code) step 1), then remove and register again |
| Undine's only tool is `open_in_undine` (in `/mcp`), or Claude says it cannot reach Undine | the launcher could not reach Undine: it runs without MCP, or could not be started. Start Undine as in [Before you start](#before-you-start) (if it does not start, see `~/.undine/serve.log`), then start a new session |
| Claude Code does not show Undine's tools | start a new session after registering (in the Code tab with Undine in WSL: a session in your WSL distribution). If they are there in one folder only, Undine was registered without `--scope user`: in the folder where the tools work, run `claude mcp remove --scope local undine`, then [A2](#claude-code) step 2 again (from any folder) |
| `claude: command not found` (or similar) | Claude Code's command-line tool is not installed in this terminal (for example, you use only the Code tab of the Claude desktop app, or installed it on Windows while Undine is in WSL), or it was just installed and this terminal does not see it yet (open a new one): see [No `claude` command?](#no-claude-command) |
| The review does not open in the browser | open Undine yourself; the **Watch** bar shows the work. On WSL the Windows browser is used |
| *"an external agent (…) is editing this changeset"* when you edit | the AI is still working on it: wait, or click **Stop** in the review. A chat that was closed releases it at once; a crashed one after 2 minutes |
| *"the server policy … does not allow …"* | the server was started with `--llm-policy`; ask whoever runs it |
| The AI places parts but they are not connected | update Undine (connections use the `connect_pins` tool) |
| Simulation / DRC / LVS cannot run | the project needs a PDK (`--pdk sky130A` when creating it) |

## Next steps

- [Design with an AI agent](../tutorials/agent.md): have the agent you connected size an inverter to its design intent, then review the change and adopt it, step by step.
- [Agent and MCP](../guide/assistant.md): the tools, design intent, several agents and scripts in detail.
- [Troubleshooting](../troubleshooting.md#ai-agents-and-mcp): when the connection or the tools do not work.
