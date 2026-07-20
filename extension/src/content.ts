// content.ts — injected into mail.google.com
// Phase 4: reads Gmail context, push-to-talk, streams audio, handles agent plan + confirmation.

import type {
  Message,
  ScreenContext,
  ScreenContextResult,
  WsConfirmationRequired,
  WsInbound,
  WsOutbound,
  WsTtsAudio,
} from "./types";

const WS_URL = "ws://localhost:8000/ws";
const HOTKEY = { altKey: true, code: "Space" };

// ── DOM extraction ────────────────────────────────────────────────────────────

function extractScreenContext(): ScreenContextResult {
  const subjectEl = document.querySelector<HTMLElement>("h2.hP");
  if (!subjectEl) {
    return { error: "No email open. Click on an email first." };
  }

  const subject = subjectEl.textContent?.trim() ?? "";

  const senderEls = document.querySelectorAll<HTMLElement>(".gD");
  const senderEl  = senderEls.length > 0 ? senderEls[senderEls.length - 1] : null;
  const senderName  = senderEl?.getAttribute("name")  ?? senderEl?.textContent?.trim() ?? "";
  const senderEmail = senderEl?.getAttribute("email") ?? "";
  const sender      = senderEmail ? `${senderName} <${senderEmail}>` : senderName;

  const bodyEls = document.querySelectorAll<HTMLElement>(".a3s");
  let bodySnippet = "";
  if (bodyEls.length > 0) {
    bodySnippet = (bodyEls[bodyEls.length - 1].innerText ?? "").trim().slice(0, 600);
  }

  const threadMatch = window.location.hash.match(/[/#]([A-Za-z0-9_\-]{10,})/);
  const threadId = threadMatch ? threadMatch[1] : null;

  const ctx: ScreenContext = {
    app: "gmail",
    sender,
    sender_name: senderName,
    sender_email: senderEmail,
    subject,
    body_snippet: bodySnippet,
    thread_id: threadId,
    url: window.location.href,
    extracted_at: new Date().toISOString(),
  };

  return ctx;
}

// ── Overlay ───────────────────────────────────────────────────────────────────

function getOrCreateOverlay(): HTMLElement {
  let el = document.getElementById("vab-overlay");
  if (el) return el;

  el = document.createElement("div");
  el.id = "vab-overlay";
  el.style.cssText = `
    position: fixed;
    bottom: 24px;
    right: 24px;
    z-index: 999999;
    background: #1e1e2e;
    color: #cdd6f4;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 13px;
    padding: 12px 16px;
    border-radius: 10px;
    border: 1px solid #cba6f7;
    box-shadow: 0 4px 20px rgba(0,0,0,0.4);
    display: none;
    min-width: 260px;
    max-width: 380px;
  `;
  document.body.appendChild(el);
  return el;
}

function showOverlay(text: string, color = "#cdd6f4"): void {
  const el = getOrCreateOverlay();
  el.style.display = "block";
  el.style.color = color;
  el.innerHTML = `<div>${escHtml(text)}</div>`;
}

function showConfirmation(payload: WsConfirmationRequired): void {
  const el = getOrCreateOverlay();
  el.style.display = "block";
  el.style.color = "#cdd6f4";

  // Show the draft text if available
  const draftSection = payload.reply_body
    ? `<div style="
        margin-bottom:10px;
        background:#313244;
        border-left:3px solid ${payload.auto_composed ? "#f9e2af" : "#89b4fa"};
        padding:8px 10px;
        border-radius:4px;
        font-size:12px;
        line-height:1.5;
      ">
        <div style="
          font-size:10px;
          color:${payload.auto_composed ? "#f9e2af" : "#89b4fa"};
          font-weight:600;
          margin-bottom:4px;
          text-transform:uppercase;
          letter-spacing:0.05em;
        ">
          ${payload.auto_composed ? "Agent composed" : "Your reply"}
        </div>
        <div style="color:#cdd6f4">${escHtml(payload.reply_body)}</div>
      </div>`
    : "";

  el.innerHTML = `
    <div style="margin-bottom:8px;color:#cba6f7;font-weight:600">Confirm Action</div>
    ${draftSection}
    <div style="margin-bottom:10px;font-size:12px;color:#a6adc8">
      ${escHtml(payload.auto_composed ? "Send this reply?" : "Confirm and send?")}
    </div>
    <div style="display:flex;gap:8px;margin-bottom:10px">
      <button id="vab-yes" style="
        flex:1;padding:6px;background:#a6e3a1;color:#1e1e2e;
        border:none;border-radius:6px;font-weight:600;cursor:pointer">
        Send
      </button>
      <button id="vab-no" style="
        flex:1;padding:6px;background:#f38ba8;color:#1e1e2e;
        border:none;border-radius:6px;font-weight:600;cursor:pointer">
        Cancel
      </button>
    </div>
    <div id="vab-voice-hint" style="
      font-size:11px;color:#a6adc8;
      border-top:1px solid #313244;padding-top:8px;
    ">🔊 Playing audio...</div>
  `;
}

function hideOverlay(): void {
  const el = document.getElementById("vab-overlay");
  if (el) el.style.display = "none";
}

function escHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// ── TTS playback ──────────────────────────────────────────────────────────────

async function playTtsAudio(audioB64: string): Promise<void> {
  const binary = atob(audioB64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);

  const ctx = new AudioContext();
  const buffer = await ctx.decodeAudioData(bytes.buffer.slice(0));
  await new Promise<void>((resolve) => {
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(ctx.destination);
    src.onended = () => { void ctx.close(); resolve(); };
    src.start();
  });
}

function setVoiceHint(text: string, color = "#a6adc8"): void {
  const hint = document.getElementById("vab-voice-hint");
  if (hint) { hint.textContent = text; hint.style.color = color; }
}

// ── Session state ─────────────────────────────────────────────────────────────

let ws: WebSocket | null = null;
let mediaRecorder: MediaRecorder | null = null;
let stream: MediaStream | null = null;
let isRecording = false;
let awaitingVoiceConfirmation = false;
let isVoiceConfirmationRecording = false;

// ── WebSocket message handler ─────────────────────────────────────────────────

function handleServerMessage(msg: WsInbound): void {
  switch (msg.type) {
    case "context_ack":
      void startMic();
      break;

    case "transcript":
      showOverlay(`Heard: "${msg.text}"`, "#89b4fa");
      break;

    case "agent_status":
      showOverlay(
        msg.status === "planning" ? "Planning..." : "Executing...",
        "#f9e2af"
      );
      break;

    case "confirmation_required":
      showConfirmation(msg);
      document.getElementById("vab-yes")?.addEventListener("click", () => sendConfirmation(true));
      document.getElementById("vab-no")?.addEventListener("click",  () => sendConfirmation(false));
      break;

    case "tts_audio":
      void playTtsAudio((msg as WsTtsAudio).audio_b64).then(() => {
        // After TTS finishes: if still on confirmation screen, enable voice response
        if (awaitingVoiceConfirmation || document.getElementById("vab-voice-hint")) {
          awaitingVoiceConfirmation = true;
          setVoiceHint("🎤 Press Alt+Space to say YES, NO, or describe changes");
        }
      });
      break;

    case "agent_result":
      // TTS of result is already sent by server; give it a moment then close overlay
      setTimeout(() => {
        showOverlay(`Done: ${msg.summary}`, "#a6e3a1");
        setTimeout(hideOverlay, 5000);
      }, 100);
      cleanup();
      break;

    case "error":
      showOverlay(`Error: ${msg.text}`, "#f38ba8");
      setTimeout(hideOverlay, 4000);
      cleanup();
      break;
  }
}

function sendConfirmation(confirmed: boolean): void {
  // Stop any in-progress voice confirmation recording
  if (isVoiceConfirmationRecording) {
    mediaRecorder?.stop();
    stream?.getTracks().forEach(t => t.stop());
    mediaRecorder = null;
    stream = null;
    isVoiceConfirmationRecording = false;
  }
  awaitingVoiceConfirmation = false;

  if (ws?.readyState !== WebSocket.OPEN) return;
  const msg: WsOutbound = { type: "confirmation_response", confirmed };
  ws.send(JSON.stringify(msg));
  showOverlay(confirmed ? "Confirmed — executing..." : "Cancelled.", confirmed ? "#f9e2af" : "#f38ba8");
  if (!confirmed) setTimeout(hideOverlay, 2500);
}

// ── Recording ─────────────────────────────────────────────────────────────────

async function startSession(): Promise<void> {
  if (isRecording) return;

  const ctxResult = extractScreenContext();
  if ("error" in ctxResult) {
    showOverlay("No email open — open an email first.", "#f38ba8");
    setTimeout(hideOverlay, 2500);
    return;
  }

  showOverlay("Connecting...", "#89b4fa");

  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    const outbound: WsOutbound = { type: "context", data: ctxResult };
    ws!.send(JSON.stringify(outbound));
  };

  ws.onmessage = (event: MessageEvent) => {
    const msg: WsInbound = JSON.parse(event.data as string);
    handleServerMessage(msg);
  };

  ws.onerror = () => {
    showOverlay("Server unreachable — is it running?", "#f38ba8");
    setTimeout(hideOverlay, 3000);
    cleanup();
  };

  ws.onclose = () => {
    isRecording = false;
  };
}

async function startMic(): Promise<void> {
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  } catch {
    showOverlay("Microphone access denied.", "#f38ba8");
    setTimeout(hideOverlay, 3000);
    cleanup();
    return;
  }

  mediaRecorder = new MediaRecorder(stream);

  mediaRecorder.ondataavailable = (e: BlobEvent) => {
    if (ws?.readyState === WebSocket.OPEN && e.data.size > 0) {
      ws.send(e.data);
    }
  };

  mediaRecorder.start(100);
  isRecording = true;
  showOverlay("Recording — press Alt+Space to send", "#f38ba8");
}

function stopSession(): void {
  if (!isRecording) return;

  mediaRecorder?.stop();
  stream?.getTracks().forEach(t => t.stop());

  if (ws?.readyState === WebSocket.OPEN) {
    const outbound: WsOutbound = { type: "end_of_audio" };
    ws.send(JSON.stringify(outbound));
    showOverlay("Transcribing...", "#f9e2af");
  }

  mediaRecorder = null;
  stream = null;
  isRecording = false;
}

// ── Voice confirmation recording ──────────────────────────────────────────────

async function startVoiceConfirmation(): Promise<void> {
  if (isVoiceConfirmationRecording) return;

  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  } catch {
    setVoiceHint("Microphone access denied.", "#f38ba8");
    return;
  }

  mediaRecorder = new MediaRecorder(stream);
  mediaRecorder.ondataavailable = (e: BlobEvent) => {
    if (ws?.readyState === WebSocket.OPEN && e.data.size > 0) ws.send(e.data);
  };
  mediaRecorder.start(100);
  isVoiceConfirmationRecording = true;
  setVoiceHint("🔴 Listening... press Alt+Space when done", "#f38ba8");
}

function stopVoiceConfirmation(): void {
  if (!isVoiceConfirmationRecording) return;

  mediaRecorder?.stop();
  stream?.getTracks().forEach(t => t.stop());
  mediaRecorder = null;
  stream = null;
  isVoiceConfirmationRecording = false;
  awaitingVoiceConfirmation = false;

  if (ws?.readyState === WebSocket.OPEN) {
    const outbound: WsOutbound = { type: "end_of_voice_confirmation" };
    ws.send(JSON.stringify(outbound));
    setVoiceHint("Processing your response...", "#f9e2af");
  }
}

function cleanup(): void {
  mediaRecorder?.stop();
  stream?.getTracks().forEach(t => t.stop());
  ws?.close();
  ws = null;
  mediaRecorder = null;
  stream = null;
  isRecording = false;
  awaitingVoiceConfirmation = false;
  isVoiceConfirmationRecording = false;
}

// ── Hotkey — Alt+Space toggle ────────────────────────────────────────────────

document.addEventListener("keydown", (e: KeyboardEvent) => {
  if (e.altKey === HOTKEY.altKey && e.code === HOTKEY.code) {
    e.preventDefault();

    // Voice confirmation mode takes priority over normal recording
    if (awaitingVoiceConfirmation || isVoiceConfirmationRecording) {
      if (isVoiceConfirmationRecording) {
        stopVoiceConfirmation();
      } else {
        void startVoiceConfirmation();
      }
      return;
    }

    // Normal command recording
    if (isRecording) {
      stopSession();
    } else {
      void startSession();
    }
  }
});

// ── Popup message handler ─────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener(
  (message: Message, _sender, sendResponse) => {
    if (message.type === "GET_SCREEN_CONTEXT") {
      sendResponse(extractScreenContext());
    }
    return true;
  }
);
