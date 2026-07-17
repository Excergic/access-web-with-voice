// Shared types across content script, popup, and background

export interface ScreenContext {
  app: "gmail";
  sender: string;
  sender_name: string;
  sender_email: string;
  subject: string;
  body_snippet: string;
  thread_id: string | null;
  url: string;
  extracted_at: string;
}

export interface ScreenContextError {
  error: string;
}

export type ScreenContextResult = ScreenContext | ScreenContextError;

export interface Message {
  type: "GET_SCREEN_CONTEXT";
}

// ── WebSocket: extension → server ────────────────────────────────────────────

export interface WsContextMessage {
  type: "context";
  data: ScreenContext;
}

export interface WsEndOfAudioMessage {
  type: "end_of_audio";
}

export interface WsConfirmationResponse {
  type: "confirmation_response";
  confirmed: boolean;
}

export type WsOutbound = WsContextMessage | WsEndOfAudioMessage | WsConfirmationResponse;

// ── WebSocket: server → extension ────────────────────────────────────────────

export interface WsContextAck {
  type: "context_ack";
}

export interface WsTranscript {
  type: "transcript";
  text: string;
}

export interface WsAgentStatus {
  type: "agent_status";
  status: "planning" | "executing" | "done";
}

export interface WsConfirmationRequired {
  type: "confirmation_required";
  message: string;
  reply_body: string | null;        // actual text that will be sent
  auto_composed: boolean;           // true = agent wrote it, false = user dictated it
  completed_steps: Array<{ action: string; status: string; detail: string }>;
  pending_steps: Array<{ action: string; reversible: boolean; rail: string; params: Record<string, unknown> }>;
}

export interface WsAgentResult {
  type: "agent_result";
  summary: string;
  steps: Array<{ action: string; status: string; detail: string }>;
  plan: unknown;
}

export interface WsError {
  type: "error";
  text: string;
}

export type WsInbound =
  | WsContextAck
  | WsTranscript
  | WsAgentStatus
  | WsConfirmationRequired
  | WsAgentResult
  | WsError;
