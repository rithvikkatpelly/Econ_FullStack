import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { streamAgent, type AgentEvent, type AgentName, type AgentResult, type PriorTurn } from "../api/agent";
import { ApiError } from "../api/client";
import { CURATED_BY_ID } from "../catalog";
import type { OpenExplorer } from "../explorer";
import { ErrorNotice, ToolTag } from "./common";

const EXAMPLES = [
  "Compare CPI and unemployment over the last 5 years — did the relationship change after 2020?",
  "Is recession risk rising? Look at unemployment and the 10-year yield since 2022.",
  "Just pull core PCE since 2021.",
  "How expensive has borrowing gotten recently?",
];

const AGENTS: Record<AgentName, { label: string; role: string }> = {
  economic_data_agent: { label: "Economic Data Agent", role: "finds and fetches the series" },
  research_agent: { label: "Research Agent", role: "adds context and caveats" },
  risk_agent: { label: "Risk Agent", role: "reads the direction of risk" },
  report_agent: { label: "Report Agent", role: "writes the grounded answer" },
};

const MAX_TURNS = 5;

type Status = "running" | "done" | "error" | "stopped";

interface Turn {
  id: number;
  query: string;
  status: Status;
  events: AgentEvent[];
  /** The answer as it streams in (report_delta events), until `final`. */
  draft: string;
  backend?: string;
  framework?: string;
  result?: AgentResult;
  error?: unknown;
}

export function AskAgent({ open }: { open: OpenExplorer }) {
  const [text, setText] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const abort = useRef<AbortController | null>(null);
  const running = turns.some((t) => t.status === "running");

  useEffect(() => () => abort.current?.abort(), []);

  const update = (id: number, fn: (t: Turn) => Turn) =>
    setTurns((all) => all.map((t) => (t.id === id ? fn(t) : t)));

  async function ask(query: string) {
    const q = query.trim();
    if (!q || running) return;
    const id = Date.now();
    // Completed turns become context for this one (the API keeps the last few).
    const history: PriorTurn[] = turns.flatMap((t) =>
      t.result ? [{ query: t.query, answer: t.result.final_report }] : [],
    );
    const controller = new AbortController();
    abort.current = controller;
    setText("");
    setTurns((all) => [...all.slice(-(MAX_TURNS - 1)), { id, query: q, status: "running", events: [], draft: "" }]);

    try {
      await streamAgent(
        q,
        history,
        (event) =>
          update(id, (t) => {
            // Deltas only grow the draft; they aren't steps in the timeline.
            if (event.type === "report_delta") return { ...t, draft: t.draft + event.text };
            const next: Turn = { ...t, events: [...t.events, event] };
            if (event.type === "start") {
              next.backend = event.backend;
              next.framework = event.framework;
            }
            // Gemini's quota ran out mid-answer: the stub starts over.
            if (event.type === "fallback" && event.restart) next.draft = "";
            if (event.type === "final") return { ...next, status: "done", backend: event.backend, result: event };
            if (event.type === "error") return { ...next, status: "error", error: new ApiError(502, event, "") };
            return next;
          }),
        controller.signal,
      );
      // The stream closed without a final/error frame (e.g. the connection dropped).
      update(id, (t) =>
        t.status === "running" ? { ...t, status: "error", error: new Error("The connection closed early.") } : t,
      );
    } catch (error) {
      const stopped = controller.signal.aborted;
      update(id, (t) => ({ ...t, status: stopped ? "stopped" : "error", error: stopped ? undefined : error }));
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    ask(text);
  }

  return (
    <section id="ask" className="section" aria-labelledby="ask-title">
      <div className="section-head">
        <h2 id="ask-title" className="section-title">
          Ask the <em>agent</em>
        </h2>
        <ToolTag>POST /agent/stream</ToolTag>
      </div>
      <p className="ask-intro muted">
        A supervisor model breaks your question into steps and hands them to four specialist agents. Watch them work
        below — every number in the answer comes from a data call you can see.
      </p>

      <div className="ask">
        {turns.length > 0 && (
          <ol className="turns" aria-live="polite">
            {turns.map((t) => (
              <TurnView key={t.id} turn={t} open={open} />
            ))}
          </ol>
        )}

        <form className="ask-form" onSubmit={onSubmit}>
          <label htmlFor="ask-input" className="sr-only">
            Your question
          </label>
          <textarea
            id="ask-input"
            value={text}
            rows={2}
            maxLength={500}
            placeholder={
              turns.length
                ? "Ask a follow-up — e.g. what about since 2015?"
                : "e.g. Compare CPI and unemployment since 2019 and explain the relationship"
            }
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                ask(text);
              }
            }}
          />
          {running ? (
            <button type="button" className="btn btn-ghost" onClick={() => abort.current?.abort()}>
              Stop
            </button>
          ) : (
            <button type="submit" className="btn btn-lime" disabled={!text.trim()}>
              Ask
            </button>
          )}
        </form>

        {turns.length > 0 && !running && (
          <div className="ask-tools">
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setTurns([])}>
              New conversation
            </button>
          </div>
        )}

        {turns.length === 0 && (
          <div className="quick">
            <span>Try:</span>
            {EXAMPLES.map((q) => (
              <button key={q} type="button" className="chip chip-btn" onClick={() => ask(q)}>
                {q}
              </button>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function TurnView({ turn, open }: { turn: Turn; open: OpenExplorer }) {
  const steps = turn.events.filter((e) => e.type !== "start" && e.type !== "final" && e.type !== "error");
  const ms = turn.result?.elapsed_ms;
  const took = ms == null ? null : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;

  return (
    <li className="turn">
      <p className="turn-q">{turn.query}</p>

      {turn.status === "running" ? (
        <Timeline steps={steps} live={turn.draft ? "Writing the answer…" : steps.length ? "Working…" : "Planning…"} />
      ) : (
        steps.length > 0 && (
          <details className="disclose turn-steps">
            <summary>
              {steps.length} step{steps.length === 1 ? "" : "s"}
              {took && ` · ${took}`}
            </summary>
            <Timeline steps={steps} />
          </details>
        )
      )}

      {turn.status === "running" && turn.draft && (
        <div className="answer answer-draft" aria-busy="true">
          <Report text={turn.draft} />
        </div>
      )}

      {turn.status === "error" && <ErrorNotice error={turn.error} />}
      {turn.status === "stopped" && <div className="notice">Stopped. The agents may finish in the background.</div>}

      {turn.result?.degraded === "model_quota_exhausted" && (
        <div className="notice">
          Today's free Gemini quota is used up, so the offline stub answered this one: the same agents and real data
          calls, with a rule-based planner instead of the model. Live answers come back after the quota resets.
        </div>
      )}

      {turn.result && (
        <div className="answer">
          <Report text={turn.result.final_report} />
          <div className="answer-foot">
            {turn.result.series_used.map((id) => (
              <button
                key={id}
                type="button"
                className="chip chip-btn chip-lime"
                title={`Chart ${CURATED_BY_ID[id]?.name ?? id}`}
                onClick={() => open({ kind: "chart", id })}
              >
                {id} ↗
              </button>
            ))}
            {turn.result.risk_signal && <RiskBadge signal={turn.result.risk_signal} />}
            <span className="mono muted answer-meta">
              {turn.framework ? `${turn.framework} · ` : ""}
              {turn.backend}
              {turn.result.input_tokens + turn.result.output_tokens > 0 &&
                ` · ${(turn.result.input_tokens + turn.result.output_tokens).toLocaleString()} tokens`}
              {turn.result.data_token_budget > 0 &&
                ` · data ${turn.result.data_tokens.toLocaleString()}/${turn.result.data_token_budget.toLocaleString()}`}
            </span>
          </div>
        </div>
      )}
    </li>
  );
}

function Timeline({ steps, live }: { steps: AgentEvent[]; live?: string }) {
  return (
    <ol className="timeline">
      {steps.map((e, i) => (
        <li key={i} className={`tl tl-${e.type}${e.type === "tool_call" && !e.ok ? " tl-bad" : ""}`}>
          <i aria-hidden="true" />
          <div>{describe(e)}</div>
        </li>
      ))}
      {live && (
        <li className="tl tl-live">
          <i aria-hidden="true" />
          <div className="muted">{live}</div>
        </li>
      )}
    </ol>
  );
}

function describe(e: AgentEvent): ReactNode {
  switch (e.type) {
    case "delegation":
      return (
        <>
          <strong>Supervisor → {AGENTS[e.agent]?.label ?? e.agent}</strong>
          <span className="muted"> — {AGENTS[e.agent]?.role}</span>
        </>
      );
    case "tool_call":
      return (
        <>
          <code className="tool-tag">{e.tool}</code> <span className="muted">{formatArgs(e.arguments)}</span>
          <span className="mono muted tl-ms"> {Math.round(e.latency_ms)} ms</span>
          {!e.ok && <span className="tl-err"> {e.error?.replace(/_/g, " ")}</span>}
        </>
      );
    case "waiting":
      return (
        <span className="muted">
          Waiting {e.seconds} s for the model's rate limit, then continuing{" "}
          <span className="mono">({AGENTS[e.agent]?.label ?? e.agent})</span>
        </span>
      );
    case "fallback":
      return (
        <span className="muted">
          <span className="mono">{e.from_model}</span>{" "}
          {e.reason === "quota_exhausted" ? "is out of quota" : "is overloaded"} —{" "}
          {e.restart ? "starting over on" : "switched to"} <span className="mono">{e.to_model}</span>
        </span>
      );
    case "agent_output":
      return (
        <details className="tl-out">
          <summary>{AGENTS[e.agent]?.label ?? e.agent} finished</summary>
          <p>{e.output}</p>
        </details>
      );
    default:
      return null;
  }
}

function formatArgs(args: Record<string, unknown>): string {
  const ids = Array.isArray(args.series_ids) ? args.series_ids.join(", ") : args.series_id;
  const range = args.start_date && args.end_date ? `${args.start_date} → ${args.end_date}` : null;
  const search = args.search_text ?? args.query;
  return [ids, search && `“${search}”`, range].filter(Boolean).join(" · ");
}

function RiskBadge({ signal }: { signal: string }) {
  const tone = signal === "rising" || signal === "elevated" ? "bad" : signal === "easing" ? "ok" : "warn";
  return (
    <span className={`risk risk-${tone}`}>
      Risk: <b>{signal}</b>
    </span>
  );
}

/** Tiny, safe renderer for the report: paragraphs, "- " bullets, **bold**,
 * "#" headings, and a short line ending in ":" (e.g. "Evidence"). Builds React
 * elements only — model output is never injected as HTML. */
function Report({ text }: { text: string }) {
  const blocks = text.trim().split(/\n\s*\n/);
  return (
    <div className="report">
      {blocks.map((block, i) => {
        const lines = block.split("\n").filter((l) => l.trim());
        const items: ReactNode[] = [];
        let list: string[] = [];
        const flush = () => {
          if (list.length) items.push(<ul key={`u${items.length}`}>{list.map((l, j) => <li key={j}>{inline(l)}</li>)}</ul>);
          list = [];
        };
        for (const raw of lines) {
          const line = raw.trim();
          const bullet = line.match(/^[-*•]\s+(.*)$/);
          if (bullet) {
            list.push(bullet[1]);
            continue;
          }
          flush();
          const heading = line.replace(/^#+\s*/, "");
          if (/^#+\s/.test(line) || /^evidence:?$/i.test(line) || (line.endsWith(":") && line.length < 40)) {
            items.push(<h4 key={`h${items.length}`}>{inline(heading.replace(/:$/, ""))}</h4>);
          } else {
            items.push(<p key={`p${items.length}`}>{inline(line)}</p>);
          }
        }
        flush();
        return <div key={i}>{items}</div>;
      })}
    </div>
  );
}

function inline(s: string): ReactNode[] {
  return s.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") ? <strong key={i}>{part.slice(2, -2)}</strong> : part,
  );
}
