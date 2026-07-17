# Hybrid Voice Browser Agent --- Version 1 (Execution Spec)

## Goal

Build a **simple but complete** hybrid voice browser agent.

The agent should:

1.  Read the currently open Gmail email.
2.  Listen to the user's voice.
3.  Convert speech to text.
4.  Understand the request using the current email as context.
5.  Generate an execution plan.
6.  Execute reversible actions immediately.
7.  Ask for confirmation before irreversible actions.
8.  Execute confirmed actions.
9.  Store execution state to prevent duplicate actions.

**Target workflow**

> "Reply to this email saying Tuesday works and send it."

------------------------------------------------------------------------

# Architecture

``` text
User
 │
 │ Press Hotkey
 ▼
Chrome Extension
 ├── Read Gmail DOM
 ├── Capture microphone
 ├── Show action log
 └── WebSocket
          │
          ▼
FastAPI + LangGraph Agent
 ├── STT
 ├── Grounding
 ├── Planner
 ├── Router
 ├── Confirmation Gate
 ├── API Rail (MCP)
 ├── Memory
 └── Recovery
          │
          ▼
 Gmail API (MCP)
```

------------------------------------------------------------------------

# Step 1 --- User opens Gmail

Read only the currently opened email.

Extract:

``` python
ScreenContext(
    app="gmail",
    sender="supplier@company.com",
    subject="Delivery Date",
    body_snippet="Can Tuesday work?",
    url="https://mail.google.com"
)
```

Do **not** send the entire DOM to the LLM.

------------------------------------------------------------------------

# Step 2 --- Hotkey

User presses a push-to-talk hotkey.

Extension:

-   starts microphone
-   reads ScreenContext
-   opens WebSocket
-   streams audio and context

------------------------------------------------------------------------

# Step 3 --- Speech to Text

Convert audio into:

``` python
Transcript(
    text="Reply saying Tuesday works and send it.",
    confidence=0.97,
    latency_ms=420
)
```

------------------------------------------------------------------------

# Step 4 --- Grounding

Input:

-   Transcript
-   ScreenContext

Resolve references like:

-   "this email"
-   "reply to this"

Output:

``` text
Target = Current Gmail thread
```

------------------------------------------------------------------------

# Step 5 --- Planning

Create a structured plan.

``` python
Plan(
  steps=[
    PlanStep(
      action="draft_reply",
      reversible=True,
      rail="api"
    ),
    PlanStep(
      action="send_email",
      reversible=False,
      rail="api"
    )
  ],
  needs_confirmation=True
)
```

The LLM plans only.

The executor performs actions.

------------------------------------------------------------------------

# Step 6 --- Router

Choose execution rail.

Rules:

-   Prefer MCP/API whenever available.
-   Use Playwright only when no API exists.

Examples:

  Action            Rail
  ----------------- ------
  Draft Gmail       API
  Send Gmail        API
  Supplier Portal   DOM

------------------------------------------------------------------------

# Step 7 --- Execute Reversible Steps

Immediately execute:

``` text
draft_reply
```

Result:

Draft exists in Gmail.

Do NOT send.

------------------------------------------------------------------------

# Step 8 --- Confirmation Gate

If any step is irreversible:

Ask once.

Example:

> "I've drafted the reply. Send it?"

Possible responses:

-   Yes
-   No
-   Change Wednesday
-   Cancel

------------------------------------------------------------------------

# Step 9 --- Handle Changes

If user changes content:

Example:

> "Change Tuesday to Wednesday."

Update draft.

Re-plan only if necessary.

Do not restart completed work.

------------------------------------------------------------------------

# Step 10 --- Execute Irreversible Steps

Only after confirmation.

Execute:

``` text
send_email
```

via Gmail MCP.

------------------------------------------------------------------------

# Step 11 --- Memory

Track execution state.

``` text
draft_reply   ✓ succeeded
send_email    ✓ succeeded
```

Never repeat a succeeded irreversible action.

------------------------------------------------------------------------

# Step 12 --- Recovery

If a step fails:

1.  Re-perceive context.
2.  Re-plan only the failed step.
3.  Retry at most two times.
4.  Stop and report.

Never repeat successful steps.

------------------------------------------------------------------------

# Data Models

``` python
ScreenContext
Transcript
PlanStep
Plan
StepResult
```

------------------------------------------------------------------------

# Project Structure

``` text
extension/
server/
    agent/
    router/
    rails/
        mcp/
        dom/
    voice/
evals/
```

------------------------------------------------------------------------

# Version 1 Scope

Included:

-   Gmail
-   Voice input
-   STT
-   LangGraph planner
-   Gmail MCP
-   Confirmation gate
-   Session memory
-   Action log

Excluded:

-   Wake word
-   Vision
-   Multi-site automation
-   Long-term memory
-   Autonomous execution

------------------------------------------------------------------------

# Acceptance Criteria

-   Read Gmail context.
-   Convert speech to text.
-   Produce a correct execution plan.
-   Draft reply automatically.
-   Ask once before sending.
-   Send only after confirmation.
-   Never send twice.
-   Log every action.
-   Recover safely from failures.

------------------------------------------------------------------------

# Guiding Principles

1.  API first.
2.  DOM fallback only when required.
3.  Confirmation before irreversible actions.
4.  Idempotent execution using memory.
5.  Bounded retries.
6.  Measure latency and task success from day one.
