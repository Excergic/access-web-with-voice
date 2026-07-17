"""
Phase 4 — FastAPI WebSocket server.
Pipeline: audio → Whisper STT → LangGraph agent → WebSocket responses.
The WebSocket stays open for the entire session to support the confirmation gate.

Observability:
  - LangSmith: automatic tracing of every LangGraph node + LLM call.
    Set LANGCHAIN_TRACING_V2=true, LANGCHAIN_API_KEY, LANGCHAIN_PROJECT in .env.
    Zero code changes required — LangChain reads the env vars automatically.
"""

import io
import json
import logging
import os
import uuid
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from langgraph.types import Command
from openai import AsyncOpenAI

from agent.graph import agent_graph
from agent.state import AgentState

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

app = FastAPI(title="Voice Action Browser", version="0.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

openai = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


# ── STT ───────────────────────────────────────────────────────────────────────

async def transcribe(audio_chunks: list[bytes]) -> str:
    audio_bytes = b"".join(audio_chunks)
    log.info("Transcribing | total_bytes=%d", len(audio_bytes))
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = "audio.webm"
    result = await openai.audio.transcriptions.create(
        model="whisper-1",
        file=audio_file,
        response_format="text",
    )
    return result.strip()


# ── Graph runner ──────────────────────────────────────────────────────────────

async def run_graph(
    ws: WebSocket,
    session_id: str,
    screen_context: dict,
    transcript: str,
) -> None:
    """
    Invoke the agent graph. Handles the confirmation interrupt loop:
    1. Initial invoke → graph runs until interrupt (or completion)
    2. If interrupted → send confirmation request to extension
    3. Wait for user response over WebSocket
    4. Resume graph with Command(resume=...)
    """
    config = {"configurable": {"thread_id": session_id}}

    initial_state: AgentState = {
        "session_id":       session_id,
        "screen_context":   screen_context,
        "transcript":       transcript,
        "grounded_target":  None,
        "plan":             None,
        "step_index":       0,
        "step_results":     [],
        "confirmed":        False,
        "user_response":    None,
        "artifacts":        {},
        "summary":          None,
        "error":            None,
    }

    # Notify client the agent is thinking
    await ws.send_json({"type": "agent_status", "status": "planning"})

    # ── First invoke ──────────────────────────────────────────────────────────
    await agent_graph.ainvoke(initial_state, config=config)

    # ── Confirmation loop ─────────────────────────────────────────────────────
    # Keep resuming as long as the graph is paused at an interrupt
    while True:
        graph_state = await agent_graph.aget_state(config)

        # No pending tasks → graph finished
        if not graph_state.tasks:
            break

        # Collect interrupt payloads (one per interrupted node)
        interrupts = [
            iv
            for task in graph_state.tasks
            for iv in task.interrupts
        ]
        if not interrupts:
            break

        # Send the first interrupt payload to the extension
        interrupt_value: dict = interrupts[0].value
        await ws.send_json(interrupt_value)
        log.info("Sent confirmation request to client")

        # Wait for the user's confirmation response.
        # Skip any lingering binary audio chunks that arrive after end_of_audio
        # (MediaRecorder fires one final ondataavailable asynchronously after stop()).
        while True:
            raw = await ws.receive()
            if "text" in raw:
                break
            log.debug("Skipping lingering binary chunk during confirmation wait (%d bytes)",
                      len(raw.get("bytes", b"")))

        user_msg: dict = json.loads(raw["text"])
        if user_msg.get("type") != "confirmation_response":
            log.warning("Unexpected message type: %s", user_msg.get("type"))
            break

        confirmed = bool(user_msg.get("confirmed", False))
        log.info("User confirmed=%s", confirmed)

        # Resume graph
        await agent_graph.ainvoke(
            Command(resume={"confirmed": confirmed}),
            config=config,
        )

    # ── Send final result ─────────────────────────────────────────────────────
    final = await agent_graph.aget_state(config)
    final_values: AgentState = final.values  # type: ignore[assignment]

    step_results = final_values.get("step_results", [])
    summary = final_values.get("summary") or (
        "Done: " + ", ".join(r.action for r in step_results if r.status == "succeeded") + "."
        if step_results else "No actions taken."
    )

    await ws.send_json({
        "type":    "agent_result",
        "summary": summary,
        "steps":   [r.model_dump() for r in step_results],
        "plan":    final_values["plan"].model_dump() if final_values.get("plan") else None,
    })


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    session_id = str(uuid.uuid4())
    log.info("Session started | id=%s", session_id)

    screen_context: dict | None = None
    audio_chunks: list[bytes] = []

    try:
        while True:
            message = await ws.receive()

            if "text" in message:
                payload: dict = json.loads(message["text"])
                msg_type = payload.get("type")

                if msg_type == "context":
                    screen_context = payload.get("data", {})
                    log.info("Context | subject=%r | from=%r",
                             screen_context.get("subject"),
                             screen_context.get("sender"))
                    await ws.send_json({"type": "context_ack"})

                elif msg_type == "end_of_audio":
                    if not audio_chunks:
                        await ws.send_json({"type": "error", "text": "No audio received."})
                        continue
                    if not screen_context:
                        await ws.send_json({"type": "error", "text": "No screen context."})
                        continue

                    try:
                        transcript = await transcribe(audio_chunks)
                        log.info("Transcript: %r", transcript)
                        await ws.send_json({"type": "transcript", "text": transcript})

                        await run_graph(ws, session_id, screen_context, transcript)

                    except Exception as exc:
                        log.exception("Agent error: %s", exc)
                        await ws.send_json({"type": "error", "text": str(exc)})

                    audio_chunks = []

            elif "bytes" in message:
                audio_chunks.append(message["bytes"])

    except WebSocketDisconnect:
        log.info("Session ended | id=%s", session_id)
    except RuntimeError as exc:
        if "disconnect" in str(exc).lower():
            log.info("Session ended | id=%s", session_id)
        else:
            log.exception("WebSocket error: %s", exc)
    except Exception as exc:
        log.exception("WebSocket error: %s", exc)
        await ws.close(code=1011)
