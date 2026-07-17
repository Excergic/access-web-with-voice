"""
LangGraph graph assembly.
"""

import logging

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .nodes import (
    confirmation_gate,
    executor,
    grounding,
    planner,
    response_formatter,
    router,
)
from .state import AgentState

log = logging.getLogger(__name__)


# ── Conditional edge logic ────────────────────────────────────────────────────

def _after_executor(state: AgentState) -> str:
    """
    After each executor step decide what comes next:
      - Last step failed                     → response_formatter (skip confirmation)
      - More steps + next is reversible      → loop back to executor
      - More steps + next is irreversible
          and not yet confirmed              → confirmation_gate
      - More steps + next is irreversible
          and already confirmed              → executor (continue)
      - No more steps                        → response_formatter
    """
    # If the step that just ran failed, skip confirmation and report immediately
    results = state.get("step_results", [])
    if results and results[-1].status == "failed":
        log.info("[_after_executor] last step failed — routing to response_formatter")
        return "response_formatter"

    plan  = state["plan"]
    index = state["step_index"]

    if plan is None or index >= len(plan.steps):
        return "response_formatter"

    next_step = plan.steps[index]

    if not next_step.reversible and not state.get("confirmed", False):
        return "confirmation_gate"

    return "executor"


def _after_gate(state: AgentState) -> str:
    """After confirmation gate: proceed if confirmed, else format and exit."""
    if state.get("confirmed", False):
        return "executor"
    return "response_formatter"


# ── Build graph ───────────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    g = StateGraph(AgentState)

    g.add_node("grounding",          grounding)
    g.add_node("planner",            planner)
    g.add_node("router",             router)
    g.add_node("executor",           executor)
    g.add_node("confirmation_gate",  confirmation_gate)
    g.add_node("response_formatter", response_formatter)

    g.add_edge(START,          "grounding")
    g.add_edge("grounding",    "planner")
    g.add_edge("planner",      "router")
    g.add_edge("router",       "executor")

    g.add_conditional_edges("executor", _after_executor, {
        "executor":           "executor",
        "confirmation_gate":  "confirmation_gate",
        "response_formatter": "response_formatter",
    })

    g.add_conditional_edges("confirmation_gate", _after_gate, {
        "executor":           "executor",
        "response_formatter": "response_formatter",
    })

    g.add_edge("response_formatter", END)

    checkpointer = MemorySaver()
    return g.compile(checkpointer=checkpointer)


# Singleton compiled graph — imported by main.py
agent_graph = build_graph()
