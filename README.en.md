# AI Whiteboard

> [中文](README.md) ｜ **English**

**A whiteboard that you and your AI agent share.**

Not "yet another drawing tool with AI bolted on" — the idea is: **one person works on the board, and their own agent joins it.**
When you talk on the board, you are talking to your agent. Whatever the agent draws, edits or circles lands on the same canvas you are looking at.

- Single-file front end: `board.html` (plain JavaScript, no framework, no build step)
- Zero-dependency back end: `server_v2.py` (**Python standard library only** — nothing to `pip install`)
- Agent interface: `mcp_server.py` (standard MCP; works with any MCP-capable client)

---

![AI Whiteboard](docs/board.png)

*The board in use: a flowchart built from a template. The green "A" badge means your agent drew that shape;
the strip at the bottom of the panel says "我的助手 已驻扎在这块板" (Chinese UI) — from that moment on, what you type here goes to it.*

---

## 30-second quick start

```bash
python server_v2.py
# open http://127.0.0.1:9091/board.html
```

That's it. **No API key, no dependencies.**

The board now runs in **pure-agent mode**: the built-in AI stays out of the way and waits for your agent to connect.

> 📖 Letting an agent onboard itself → the next section ｜ deploying it for a team → "Deploying it for a team".

## Let your agent onboard itself (recommended)

**No docs to read, no config file to write — just point your agent at a URL.**

The board publishes how to join it, so an agent can onboard itself:

| Request | What you get |
|---|---|
| `GET /` or `/board.html` | the board itself (for humans) |
| `GET /.well-known/agent.json` | **machine-readable manifest**: MCP endpoint, HTTP API, onboarding link |
| `GET /join.md` or `/llms.txt` | **onboarding instructions** (markdown — an agent can follow it step by step) |
| `POST /mcp` | **MCP over HTTP** — an MCP-capable agent connects straight here, **no local files needed** |

**Everything an agent reads is bilingual**: `/join.md?lang=en` and `/.well-known/agent.json?lang=en` return English;
MCP clients get **English tool descriptions** with `/mcp?board=<board-id>&lang=en` (HTTP) or `WB_LANG=en` (stdio).
Tool descriptions are what an agent uses to pick a tool, so English models do measurably better with them.
Without the parameter you get Chinese, which is the default.

For example, hand an agent `http://<server>:<port>/join.md?board=<board-id>` and it can start working from the three steps inside:

```
POST /api/agent/hello   {"name":"your-name","board":"<board-id>"}      ← say hello; the user sees you on the board
POST /api/chat/wait     {"since":0,"timeout":50,"board":"<board-id>"}  ← listen to the user
POST /api/chat/push     {"who":"your-name","text":"...","board":"<board-id>"} ← reply
```

Board tools: `GET /api/tools` (46 of them); call them with `POST /api/agent/call {"tool":..., "args":{...}, "board":"<board-id>"}`.

> Both `/join.md` and `/.well-known/agent.json` are readable **without a token**, so an agent can always find out how to
> authenticate. On deployments that require a token, the manifest tells it to send `X-WB-Token`.
>
> **Every request must carry the board id.** Without it you land on the `default` board — the user will not see you,
> and you will not see the user. This is how "whoever created the board owns the agent" is enforced.

## Connect your agent (MCP, local-file variant)

Add this to your MCP client config:

```json
{
  "mcpServers": {
    "ai-whiteboard": {
      "command": "python",
      "args": ["/absolute/path/mcp_server.py"],
      "env": { "WB_SERVER": "http://127.0.0.1:9091", "WB_BOARD": "<board-id>", "WB_AGENT_NAME": "my-assistant" }
    }
  }
}
```

As soon as the agent connects, the bottom of the board panel shows:

```
🤖 我的助手 已接入 —— 你在这里说话就是跟它对话     (Chinese UI)
```

Then:

- **You type in the board** → the message goes into the shared conversation → the agent hears it with `board_chat_wait` → answers with `board_chat_say` → you see it immediately
- **The agent calls any of the 46 board tools** → flowcharts / architecture diagrams / sequence diagrams, adding-editing-deleting shapes, connectors, auto layout, annotation, export, multi-page, presentation, replay…

MCP exposes **51 tools** in total (46 board tools + 3 chat tools + 2 document tools).

> ⚠️ Board tools execute **inside the browser** (they act on the real canvas of an open page), so **one browser tab must have
> `board.html` open** on that board. Chat and document tools do not need it — they live on the server, so an agent can
> leave messages, and read an uploaded plan, even when nobody has the page open.

## Want the built-in AI? (optional)

Without an agent, the board can also run its own AI:

```bash
cp ai_config.example.json ai_config.json   # then fill in api_key
```

**Leaving the key empty changes nothing about how the board works** — you just do not get the built-in AI.
Voice input (`asr_model` + speech key) is the only feature that genuinely requires configuration.

## Feature overview

| Capability | Notes |
|---|---|
| Drawing | 33 shapes (flowchart / architecture / sequence / swimlane / UML class / table / queue / firewall / browser …), freehand, connectors, auto layout, align & distribute |
| Multiple pages | page manager, cross-page references, comparison, presentation mode |
| Board management | right-click a board tab for Rename / Duplicate / Delete / Move left / Move right / Move to first / Move to last / Present from here / Export this board as PNG; **deleting is undoable** (a top bar offers Undo for 12 seconds); `Alt+1..9` jumps between boards |
| Toolbar groups | the left toolbar is split into **6 collapsible groups** (Basic / Lines / Shapes / Flow / Structure / Other) and the state is remembered; the flow and structure symbol sets used to hide behind a popup menu and are now visible inline |
| Element panel | top-bar "▤ Elements": the current board's elements **grouped by type** (biggest group first), **each group collapses** (state is remembered); click an item to select and center it, click a count to select the whole group |
| Web review | screenshot a web page onto the board and annotate it directly; AI/agent can see every mark you made |
| Discussions | record → transcribe → structure into a plan → draw it |
| Todos | extracted from discussions / reviews, persisted to disk |
| Collaboration | several people on one board (`?board=xxx` link), **element-level merge: everyone draws without overwriting each other** |
| Accounts | register / sign in → everyone gets their own board, **and their agent garrisons that board**; invite links bring others in |
| Voice | microphone → server-side speech recognition (**requires HTTPS**: browsers only expose the microphone in a secure context); **if the server side is unavailable (e.g. the gateway ran out of quota) the browser's own recognition takes over automatically, and the UI says plainly that the line came from the browser** |
| Send a file to the AI | click 📎 in the chat, or **drag a file straight into the input box**: the AI/agent reads your plan (markdown/txt/json/csv…) and then draws it; long documents can be read line-range by line-range, up to 400k characters |
| History that survives | **the conversation, the discussion transcript and uploaded documents are persisted per board** (`chat.json` / `disc.json` / `docs.json`) — restart the service or open the board from another device, the history is still there |
| Export | PNG / SVG / PDF / JSON / Mermaid / PlantUML |

### Why simultaneous editing does not clobber itself

When several people work on one board, the sync is **per element, not whole-board overwrite**:

- the front end pushes only the elements it actually changed (content fingerprints — no hooks buried in every mutation site) together with a modification timestamp;
- the server compares timestamps per element: newest wins; deletions are propagated as **tombstones**, so something you deleted is not pushed back by a peer;
- each side keeps its "not yet pushed" edits and re-pushes them — you never lose a shape you just drew to someone else's sync;
- if two people edit **the same** element, the newer timestamp wins and the UI tells you "N conflicting edits were merged" instead of silently dropping one.

Boards are invisible to each other: agent presence, chat and tool calls are all scoped by board id.
An empty `?board=` is treated as the `default` board (**never a wildcard** — otherwise one stale page can steal tool calls
meant for somebody else's board; we actually hit that bug in testing).


## One person + their agent (the intended setup)

The design premise is: **one person mainly uses one board, and their agent garrisons that board.** You are, in effect, talking to your own agent.

1. Open the board → 👤 in the top bar → **register** (name + password).
2. You get **your own board** (its id derives from your account). Nobody else sees it, nothing bleeds across.
3. Bring the agent in: click 「邀请别人」 in the 👤 panel to get the address, or hand your agent
   `https://<server-ip>:9443/join.md?board=<your-board-id>` — it will onboard itself.
4. From then on, whatever you say, draw or circle on the board is visible to the agent; what it draws appears in front of you.
5. Want others to watch or draw with you? Send them the **invite link** (`?board=<your-board-id>`); opening it puts them on your board.

> Voice input needs HTTPS: on a LAN, open `https://<server-ip>:9443/board.html` (self-signed certificate — the browser
> asks for confirmation once). Over plain HTTP `navigator.mediaDevices` is `undefined`; that is a browser rule, not a bug,
> and the page detects it and offers a one-click switch to the HTTPS address.

## Deploying it for a team

The essentials:

```bash
PORT=9091 python server_v2.py      # listens on 0.0.0.0
```

| Environment variable | Behaviour | Good for |
|---|---|---|
| *(none set)* | every non-local request needs a token; the page asks once | **public deployments** (safe default) |
| `WB_TRUST_LAN=1` | private addresses (192.168./10./172.16-31.) need no token, external ones still do | **team on a LAN** (opens and works) |
| `WB_NO_TOKEN=1` | no token at all | fully trusted, isolated networks |

A shared link may carry the token: `http://<server>:<port>/board.html?token=<token>`.

> For internet-facing use, put Nginx/Caddy in front with real HTTPS instead of exposing the port directly.
> `docker compose up -d` works too — see the deployment guide (the container has no Edge/Chrome, so the
> "web review screenshot" feature is unavailable there; it fails with a clear error and nothing else is affected).

## Project layout

This repository only contains what you need to run it — 8 files plus the screenshot:

```
board.html           single-file front end (all CSS/JS inline, no framework, no build)
server_v2.py         back end (pure Python standard library: sync, accounts, chat, todos, uploads,
                     MCP bridge, optional AI calls)
mcp_server.py        MCP server (exposes the board to any agent; no other local files needed)
截图.js              CDP full-page screenshot (used by web review; needs Chrome/Edge on the host)
ai_config.example.json   config template (copy to ai_config.json and fill in your own keys)
Dockerfile / docker-compose.yml  container deployment
README.md / README.en.md   documentation
LICENSE              MIT
.gitignore           keeps keys, tokens, canvas state and logs out of the repo (**do not delete**)
docs/board.png       the screenshot used above
```

Environment: **Python 3.8+** (that is what Ubuntu 20.04 ships) and any modern browser.

## Where the data lives

On the server (next to `server_v2.py` by default; plain JSON files, copy them to back up):

| File | Contents |
|---|---|
| `sync_state.json` | the canvas (authoritative copy); the browser keeps a local cache in `localStorage['wb2']` |
| `chat.json` | the board conversation (bucketed per board, last 300 messages each) |
| `disc.json` | discussion transcript + timeline |
| `docs.json` | **uploaded plans/documents** (max 20 per board, 400k characters each, **expires after 24 hours**) |
| `todos.json` | todos |
| `users.json` | accounts (password hashes — never share it) |
| `web_rules.json` | web-review rules |

In the browser (`localStorage`): canvas cache, chat/discussion cache, session, language and collapse preferences.
Opening the same board from another device shows the history from the server, so a restart or a new laptop loses nothing.

> Backing up means copying those JSON files. Uploaded attachments **expire after 24 hours on purpose** — they are
> material you hand to the AI, not a document store; do not treat them as your only copy.

## Checking that it works

No test framework needed: start the server, open the page, draw something, connect an agent.

```bash
curl -s http://127.0.0.1:9091/api/health              # alive, and reports its capabilities
curl -s http://127.0.0.1:9091/api/tools | head -c 200 # the 46 board tools
curl -s http://127.0.0.1:9091/join.md | head -20      # the onboarding doc an agent follows
```

**Make sure these are not served** — please confirm once yourself (replace `<port>`):

```bash
for p in ai_config.json wb_token.txt users.json wb.key server_v2.py; do
  echo -n "/$p -> "; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<port>/$p
done
# expected: all 404
```

> Why this gets its own section: the server is built on `SimpleHTTPRequestHandler`, whose default behaviour is to serve
> **any file in the working directory**. Since 1.0.4 static files are an **allowlist** (`/board.html`, `/`,
> `docs/board.png`) and directory listings are refused. If you change that code, re-run the check above.

## Language status (being honest)

- **Everything an agent reads is bilingual** (onboarding doc, manifest, 51 tool descriptions) via `?lang=en`.
- **Documentation** ships in English and Chinese (`README.md` / `README.en.md`).
- **The UI has a 🌐 EN switch** next to "更多 ▾" in the top bar. It applies instantly and the choice is remembered.
  The table holds **843 strings** and covers buttons, **every dropdown menu** (File / Template / Tidy / Code / Board /
  Export / More — including template names and on-off states), panel titles, dropdown options, tooltips and the common
  dynamic text (sync pill, agent strip, input placeholder). Menus are built when opened, so `popMenu` re-applies the
  table right after building them — we missed exactly that on the first pass.
- **Still Chinese**: a few long help paragraphs shown on hover, rare toasts and edge-case errors, and server-side error
  messages. The table is keyed by the Chinese original, so **anything untranslated stays Chinese rather than going blank**.
- **The default is Chinese and does not follow the browser locale** — a deterministic default keeps CI (headless
  browsers report en-US) and screenshot baselines stable. Click EN in the top bar to switch.

> Want the remaining long-tail strings translated? PRs welcome: add the Chinese original as a key in the `EN` table
> inside `board.html`.

## Security notes

- `ai_config.json` (API keys) and `wb_token.txt` (access token) **must never be committed** — `.gitignore` already
  excludes them. **Do not delete `.gitignore`**: without it, a single `git add .` publishes your keys and canvas state.
- State files are written with mode `0600` (only the service user can read them); `chmod 600` the `ai_config.json`
  you create yourself.
- Non-local requests need the token; `127.0.0.1` does not. `WB_TRUST_LAN=1` is for a **trusted LAN** only, and
  `WB_NO_TOKEN=1` for an isolated network — never expose either to the internet.
- **Static files are an explicit allowlist** (the page and the screenshot); everything else is 404 by design.
  Read the "Checking that it works" section before changing that code.

## License

MIT — see [`LICENSE`](LICENSE).
