"""
Phase 4 — FastAPI WebSocket server.
Pipeline: audio → Whisper STT → LangGraph agent → WebSocket responses.
The WebSocket stays open for the entire session to support the confirmation gate.

Observability:
  - LangSmith: automatic tracing of every LangGraph node + LLM call.
    Set LANGCHAIN_TRACING_V2=true, LANGCHAIN_API_KEY, LANGCHAIN_PROJECT in .env.
    Zero code changes required — LangChain reads the env vars automatically.
"""

import base64
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
from rails.api.gmail import delete_draft as gmail_delete_draft
from rails.api.gmail import draft_reply as gmail_draft_reply

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


# ── TTS ───────────────────────────────────────────────────────────────────────

async def generate_tts(text: str) -> bytes:
    """Generate MP3 speech from text via OpenAI TTS. Returns raw MP3 bytes."""
    response = await openai.audio.speech.create(
        model="tts-1",
        voice="alloy",
        input=text,
        response_format="mp3",
    )
    return response.content


# ── Voice confirmation classifier ─────────────────────────────────────────────

def _classify_voice_response(transcript: str) -> tuple[str, str | None]:
    """
    Classify a spoken confirmation response.
    Returns ("confirm" | "cancel" | "edit", edit_instruction | None).
    """
    t = transcript.lower().strip().rstrip(".")
    words = t.split()
    first = words[0] if words else ""
    count = len(words)

    YES_EXACT = {"yes", "yeah", "yep", "yup", "send", "confirm", "go", "okay",
                 "ok", "sure", "correct", "right", "affirmative",
                 "send it", "go ahead", "do it", "yes send", "yes please"}
    NO_EXACT  = {"no", "nope", "cancel", "stop", "abort", "reject",
                 "never mind", "nevermind", "don't send", "no cancel"}
    YES_FIRST = {"yes", "yeah", "yep", "yup", "confirm", "ok", "okay", "sure"}
    NO_FIRST  = {"no", "nope", "cancel", "stop", "abort"}

    if t in YES_EXACT:
        return "confirm", None
    if t in NO_EXACT:
        return "cancel", None
    # Short phrase starting with strong signal word
    if first in YES_FIRST and count <= 4:
        return "confirm", None
    if first in NO_FIRST and count <= 4:
        return "cancel", None

    return "edit", transcript


# ── Reply body refiner ────────────────────────────────────────────────────────

async def _refine_reply_body(original: str, instruction: str) -> str:
    """Use gpt-4o to apply an edit instruction to the current reply draft."""
    response = await openai.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are editing an email reply draft. "
                    "Apply the user's edit instruction and return only the revised draft text. "
                    "No preamble, no explanation, no surrounding quotes."
                ),
            },
            {
                "role": "user",
                "content": f"Original draft:\n{original}\n\nEdit instruction: {instruction}",
            },
        ],
    )
    return response.choices[0].message.content.strip()


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
    2. If interrupted → send confirmation_required + TTS audio to extension
    3. Wait for user response: button click OR voice (yes/no/edit)
    4. Edit path: refine draft body → new Gmail draft → speak again → loop (max 3x)
    5. Resume graph with Command(resume={confirmed: bool})
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

    await ws.send_json({"type": "agent_status", "status": "planning"})
    await agent_graph.ainvoke(initial_state, config=config)

    # ── Confirmation loop ─────────────────────────────────────────────────────
    while True:
        graph_state = await agent_graph.aget_state(config)
        if not graph_state.tasks:
            break

        interrupts = [iv for task in graph_state.tasks for iv in task.interrupts]
        if not interrupts:
            break

        interrupt_value: dict = interrupts[0].value
        current_reply_body: str = interrupt_value.get("reply_body") or ""
        subject: str = screen_context.get("subject", "")

        # ── Display confirmation overlay ───────────────────────────────────────
        await ws.send_json(interrupt_value)
        log.info("Sent confirmation request to client")

        # ── Speak the confirmation message ────────────────────────────────────
        try:
            tts_bytes = await generate_tts(interrupt_value.get("message", ""))
            await ws.send_json({"type": "tts_audio", "audio_b64": base64.b64encode(tts_bytes).decode()})
            log.info("Sent TTS audio (%d bytes)", len(tts_bytes))
        except Exception as tts_err:
            log.warning("TTS generation failed (non-fatal): %s", tts_err)

        # ── Wait for user response (button click or voice confirmation) ───────
        MAX_EDITS = 3
        edit_count = 0
        confirmed: bool | None = None

        while confirmed is None:
            voice_chunks: list[bytes] = []

            # Receive loop: collects binary audio and waits for a decisive text msg
            while True:
                raw = await ws.receive()

                if "bytes" in raw:
                    voice_chunks.append(raw["bytes"])
                    continue

                if "text" not in raw:
                    continue

                msg = json.loads(raw["text"])
                msg_type = msg.get("type")

                # ── Button click path ──────────────────────────────────────────
                if msg_type == "confirmation_response":
                    confirmed = bool(msg.get("confirmed", False))
                    log.info("User confirmed=%s (button)", confirmed)
                    break

                # ── Voice recording started — discard lingering previous chunks ─
                if msg_type == "start_of_voice_confirmation":
                    voice_chunks = []
                    log.debug("Voice confirmation recording started — buffer cleared")
                    continue

                # ── Voice path ─────────────────────────────────────────────────
                if msg_type == "end_of_voice_confirmation":
                    if not voice_chunks:
                        log.warning("end_of_voice_confirmation with no audio — ignoring")
                        break

                    voice_transcript = await transcribe(voice_chunks)
                    log.info("Voice confirmation: %r", voice_transcript)
                    await ws.send_json({"type": "transcript", "text": voice_transcript})

                    action, edit_instruction = _classify_voice_response(voice_transcript)
                    log.info("Classified as: %s", action)

                    if action == "confirm":
                        confirmed = True
                        break

                    if action == "cancel":
                        confirmed = False
                        break

                    # ── Edit path ──────────────────────────────────────────────
                    if edit_count >= MAX_EDITS:
                        log.warning("Max edits (%d) reached — cancelling", MAX_EDITS)
                        confirmed = False
                        break

                    edit_count += 1
                    log.info("[edit %d/%d] %r", edit_count, MAX_EDITS, edit_instruction)
                    await ws.send_json({"type": "agent_status", "status": "executing"})

                    try:
                        new_body = await _refine_reply_body(current_reply_body, edit_instruction or "")

                        # Create new draft, delete old one
                        cur_state = await agent_graph.aget_state(config)
                        old_draft_id = cur_state.values.get("artifacts", {}).get("draft_id")
                        new_draft = await gmail_draft_reply(subject=subject, body=new_body)
                        new_draft_id = new_draft["draft_id"]

                        if old_draft_id:
                            try:
                                await gmail_delete_draft(old_draft_id)
                            except Exception as del_err:
                                log.warning("Could not delete old draft: %s", del_err)

                        # Patch graph state with new draft_id so send_email uses it
                        new_artifacts = {**cur_state.values.get("artifacts", {}), "draft_id": new_draft_id}
                        await agent_graph.aupdate_state(config, {"artifacts": new_artifacts})

                        current_reply_body = new_body

                        # Send updated confirmation overlay
                        updated_interrupt = {
                            **interrupt_value,
                            "message": f'I updated the reply:\n\n"{new_body}"\n\nShall I send it?',
                            "reply_body": new_body,
                            "auto_composed": True,
                        }
                        await ws.send_json(updated_interrupt)

                        # Speak the updated draft
                        try:
                            tts_bytes = await generate_tts(
                                f"I updated the reply: {new_body}. Shall I send it?"
                            )
                            await ws.send_json({
                                "type": "tts_audio",
                                "audio_b64": base64.b64encode(tts_bytes).decode(),
                            })
                        except Exception as tts_err:
                            log.warning("TTS failed on edit: %s", tts_err)

                    except Exception as edit_err:
                        log.exception("Edit/re-draft failed: %s", edit_err)
                        confirmed = False

                    break  # break receive loop; outer loop retries if confirmed still None

                # Stray message — skip
                log.debug("Ignoring unexpected message during confirmation: %s", msg_type)

        log.info("Final confirmed=%s", confirmed)

        # Resume graph
        await agent_graph.ainvoke(
            Command(resume={"confirmed": bool(confirmed)}),
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

    # Speak the result
    try:
        tts_bytes = await generate_tts(summary)
        await ws.send_json({"type": "tts_audio", "audio_b64": base64.b64encode(tts_bytes).decode()})
    except Exception as tts_err:
        log.warning("TTS failed for result: %s", tts_err)

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
