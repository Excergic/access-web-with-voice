"""
Shared data models and AgentState for the LangGraph agent.
"""

import operator
from typing import Annotated, Literal
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


# ── Gmail / screen context (mirrors extension types) ─────────────────────────

class ScreenContext(BaseModel):
    app: Literal["gmail"]
    sender: str
    sender_name: str
    sender_email: str
    subject: str
    body_snippet: str
    thread_id: str | None
    url: str
    extracted_at: str


# ── Grounding output ──────────────────────────────────────────────────────────

class GroundedTarget(BaseModel):
    thread_id: str = Field(description="Gmail thread ID resolved from context")
    action_intent: str = Field(description="Short label, e.g. 'reply_and_send', 'forward', 'archive'")
    reply_body: str | None = Field(
        default=None,
        description=(
            "The reply text to send. "
            "If the user specified content ('reply saying Tuesday works'), extract it. "
            "If the user only said 'reply to this' with no content, compose an appropriate "
            "reply based on the email body and tone."
        )
    )
    auto_composed: bool = Field(
        default=False,
        description="True if reply_body was composed by the agent from email context, not dictated by the user"
    )
    reasoning: str = Field(description="One sentence explaining how references were resolved")


# ── Plan ──────────────────────────────────────────────────────────────────────

Rail = Literal["api", "dom"]
ActionName = Literal["draft_reply", "send_email", "forward_email", "archive", "label", "search"]


class PlanStep(BaseModel):
    action: ActionName = Field(description="The action to perform")
    reversible: bool = Field(description="True if the action can be undone")
    rail: Rail = Field(description="Execution rail: 'api' for Gmail API, 'dom' for Playwright")
    params: dict = Field(default_factory=dict, description="Action-specific parameters")
    idempotency_key: str = Field(default="", description="Unique key to prevent duplicate execution")


class Plan(BaseModel):
    steps: list[PlanStep] = Field(description="Ordered list of steps to execute")
    needs_confirmation: bool = Field(description="True if any step is irreversible")
    summary: str = Field(description="One sentence plan summary shown to user")


# ── Step result ───────────────────────────────────────────────────────────────

class StepResult(BaseModel):
    action: str
    status: Literal["succeeded", "failed", "skipped"]
    detail: str = ""


# ── Agent state ───────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    # ── inputs ──
    session_id: str
    screen_context: dict          # raw dict from extension, parsed into ScreenContext in nodes
    transcript: str

    # ── grounding ──
    grounded_target: GroundedTarget | None

    # ── plan ──
    plan: Plan | None
    step_index: int               # index of next step to execute

    # ── execution ──
    step_results: Annotated[list[StepResult], operator.add]

    # ── confirmation gate ──
    confirmed: bool
    user_response: str | None     # "yes" | "no" | change text

    # ── inter-step artifacts (e.g. draft_id produced by draft_reply) ──
    artifacts: dict

    # ── output ──
    summary: str | None
    error: str | None
