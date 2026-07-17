"""
LangGraph node functions.
Each function receives AgentState and returns a partial state update dict.
"""

import hashlib
import logging
from functools import lru_cache

from rails.api.gmail import draft_reply as gmail_draft_reply
from rails.api.gmail import send_draft as gmail_send_draft

from langchain_openai import ChatOpenAI
from langgraph.types import interrupt

from .state import (
    AgentState,
    GroundedTarget,
    Plan,
    Rail,
    ScreenContext,
    StepResult,
)

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_llm() -> ChatOpenAI:
    return ChatOpenAI(model="gpt-4o", temperature=0)

def _grounding_llm():
    return _get_llm().with_structured_output(GroundedTarget, method="function_calling")

def _planner_llm():
    return _get_llm().with_structured_output(Plan, method="function_calling")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _idempotency_key(session_id: str, action: str, index: int) -> str:
    raw = f"{session_id}:{action}:{index}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _rail_for(action: str) -> Rail:
    """Rule-based rail selection: prefer API, fall back to DOM."""
    api_actions = {"draft_reply", "send_email", "forward_email", "archive", "label", "search"}
    return "api" if action in api_actions else "dom"


# ── Node 1: Grounding ─────────────────────────────────────────────────────────

async def grounding(state: AgentState) -> dict:
    log.info("[grounding] resolving references in transcript")

    ctx = ScreenContext(**state["screen_context"])

    prompt = f"""You are a Gmail voice assistant resolving a user's voice command against the currently open email.

Email context:
  Subject: {ctx.subject}
  From: {ctx.sender}
  Thread ID: {ctx.thread_id or "unknown"}
  Body:
{ctx.body_snippet[:600]}

User said: "{state['transcript']}"

Your job:
1. Resolve the action intent (e.g. reply_and_send, reply_only, forward, archive).
2. Resolve the thread_id from context. If unknown, use "unknown".
3. Determine reply_body using these rules:
   - If the user explicitly dictated reply content (e.g. "reply saying Tuesday works"), extract exactly what they said to send.
   - If the user only expressed intent with no content (e.g. "reply to this email", "reply to this"), compose a professional and contextually appropriate reply based on the email body above. Set auto_composed=true.
   - If the action does not involve replying, set reply_body=null.
4. Set auto_composed=true ONLY if you wrote the reply yourself from the email context. Set false if you extracted it from what the user said.
"""

    result: GroundedTarget = await _grounding_llm().ainvoke(prompt)
    log.info(
        "[grounding] intent=%r auto_composed=%s reply_body=%r",
        result.action_intent, result.auto_composed,
        (result.reply_body or "")[:80]
    )
    return {"grounded_target": result}


# ── Node 2: Planner ───────────────────────────────────────────────────────────

async def planner(state: AgentState) -> dict:
    log.info("[planner] creating execution plan")

    target = state["grounded_target"]
    ctx    = ScreenContext(**state["screen_context"])

    prompt = f"""You are planning Gmail actions for a voice command.

Email context:
  Subject: {ctx.subject}
  From: {ctx.sender}
  Thread ID: {target.thread_id}
  Body snippet: {ctx.body_snippet[:400]}

Grounded intent: {target.action_intent}
Reply body (if applicable): {target.reply_body or "N/A"}
User said: "{state['transcript']}"

Create an ordered list of steps to fulfil the request.
Available actions: draft_reply, send_email, forward_email, archive, label, search.
Mark reversible=true only for actions that can be undone (drafting).
Mark reversible=false for actions that cannot be undone (sending, deleting).
Set needs_confirmation=true if any step is irreversible.

For each step include relevant params, for example:
  draft_reply → {{"thread_id": "...", "body": "..."}}
  send_email  → {{"thread_id": "...", "draft_id": "tbd"}}
"""

    result: Plan = await _planner_llm().ainvoke(prompt)

    # Assign idempotency keys
    for i, step in enumerate(result.steps):
        step.idempotency_key = _idempotency_key(state["session_id"], step.action, i)

    log.info("[planner] steps=%s needs_confirmation=%s",
             [s.action for s in result.steps], result.needs_confirmation)
    return {"plan": result}


# ── Node 3: Router ────────────────────────────────────────────────────────────

def router(state: AgentState) -> dict:
    """Rule-based: assign execution rail to each step."""
    log.info("[router] assigning rails")
    plan = state["plan"]
    assert plan is not None

    for step in plan.steps:
        step.rail = _rail_for(step.action)

    log.info("[router] rails=%s", [(s.action, s.rail) for s in plan.steps])
    return {"plan": plan, "step_index": 0, "confirmed": False}


# ── Node 4: Executor (one step at a time) ────────────────────────────────────

async def executor(state: AgentState) -> dict:
    plan      = state["plan"]
    assert plan is not None
    index     = state["step_index"]
    step      = plan.steps[index]
    artifacts = dict(state.get("artifacts", {}))

    # Idempotency check: skip if this key already succeeded in this session
    succeeded_keys: set = artifacts.get("succeeded_keys", set())
    if step.idempotency_key and step.idempotency_key in succeeded_keys:
        log.info("[executor] step=%r idempotency_key=%r already succeeded — skipping",
                 step.action, step.idempotency_key)
        cached = StepResult(action=step.action, status="succeeded", detail="[idempotent — skipped duplicate]")
        return {"step_results": [cached], "step_index": index + 1, "artifacts": artifacts}

    log.info("[executor] step=%d action=%r rail=%r reversible=%s",
             index, step.action, step.rail, step.reversible)

    try:
        new_artifacts, detail = await _dispatch(step, artifacts, state)
        result = StepResult(action=step.action, status="succeeded", detail=detail)
        artifacts.update(new_artifacts)
        # Record succeeded idempotency key
        if step.idempotency_key:
            keys = set(artifacts.get("succeeded_keys", set()))
            keys.add(step.idempotency_key)
            artifacts["succeeded_keys"] = keys
    except Exception as exc:
        log.exception("[executor] step=%r failed: %s", step.action, exc)
        result = StepResult(action=step.action, status="failed", detail=str(exc))

    log.info("[executor] %s → %s", step.action, result.status)
    return {
        "step_results": [result],
        "step_index":   index + 1,
        "artifacts":    artifacts,
    }


async def _dispatch(step, artifacts: dict, state: AgentState) -> tuple[dict, str]:
    """
    Route a plan step to its real implementation.
    Returns (new_artifacts, detail_string).
    """
    target  = state.get("grounded_target")
    subject = state.get("screen_context", {}).get("subject", "")

    if step.action == "draft_reply":
        body = (target.reply_body if target else None) or step.params.get("body", "")
        if not body:
            raise ValueError("No reply body available — check grounding output")
        # Pass subject so gmail.py can resolve the real API thread ID via search
        res = await gmail_draft_reply(subject=subject, body=body)
        return {"draft_id": res["draft_id"]}, f"Draft created (id={res['draft_id']})"

    if step.action == "send_email":
        draft_id = artifacts.get("draft_id") or step.params.get("draft_id", "")
        if not draft_id:
            raise ValueError("No draft_id found — draft_reply must run before send_email")
        res = await gmail_send_draft(draft_id=draft_id)
        return {}, f"Email sent (message_id={res['message_id']})"

    # Fallback for actions not yet implemented
    log.warning("[executor] no implementation for action=%r — skipping", step.action)
    return {}, f"[not implemented] {step.action}"


# ── Node 5: Confirmation gate ─────────────────────────────────────────────────

def confirmation_gate(state: AgentState) -> dict:
    plan   = state["plan"]
    assert plan is not None
    target = state.get("grounded_target")

    irreversible = [s for s in plan.steps if not s.reversible]
    log.info("[confirmation_gate] waiting for user — irreversible steps: %s",
             [s.action for s in irreversible])

    done      = [r for r in state["step_results"] if r.status == "succeeded"]
    done_text = ", ".join(r.action for r in done) if done else "nothing yet"

    # Include reply body so the user can verify what will be sent.
    # Mark auto_composed so the overlay can flag it clearly.
    reply_body    = target.reply_body    if target else None
    auto_composed = target.auto_composed if target else False

    if reply_body and auto_composed:
        message = f"I composed this reply:\n\n\"{reply_body}\"\n\nSend it?"
    elif reply_body:
        message = f"Ready to send your reply:\n\n\"{reply_body}\"\n\nConfirm?"
    else:
        message = f"Done: {done_text}. Proceed with: {', '.join(s.action for s in irreversible)}?"

    user_response: dict = interrupt({
        "type":          "confirmation_required",
        "message":       message,
        "reply_body":    reply_body,
        "auto_composed": auto_composed,
        "completed_steps": [r.model_dump() for r in done],
        "pending_steps":   [s.model_dump() for s in irreversible],
    })

    confirmed = bool(user_response.get("confirmed", False))
    log.info("[confirmation_gate] user confirmed=%s", confirmed)
    return {"confirmed": confirmed, "user_response": "yes" if confirmed else "no"}


# ── Node 6: Response formatter ────────────────────────────────────────────────

def response_formatter(state: AgentState) -> dict:
    results = state["step_results"]
    succeeded = [r for r in results if r.status == "succeeded"]
    failed    = [r for r in results if r.status == "failed"]

    parts = []
    if succeeded:
        parts.append(f"Done: {', '.join(r.action for r in succeeded)}.")
    if failed:
        parts.append(f"Failed: {', '.join(r.action for r in failed)}.")
    if not state.get("confirmed") and state.get("user_response") == "no":
        parts.append("Cancelled by user.")

    summary = " ".join(parts) or "No actions taken."
    log.info("[response_formatter] %s", summary)
    return {"summary": summary}
