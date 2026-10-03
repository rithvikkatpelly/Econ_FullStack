import { API_BASE_URL, ApiError } from "./client";
import type { ApiErrorBody } from "./types";

// Mirrors the event feed documented in backend/app/agent.py. Tool *results*
// never appear here — only what was called and whether it worked.

export type AgentName = "economic_data_agent" | "research_agent" | "risk_agent" | "report_agent";

interface Timed {
  elapsed_ms?: number;
}

export type AgentEvent =
  | ({ type: "start"; query: string; backend: string; follow_up: boolean } & Timed)
  | ({ type: "delegation"; agent: AgentName; task: string } & Timed)
  | ({
      type: "tool_call";
      agent: AgentName;
      tool: string;
      arguments: Record<string, unknown>;
      ok: boolean;
      error: string | null;
      latency_ms: number;
    } & Timed)
  | ({ type: "agent_output"; agent: AgentName; output: string } & Timed)
  | ({ type: "report_delta"; agent: AgentName; text: string } & Timed)
  | ({ type: "waiting"; agent: AgentName; reason: string; seconds: number } & Timed)
  | ({ type: "fallback"; agent: AgentName; reason: string; from_model: string; to_model: string } & Timed)
  | ({ type: "final"; backend: string } & AgentResult & Timed)
  | ({ type: "error" } & ApiErrorBody & Timed);

export interface AgentResult {
  final_report: string;
  series_used: string[];
  risk_signal: string | null;
  input_tokens: number;
  output_tokens: number;
  elapsed_ms: number;
  /** Tool-data tokens this run pulled, against its own per-run budget. */
  data_tokens: number;
  data_token_budget: number;
}

/** Split an SSE buffer into complete `data:` payloads; returns the parsed
 * events and whatever partial frame is left over. */
export function parseFrames(buffer: string): { events: AgentEvent[]; rest: string } {
  const frames = buffer.split("\n\n");
  const rest = frames.pop() ?? "";
  const events: AgentEvent[] = [];
  for (const frame of frames) {
    const data = frame
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (data) events.push(JSON.parse(data) as AgentEvent);
  }
  return { events, rest };
}

/**
 * POST the question to /agent/stream and call `onEvent` for each event as it
 * arrives. fetch + a stream reader rather than EventSource, because
 * EventSource can only GET (the question would end up in a URL and in logs).
 * Rejects with `ApiError` if the run is refused up front (rate limit, busy,
 * bad input); a run that fails midway arrives as an `error` event instead.
 */
/** An earlier question/answer pair, sent back so a follow-up can refer to it. */
export interface PriorTurn {
  query: string;
  answer: string;
}

/** The API accepts at most this many earlier turns (agents/conversation.py). */
export const MAX_HISTORY = 3;

export async function streamAgent(
  query: string,
  history: PriorTurn[],
  onEvent: (event: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/agent/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ query, history: history.slice(-MAX_HISTORY) }),
    signal,
  });

  if (!response.ok || !response.body) {
    const payload = await response.json().catch(() => null);
    const detail = payload?.detail;
    const body: ApiErrorBody | null =
      detail && typeof detail === "object" && "error" in detail
        ? (detail as ApiErrorBody)
        : Array.isArray(detail)
          ? { error: "validation_error", detail: detail[0]?.msg?.replace(/^Value error, /, "") }
          : null;
    throw new ApiError(response.status, body, `Request failed (${response.status})`);
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    const { events, rest } = parseFrames(buffer + value);
    buffer = rest;
    events.forEach(onEvent);
  }
}
