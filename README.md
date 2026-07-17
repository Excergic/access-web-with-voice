# Voice Action Browser

A voice-controlled Gmail agent. Press `Alt+Space`, speak your intent, and the agent reads your email, reasons about what to do, and executes the action — with a confirmation step before anything is sent.

> "Reply to this email saying Tuesday works for me" → draft created → shown to you → sent on confirm.
> "Reply to this email" → agent reads the email body, composes a contextual reply → shown to you → sent on confirm.

---

## How It Works

```
You speak → Chrome Extension captures audio
         → Whisper transcribes it
         → LangGraph agent:
              grounding  (resolves intent + reply body from transcript + email context)
              planner    (creates ordered steps: draft_reply → send_email)
              executor   (calls Gmail API)
              confirmation gate  (pauses, shows draft, waits for your Yes/No)
              executor   (sends on confirm)
         → Done overlay shown in Gmail
```

**Stack:**
- Chrome Extension (TypeScript, MV3) — reads Gmail DOM, captures mic, shows overlay
- FastAPI + WebSocket (Python) — real-time bidirectional communication
- OpenAI Whisper — speech-to-text
- LangGraph — stateful agent with human-in-the-loop confirmation
- gpt-4o — intent grounding and reply composition
- Gmail API (OAuth2) — real draft creation and sending
- LangSmith — agent trace observability

---

## Project Structure

```
voice-action-browser/
├── extension/
│   ├── src/
│   │   ├── content.ts      # Gmail DOM reader, hotkey, mic, WebSocket, overlay UI
│   │   ├── types.ts        # Shared TypeScript types
│   │   ├── popup.ts        # Extension popup
│   │   └── background.ts   # Service worker placeholder
│   ├── manifest.json
│   ├── popup.html
│   ├── build.mjs           # esbuild config
│   └── package.json
└── server/
    ├── agent/
    │   ├── graph.py        # LangGraph StateGraph assembly
    │   ├── nodes.py        # Grounding, planner, router, executor, confirmation gate
    │   └── state.py        # AgentState + Pydantic models
    ├── rails/
    │   └── api/
    │       └── gmail.py    # Gmail API: draft_reply, send_draft
    ├── main.py             # FastAPI app + WebSocket + STT orchestration
    ├── auth_gmail.py       # One-time OAuth setup script
    └── pyproject.toml
```

---

## Setup

### Prerequisites

- Python 3.12+ with [uv](https://docs.astral.sh/uv/)
- Node.js 18+
- Chrome browser
- OpenAI API key
- Google Cloud project with Gmail API enabled (see below)
- LangSmith account (optional, for tracing)

---

### 1. Clone the repo

```bash
git clone https://github.com/Excergic/access-web-with-voice.git
cd access-web-with-voice
```

---

### 2. Server setup

```bash
cd server
```

Create `.env`:

```env
OPENAI_API_KEY=sk-...

# Optional — LangSmith tracing
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=lsv2_...
LANGCHAIN_PROJECT=voice-action-browser
```

Install dependencies:

```bash
uv sync
```

---

### 3. Gmail API credentials

You need a Google Cloud project with the Gmail API enabled and OAuth2 Desktop credentials.

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → Enable **Gmail API**
3. Go to **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
4. Application type: **Desktop app**
5. Download the JSON → save as `server/credentials.json`
6. Go to **OAuth consent screen** → set to **External** → add your Gmail address as a test user
7. Run the one-time auth flow:

```bash
cd server
uv run python auth_gmail.py
```

This opens a browser, asks you to sign in and grant permission, then saves `server/token.json`. You only do this once.

---

### 4. Extension setup

```bash
cd extension
npm install
npm run build
```

This compiles TypeScript to `extension/dist/`.

Load into Chrome:

1. Open `chrome://extensions`
2. Enable **Developer mode** (top right)
3. Click **Load unpacked** → select the `extension/` folder
4. Open [Gmail](https://mail.google.com) and refresh the tab

---

## Usage

### Start the server

```bash
cd server
uv run uvicorn main:app --port 8000 --log-level info
```

### Use the agent

1. Open Gmail and click on any email to open it
2. Press **`Alt+Space`** — the overlay shows "Connecting..." then "Recording"
3. Speak your command (examples below)
4. Press **`Alt+Space`** again to stop recording
5. Wait for transcription → agent reasons → draft appears in overlay
6. Click **Send** to confirm or **Cancel** to abort

### Example commands

| What you say | What happens |
|---|---|
| "Reply to this email saying Tuesday works for me" | Drafts reply with exactly that text, asks to confirm |
| "Reply to this email" | Agent reads the email body and composes a contextual reply, shows draft, asks to confirm |
| "Reply saying thanks, I'll get back to you soon" | Drafts that reply, asks to confirm |

### Overlay states

| Color | Meaning |
|---|---|
| Blue | Connecting / transcribing / heard text |
| Yellow | Agent is planning or executing |
| Yellow border in draft | Agent composed the reply autonomously |
| Blue border in draft | You dictated the reply content |
| Green | Done |
| Red | Error or cancelled |

---

## Development

### Rebuild extension after changes

```bash
cd extension && npm run build
```

Then go to `chrome://extensions` → click the reload icon → refresh the Gmail tab.

### Restart server

```bash
# Kill existing server if running
lsof -ti :8000 | xargs kill -9

cd server && uv run uvicorn main:app --port 8000 --log-level info
```

### View agent traces

If LangSmith is configured, every agent run is traced at [smith.langchain.com](https://smith.langchain.com). You can see each node's input/output, LLM calls, and the full state at every step.

---

## Agent Architecture

```
START
  └─► grounding          — resolves action intent + reply body from transcript + email context
        └─► planner      — creates ordered list of steps (draft_reply, send_email, etc.)
              └─► router — assigns execution rail (api / dom) to each step
                    └─► executor ──────────────────────────────────────────────┐
                          │                                                     │
                          ├─ next step reversible? ──► executor (loop)         │
                          │                                                     │
                          ├─ next step irreversible + not confirmed?            │
                          │     └─► confirmation_gate                          │
                          │           ├─ user confirmed ──► executor ──────────┘
                          │           └─ user cancelled ──► response_formatter
                          │
                          ├─ last step failed? ──► response_formatter
                          └─ all steps done? ──► response_formatter
                                                        └─► END
```

**Key design decisions:**
- Agent uses `interrupt()` to freeze mid-graph and wait for user confirmation before any irreversible action (sending email)
- Grounding LLM composes a reply autonomously when the user only states intent ("reply to this") with no explicit content
- Gmail API thread ID is resolved by subject search — the URL hash format is incompatible with the API
- Idempotency keys prevent duplicate API calls if a step is retried within a session

---

## Security Notes

- `server/.env`, `server/credentials.json`, and `server/token.json` are gitignored — never commit them
- The WebSocket server runs locally only (`localhost:8000`) — no external exposure
- Gmail OAuth scopes: `gmail.compose` + `gmail.readonly` — no delete or admin access

---

## Roadmap

- [ ] Recovery node — re-plan failed steps, max 2 retries
- [ ] Persistent session state (Postgres checkpointer instead of in-memory)
- [ ] More Gmail actions: archive, label, forward, search
- [ ] Support for other email clients and web apps beyond Gmail
