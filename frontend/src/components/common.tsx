import { useState, type ReactNode } from "react";
import { ApiError } from "../api/client";

/** Turn an API/network failure into words a non-developer can act on. */
export function ErrorNotice({ error }: { error: unknown }) {
  let title = "Something went wrong";
  let detail: string | undefined;
  let hint: string | undefined;

  if (error instanceof ApiError) {
    const code = error.body?.error;
    detail = error.body?.detail ?? undefined;
    hint = error.body?.suggestion ?? undefined;
    switch (code) {
      case "validation_error":
        title = "That input doesn't look right";
        break;
      case "series_not_found":
        title = "We couldn't find that series";
        hint = hint ?? "Use “Find a series” to look one up by name.";
        break;
      case "session_budget_exceeded":
        title = "The demo's data allowance is used up";
        hint = "Try a shorter date range, or come back a little later.";
        break;
      case "rate_limited":
        title = "You're asking faster than the demo allows";
        break;
      case "agent_busy":
        title = "The agent is busy with other questions";
        break;
      case "agent_error":
        title = "The agent couldn't finish this one";
        break;
      case "fred_api_error":
        title = "The data provider had a problem";
        hint = "This is usually temporary — try again in a moment.";
        break;
      default:
        title = code ? code.replace(/_/g, " ") : `Request failed (${error.status})`;
    }
    if (error.status === 422 && !error.body) {
      title = "That input doesn't look right";
      detail = "Check the fields above and try again.";
    }
  } else if (error instanceof TypeError) {
    title = "Can't reach the API";
    detail = "The data service didn't respond.";
    hint = "If you're running locally, start it with `uvicorn app.main:app` in backend/.";
  } else if (error instanceof Error) {
    detail = error.message;
  }

  return (
    <div className="notice notice-error" role="alert">
      <strong>{title}</strong>
      {detail && <p>{detail}</p>}
      {hint && <p className="notice-hint">{hint}</p>}
    </div>
  );
}

export function Skeleton({ height = 240 }: { height?: number }) {
  return <div className="skeleton" style={{ height }} aria-busy="true" aria-label="Loading" />;
}

export function DataTable({ columns, rows }: { columns: string[]; rows: (string | number)[][] }) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {row.map((cell, j) => (
                <td key={j}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Field({
  label,
  hint,
  children,
  grow,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  grow?: boolean;
}) {
  return (
    <label className={`field${grow ? " field-grow" : ""}`}>
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

export function Chip({ children, tone }: { children: ReactNode; tone?: "lime" }) {
  return <span className={`chip${tone ? ` chip-${tone}` : ""}`}>{children}</span>;
}

export function ToolTag({ children }: { children: ReactNode }) {
  return <code className="tool-tag">{children}</code>;
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="btn btn-ghost btn-sm"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1600);
        } catch {
          /* clipboard blocked — nothing useful to do */
        }
      }}
    >
      {copied ? "Copied ✓" : label}
    </button>
  );
}
