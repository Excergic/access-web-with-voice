# Voice Action Browser — Build Progress

## Status: Phase 6 Complete

---

## What Is Built

### Phase 1 — Chrome Extension (Gmail DOM Reader)
**Status: Done**

- Extension: `extension/` (TypeScript, compiled via esbuild to `dist/`)
- Reads the currently open Gmail email from the DOM
- Extracts: sender, subject, body snippet, thread ID, URL
- Popup button shows extracted ScreenContext
- Hotkey: `Alt+Space` toggles recording (press once to start, once to stop)

Key files:
- `extension/src/types.ts` — shared types (ScreenContext, WsInbound/Outbound)
- `extension/src/content.ts` — Gmail DOM extraction, hotkey, mic capture, WebSocket, overlay UI
- `extension/src/popup.ts` — popup "Read Current Email" button
- `extension/manifest.json` — Chrome MV3, points to `dist/`
- `extension/build.mjs` — esbuild config (IIFE bundles, no ES module issues)

Build: `cd extension && npm run build`
Reload: chrome://extensions → reload → refresh Gmail tab

---

### Phase 2 — FastAPI WebSocket Server + Push-to-Talk
**Status: Done**

- Server: `server/` (Python, uv, FastAPI + uvicorn)
- WebSocket at `ws://localhost:8000/ws`
- Extension opens WS on hotkey → sends ScreenContext → streams audio binary chunks → sends `end_of_audio`
- Server logs context + audio received
- Visual overlay on Gmail page shows recording state

Run: `cd server && uv run uvicorn main:app --port 8000 --log-level info`

---

### Phase 3 — Speech-to-Text (OpenAI Whisper)
**Status: Done**

- Audio chunks (WebM/Opus from MediaRecorder) collected server-side
- On `end_of_audio`: sent to `openai.audio.transcriptions.create(model="whisper-1")`
- Transcript sent back over WebSocket → shown in overlay as `Heard: "..."`

Config: `OPENAI_API_KEY` in `server/.env`

---

### Phase 4 — LangGraph Agent (Grounding + Planning + Confirmation Gate)
**Status: Done**

Agent graph: `server/agent/`

- `state.py` — AgentState TypedDict + Pydantic models (ScreenContext, GroundedTarget, Plan, PlanStep, StepResult)
- `nodes.py` — grounding, planner, router, executor, confirmation_gate, response_formatter
- `graph.py` — StateGraph assembled with MemorySaver checkpointer

Graph flow:
```
START -> grounding -> planner -> router -> executor (loop) -> confirmation_gate (interrupt) -> executor -> response_formatter -> END
```

Conditional edges:
- After executor: next step reversible -> loop to executor, next step irreversible + not confirmed -> confirmation_gate, all done -> response_formatter
- After confirmation_gate: confirmed -> executor, denied -> response_formatter

Confirmation gate:
- Uses LangGraph `interrupt()` — graph freezes, state checkpointed
- Server sends `{type: "confirmation_required", message, pending_steps}` over WebSocket
- Extension shows overlay with Yes/No buttons
- User clicks -> WS sends `{type: "confirmation_response", confirmed: true/false}`
- Server resumes with `graph.ainvoke(Command(resume={confirmed: ...}), config)`

WebSocket protocol (extension -> server):
- `{type: "context", data: ScreenContext}`
- `{type: "end_of_audio"}`
- `{type: "confirmation_response", confirmed: bool}`

WebSocket protocol (server -> extension):
- `{type: "context_ack"}`
- `{type: "transcript", text}`
- `{type: "agent_status", status}`
- `{type: "confirmation_required", message, completed_steps, pending_steps}`
- `{type: "agent_result", summary, steps, plan}`
- `{type: "error", text}`

LLM: `gpt-4o`, `method="function_calling"` for structured output (not json_schema — avoids additionalProperties error)

Observability: LangSmith traces every node + LLM call automatically.
Config: `LANGCHAIN_TRACING_V2=true`, `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT=voice-action-browser` in `server/.env`

---

### Phase 5 — Gmail API (Real Draft + Send)
**Status: Done**

- Rail: `server/rails/api/gmail.py`
- OAuth2 Desktop app flow: `credentials.json` + `token.json` in `server/`
- One-time auth: `uv run python auth_gmail.py`

Actions implemented:
- `draft_reply(subject, body)` — searches Gmail API by subject to resolve real thread ID (URL hash format != API thread ID), creates draft, returns `draft_id`
- `send_draft(draft_id)` — sends existing draft

Key fix: Gmail URL hash (e.g. `FMfcgz...`) is NOT the API thread ID. We resolve the real thread ID by searching Gmail API with the email subject.

Artifacts flow: `draft_id` produced by `draft_reply` is stored in `state["artifacts"]` and consumed by `send_email`.

Google Cloud setup:
- Project: `voice-action-browser`
- Gmail API enabled
- OAuth2 credentials: Desktop app type, External audience, Testing status
- Test user: dhaivat.jambudia@gmail.com
- Scopes: `gmail.compose` + `gmail.readonly`

---

## Phase 6 — Complete

### Changes made:

1. **Fix: confirmation gate no longer fires when draft_reply fails** (`server/agent/graph.py`)
   - `_after_executor` now checks `state["step_results"][-1].status == "failed"` and routes directly to `response_formatter`, bypassing the confirmation gate.

2. **Idempotency enforcement** (`server/agent/nodes.py`)
   - Before executing any step, checks `artifacts["succeeded_keys"]` set for the step's idempotency key.
   - If found: returns a cached `StepResult(status="succeeded")` immediately without re-executing.
   - After a successful step, stores the key in `artifacts["succeeded_keys"]`.

3. **`Rail` type import fix** (`server/agent/nodes.py`)
   - Added `Rail` to imports from `.state` (was referenced in `_rail_for` but not imported).

4. **Autonomous reply composition** (Phase 5 late addition, `server/agent/nodes.py` + `server/agent/state.py`)
   - Grounding node: if user says "reply to this email" with no content, LLM composes a reply from the email body. Sets `auto_composed=True`.
   - Confirmation overlay shows the draft text, color-coded: yellow "Agent composed" or blue "Your reply".

### Remaining optional items:
- Replace MemorySaver with Postgres checkpointer (`langgraph-checkpoint-postgres`)
- Recovery node: re-plan failed steps, max 2 retries

---

## Known Issues / Bugs

- LangGraph warnings about unregistered types (`GroundedTarget`, `Plan`, `StepResult`) in checkpoint — cosmetic only, add to `allowed_msgpack_modules` to suppress
- `file_cache` warning from Google API client — cosmetic, use `cache_discovery=False` in `build()` call
- Confirmation gate still shows even if `draft_reply` failed (Phase 6 fix)

---

## Project Structure

```
voice-action-browser/
  extension/
    src/
      types.ts          shared types
      content.ts        Gmail DOM + hotkey + WebSocket + overlay
      popup.ts          popup UI
      background.ts     placeholder
    dist/               compiled JS (esbuild output)
    manifest.json
    popup.html
    build.mjs           esbuild config
    package.json
  server/
    agent/
      state.py          AgentState + Pydantic models
      nodes.py          all LangGraph node functions
      graph.py          graph assembly + MemorySaver
    rails/
      api/
        gmail.py        Gmail API rail (draft + send)
    main.py             FastAPI app + WebSocket + STT + graph orchestration
    auth_gmail.py       one-time OAuth flow script
    credentials.json    Google OAuth client (do not commit)
    token.json          OAuth token (do not commit)
    .env                API keys (do not commit)
    pyproject.toml
  Hybrid_Voice_Browser_Agent_v1_Workflow.md   original spec
  PROGRESS.md                                  this file
```

---

## Environment

```
# server/.env
OPENAI_API_KEY=...
LANGCHAIN_TRACING_V2=true
LANGCHAIN_PROJECT=voice-action-browser
LANGCHAIN_API_KEY=...
```

Python: 3.14 (uv managed)
Node: 22.6.0
Chrome Extension: MV3, TypeScript, esbuild

## Commands

```bash
# Build extension
cd extension && npm run build

# Start server
cd server && uv run uvicorn main:app --port 8000 --log-level info

# One-time Gmail auth (run once after placing credentials.json in server/)
cd server && uv run python auth_gmail.py
```
