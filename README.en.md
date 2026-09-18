# AI Whiteboard

> [中文](README.md) ｜ **English**

[![ci](https://github.com/ccjianxing/ai-whiteboard/actions/workflows/ci.yml/badge.svg)](https://github.com/ccjianxing/ai-whiteboard/actions/workflows/ci.yml)
&nbsp;Python 3.8+ &nbsp;·&nbsp; MIT &nbsp;·&nbsp; zero dependencies (standard library only)

**A whiteboard shared by you and your own AI agent.**

Not "yet another drawing tool with AI bolted on" — the point is: **one person uses the board, and their own agent joins
that board**. When you speak on the board you are talking to your agent; whatever it draws, edits or circles lands on
the same canvas you are looking at.

- Single-file front end: `board.html` (plain JS, no framework, no build step, no external requests)
- Zero-dependency back end: `server_v2.py` (**pure Python standard library** — nothing to `pip install`)
- Agent interface: `mcp_server.py` (standard MCP; or let an agent onboard itself by fetching one URL)

---

![Discussion mode demo](docs/demo.gif)

*Demo (scripted screen recording, no audio): discussion mode transcribes while it listens and tags **decisions /
risks / todos**; "🎯 Circle risks" draws the risk onto the matching element on the canvas; "💡 Suggested nodes" lists
what was mentioned in the discussion but is still missing from the diagram.*

---

## What makes it different

| | In one line |
|---|---|
| 🎙 **Meetings produce artefacts** | **Discussion mode** transcribes while it listens, tags the important sentences, and turns the whole thing into a written plan, a diagram and a todo list in one click. Others hand you a whiteboard; this hands you the **meeting → diagram → todos** pipeline. |
| 🤖 **Your agent lives on the same board** | It installs nothing on your machine — it fetches `http://<your-board>/join.md` and it is in. It hears what you say on the board, and everything it draws carries a **green "A" badge**. |
| 📄 **Drop a plan in, get a diagram** | Drag a `.md/.txt/.json` into the chat box; the AI/agent reads it first, then draws it. Long documents are read in line ranges so the context window survives. |
| 👥 **Simultaneous drawing without clobbering** | Not whole-board overwrite but **per-element merge**: everyone draws, nothing gets overwritten, deletions come back as tombstones, real collisions are reported. |
| 🧩 **Zero-dependency self-hosting** | Single-file front end plus a pure-stdlib back end: `python server_v2.py` and it runs, on Python 3.8 too. The built-in AI is entirely optional. |

## Discussion mode: from speech to artefacts

This is the part of the project that got the most care — it exists because **meetings end with nobody having drawn the
diagram or written down the todos**.

Click "讨论" in the top bar and the rest is automatic:

| Step | What it does |
|---|---|
| 1️⃣ **Transcribe while listening** | Microphone → server-side recognition → lines appear with a timestamp and a speaker. Silence of 1.8 s closes a segment; you can pause any time, and the panel can be **collapsed into a small pill** so it never blocks the canvas. |
| 2️⃣ **Split speakers automatically** | Each segment gets voice features (pitch, spectral centroid, zero-crossing rate) clustered online into up to 4 speakers; names are editable. |
| 3️⃣ **Several devices, one discussion** | Every device contributes its own track and everything is merged onto the same board. It cross-checks and warns when "**this device has more than one voice**" or "**these two devices sound like the same person**". |
| 4️⃣ **Important sentences get tagged** | Five categories are scanned in real time and shown as coloured chips next to the transcript: 🟢 decision ｜ 🔴 risk ｜ 🔵 todo ｜ 🟠 question ｜ 🟣 dependency. |
| 5️⃣ **Canvas timeline with replay** | From the moment discussion starts, **every change to the canvas is recorded** (with snapshots). Click any entry to **replay** to that moment — optionally with the audio of that segment. |
| 6️⃣ **Finish & summarise** | One click sends "transcript + canvas changes + current canvas + context" to the AI → you get a **written plan** (copy it or write it into a new board) plus an **extracted todo list**. |

And then four one-click follow-ups:

| Button | What it does |
|---|---|
| 🖊 **Draw it** | Turn the conclusions into a flowchart on a new board |
| 🎯 **Circle risks** | Put the risks/problems found in the discussion onto the matching elements on the canvas |
| 💡 **Suggested nodes** | List nodes that were **mentioned but are not on the diagram yet** (text similarity), so you notice what is missing |
| ⬇ **Export MD** | One Markdown file: plan + full transcript + canvas timeline + board screenshot |

> Typical uses: **requirements review, incident post-mortem, design discussion**. Keep discussion mode running while you
> draw; when the meeting ends you already have the plan, the diagram and the todos.
>
> ![Interface](docs/board.png)
>
> *A still of the UI: bottom-left is the discussion panel (5 transcript lines with decision / risk / todo / dependency
> tags, plus a "these two devices sound like the same person" warning); on the canvas is the flowchart that grew while
> the discussion was running; the red dashed circle is what "🎯 Circle risks" marked.*

## 30-second quick start

```bash
python server_v2.py
# open http://127.0.0.1:9091/board.html
```

That's it. **No API key, no dependencies.**

The board now runs in **pure-agent mode**: the built-in AI stays out of the way and waits for your agent to connect.

## Let your agent onboard itself (recommended)

**No docs to read, no config file to write — just point your agent at a URL.**

The board publishes how to join it, so an agent can onboard itself:

| Request | What you get |
|---|---|
| `GET /` or `/board.html` | the board itself (for humans) |
| `GET /.well-known/agent.json` | **machine-readable manifest**: MCP endpoint, HTTP API, onboarding link |
| `GET /join.md` or `/llms.txt` | **onboarding instructions** (markdown — an agent can follow it step by step) |
| `POST /mcp` | **MCP over HTTP** — an MCP-capable agent connects straight here, **no local files needed** |

**Everything an agent reads is bilingual**: `/join.md?lang=en`, `/.well-known/agent.json?lang=en`,
`/mcp?board=<id>&lang=en` and `WB_LANG=en` for the stdio variant give English tool descriptions
(46 + 3 + 2 of them — they are how the model picks a tool). No parameter means Chinese.

An agent that fetches `http://<host>:<port>/join.md` needs three calls to start working:

```
POST /api/agent/hello   {"name":"your name"}        ← check in; the user sees you on the board
POST /api/chat/wait     {"since":0,"timeout":50}    ← hear what the user said
POST /api/chat/push     {"who":"your name","text":"..."} ← answer
```

Board tools: `GET /api/tools` (46 of them); call them with `POST /api/agent/call {"tool":..., "args":{...}}`.

> `/join.md` and `/.well-known/agent.json` are readable **without a token** — and on token-protected deployments the
> manifest tells the agent to send `X-WB-Token`.

## Connect your agent (MCP, local-file variant)

Add a block to your MCP client config:

```json
{
  "mcpServers": {
    "ai-whiteboard": {
      "command": "python",
      "args": ["/absolute/path/mcp_server.py"],
      "env": { "WB_AGENT_NAME": "my assistant" }
    }
  }
}
```

As soon as the agent connects, the board shows:

```
🤖 my assistant connected — talking here is talking to it
```

Then:

- **You type on the board** → the message enters the shared conversation → the agent hears it with `board_chat_wait` →
  answers with `board_chat_say` → you see it immediately
- **The agent calls any of the 46 board tools** → flowcharts / architecture diagrams / sequence diagrams, adding-editing-
  deleting shapes, connectors, auto layout, annotation, export, multi-page, presentation, replay…

MCP exposes **51 tools** in total (46 board tools + 3 chat tools + 2 document tools).

> ⚠️ Board tools execute **inside the browser** (they act on the real canvas of an open page), so **one browser tab must
> have `board.html` open** on that board. Chat and document tools do not need it — they live on the server.

## Want the built-in AI? (optional)

Without an agent, the board can also run its own AI:

```bash
cp ai_config.example.json ai_config.json   # then fill in api_key
```

**Leaving the key empty changes nothing about how the board works** — you just do not get the built-in AI.

## Feature overview

| Capability | Notes |
|---|---|
| **Discussion mode** | the whole section above: transcription / speaker split / multi-device / tagging / canvas timeline with replay / one-click summary + diagram + risk circling + suggested nodes + Markdown export |
| Drawing | 33 shapes (flowchart / architecture / sequence / swimlane / UML class / table / queue / firewall / browser …), freehand, connectors, auto layout, align & distribute |
| Multiple pages | page manager, cross-page reference blocks (a frame showing another page live), comparison, presentation mode |
| Board management | right-click a board tab for Rename / Duplicate / Delete / Move / Present / Export this board as PNG; **deleting is undoable**; `Alt+1..9` jumps between boards |
| Toolbar groups | the left toolbar is split into **6 collapsible groups** (Basic / Lines / Shapes / Flow / Structure / Other) with remembered state |
| Element panel | top-bar "▤ Elements": elements **grouped by type**, **each group collapses**, click an item to select and centre it |
| Web review | screenshot a web page onto the board and annotate it directly; AI/agent can see every mark you made |
| Send a file to the AI | click 📎, or **drag a file straight into the chat box**: the AI/agent reads your plan (md/txt/json/csv…), then draws it; long documents are read in line ranges, up to 400k characters |
| History that survives | **conversation, discussion transcript and uploads are persisted per board** (`chat.json` / `disc.json` / `docs.json`) — restart the service or open the board from another device |
| Collaboration | several people on one board (`?board=xxx` link), **element-level merge: everyone draws without overwriting each other** |
| Accounts | register / sign in → everyone gets their own board, **and their agent garrisons that board**; invite links bring others in |
| Voice | microphone → server-side recognition (**HTTPS required**); **if the server side is unavailable the browser's own recognition takes over and the UI says so** |
| Export | PNG / SVG / PDF / JSON / Mermaid / PlantUML |

### Why simultaneous editing does not clobber itself

When several people work on one board, the sync is **per element, not whole-board overwrite**:

- the front end pushes only the elements it actually changed (content fingerprints) together with a modification time;
- the server compares timestamps per element: newest wins; deletions are propagated as **tombstones**;
- each side keeps its "not yet pushed" edits and re-pushes them — you never lose a shape you just drew;
- if two people edit **the same** element, the newer timestamp wins and the UI tells you "N conflicting edits were merged".

Boards are isolated: agent membership, chat, uploads and tool calls are all per board; an empty `?board=` means the
`default` board (never a wildcard).

## One person + their agent (the intended setup)

1. Open the board → 👤 in the top bar → **register** (username + password).
2. You get **your own board** (its id follows your account); nobody else sees it and nothing leaks across.
3. Let your agent in: take the address from the 「invite」 panel, or hand
   `https://<host>:9443/join.md?board=<your-board>` to your agent — it connects itself.
4. From then on it sees what you say, draw and circle on the board, and what it draws shows up in front of you.
5. To let someone else look or draw: send them the invite link (`?board=<your-board>`).

> Voice input needs HTTPS: use `https://<host>:9443/board.html` on a LAN (self-signed certificate — accept it once).
> Over plain HTTP `navigator.mediaDevices` is `undefined`; that is a browser rule, and the page offers a one-click switch.

## Deploying it for a team

```bash
PORT=9091 python server_v2.py      # listens on 0.0.0.0
```

- **Access token** — three policies, selected by environment variables (the server generates `wb_token.txt` on first
  start and prints it):

  | Environment variable | Behaviour | Good for |
  |---|---|---|
  | *(none set)* | every non-local request needs a token; the page asks once | **public deployments** (safe default) |
  | `WB_TRUST_LAN=1` | private addresses (192.168./10./172.16-31.) need no token, external ones still do | **team on a trusted LAN** |
  | `WB_NO_TOKEN=1` | no token at all | fully trusted, isolated networks |

  Share links can carry the token: `http://<host>:<port>/board.html?token=<token>`.
- Put Nginx/Caddy in front with HTTPS; do not expose the port directly to the internet.
- Docker:

```bash
docker build -t ai-whiteboard .
docker run -p 9091:9091 ai-whiteboard
```

> The container has no Edge/Chrome, so the screenshot feature is unavailable (it fails loudly, nothing else breaks).
> **State files live in the code directory** (`sync_state.json`, `todos.json`, `wb_token.txt`), so mount `/app` if you
> want them to survive a container rebuild.

## Agent-side details

| Tool | Purpose |
|---|---|
| `board_chat_wait(timeout)` | **hear** what the user said on the board (blocks up to 120 s, returns immediately on a message) |
| `board_chat_say` / `board_chat_read` | speak / read the conversation (no browser needed) |
| `board_doc_list()` | list the documents the user **uploaded** into the chat |
| `board_doc_read(id, from, to)` | read a document, **by line range**, so long plans can be read in chunks |
| the other 46 | board operations, defined once and shared with the built-in AI |

The tool list is parsed from `WB_TOOLS` in `server_v2.py` — there is no second copy to drift out of sync.

> An agent machine only needs **`mcp_server.py` + `wb_token.txt`**: with no local `server_v2.py` it fetches the tool list
> from `/api/tools`.

## Project layout

The repository contains only what you need to run it:

```
board.html           single-file front end (all CSS/JS inline, no framework, no build)
server_v2.py         back end (pure Python stdlib: sync, accounts, chat, discussion, todos, uploads,
                     MCP bridge, optional AI calls)
mcp_server.py        MCP server (exposes the board to any agent; no other local files needed)
截图.js              CDP full-page screenshot (used by web review; needs Chrome/Edge on the host)
ai_config.example.json   config template (copy to ai_config.json and fill in your own keys)
Dockerfile / docker-compose.yml  container deployment
README.md / README.en.md   documentation
LICENSE              MIT
.gitignore           keeps keys, tokens, canvas state and logs out of the repo (**do not delete**)
docs/demo.gif        the demo above
docs/board.png       a still of the UI
```

## Where the data lives

On the server (next to `server_v2.py` by default; plain JSON files, copy them to back up):

| File | Contents |
|---|---|
| `sync_state.json` | the canvas (authoritative); the browser keeps a local cache in `localStorage['wb2']` |
| `chat.json` | the board conversation (per board, last 300 messages) |
| `disc.json` | discussion transcript + timeline (per board) |
| `docs.json` | **uploaded plans/documents** (max 20 per board, 400k chars each, **expires after 24 h**) |
| `todos.json` | todos (including the ones extracted from a discussion) |
| `users.json` | accounts (salted hashes — never share it) |
| `web_rules.json` | web-review rules |

In the browser (`localStorage`): canvas cache, chat/discussion cache, session, language, collapse preferences.
Opening the same board elsewhere shows the server's history, so a restart or a new laptop loses nothing.

> Backing up means copying those JSON files. Uploaded attachments **expire after 24 hours on purpose** — they are
> material you hand to the AI, not a document store.

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
> **any file in the working directory**. Since 1.0.4 static files are an **allowlist** (`/board.html`, `/`, and the
> screenshot `docs/board.png`) and directory listings are refused. If you change that code, re-run the check above.

## Language status (being honest)

- **Everything an agent reads is bilingual** (onboarding doc, manifest, 51 tool descriptions) via `?lang=en`.
- **Documentation** ships in English and Chinese (`README.md` / `README.en.md`).
- **The UI has a 🌐 EN switch** in the top bar. It applies instantly and the choice is remembered. The table holds
  **843 strings** and covers buttons, **every dropdown menu** (including template names and on/off states), panel
  titles, options, tooltips and the common dynamic text.
- **Still Chinese**: a few long help paragraphs shown on hover, rare toasts and edge-case errors, and server-side error
  messages. The table is keyed by the Chinese original, so **anything untranslated stays Chinese rather than going blank**.
- **The default is Chinese and does not follow the browser locale** — a deterministic default keeps screenshot baselines
  stable. Click EN in the top bar to switch.

> Want the remaining long-tail strings translated? PRs welcome: add the Chinese original as a key in the `EN` table
> inside `board.html`.

## Security notes

- `ai_config.json` (API keys) and `wb_token.txt` (access token) **must never be committed** — `.gitignore` excludes
  them. **Do not delete `.gitignore`**: without it a single `git add .` publishes your keys and canvas state.
- State files are written with mode `0600`; `chmod 600` the `ai_config.json` you create yourself.
- Non-local requests need the token; `127.0.0.1` does not. `WB_TRUST_LAN=1` is for a **trusted LAN** and
  `WB_NO_TOKEN=1` for an isolated network — never expose either to the internet.
- **Discussion audio leaves your machine**: transcription goes to the speech gateway you configure, and if that is
  unavailable the browser's own online recognition is used. The UI says which one produced each line.
- **Static files are an explicit allowlist** (the page and the screenshot); everything else is 404 by design. Read the
  "Checking that it works" section before changing that code.

## Author

**ccjianxing** — https://github.com/ccjianxing/ai-whiteboard

Issues and pull requests are welcome.

## License

MIT — see [`LICENSE`](LICENSE).
