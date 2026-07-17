// popup.ts — action popup UI logic

import type { Message, ScreenContext, ScreenContextResult } from "./types";

function isError(result: ScreenContextResult): result is { error: string } {
  return "error" in result;
}

function renderContext(ctx: ScreenContext): void {
  const fields: Record<string, string> = {
    Subject: ctx.subject,
    From:    ctx.sender,
    Snippet: ctx.body_snippet || "(empty)",
    Thread:  ctx.thread_id ?? "(not detected)",
    URL:     ctx.url,
    At:      ctx.extracted_at,
  };

  const rows = Object.entries(fields)
    .map(([k, v]) => `<tr><td class="label">${k}</td><td class="value">${escHtml(v)}</td></tr>`)
    .join("");

  document.getElementById("output")!.innerHTML = `<table>${rows}</table>`;
}

function escHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

async function readEmail(): Promise<void> {
  const output = document.getElementById("output")!;
  const btn    = document.getElementById("read-btn") as HTMLButtonElement;

  btn.disabled = true;
  output.textContent = "Reading...";

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab.id) {
    output.textContent = "Error: no active tab.";
    btn.disabled = false;
    return;
  }

  const msg: Message = { type: "GET_SCREEN_CONTEXT" };

  chrome.tabs.sendMessage(tab.id, msg, (result: ScreenContextResult) => {
    btn.disabled = false;
    if (chrome.runtime.lastError) {
      output.textContent = `Error: ${chrome.runtime.lastError.message}`;
      return;
    }
    if (isError(result)) {
      output.textContent = result.error;
      return;
    }
    renderContext(result);
  });
}

document.getElementById("read-btn")!.addEventListener("click", readEmail);
