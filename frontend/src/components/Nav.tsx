import { REPO_URL } from "../catalog";
import { API_BASE_URL } from "../api/client";

export type ApiState = { status: "checking" } | { status: "down" } | { status: "up"; offline: boolean };

function Logo() {
  return (
    <a className="logo" href="#top" aria-label="Econ Data — home">
      <svg viewBox="0 0 40 44" width="34" height="38" aria-hidden="true">
        <path d="M20 1.5 37 11v22L20 42.5 3 33V11z" fill="#ddf247" />
        <path d="M10 27l6-7 5 4 9-11" fill="none" stroke="#141414" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <span>
        Econ<b>Data</b>
      </span>
    </a>
  );
}

export function Nav({ api }: { api: ApiState }) {
  const label =
    api.status === "checking"
      ? "Connecting…"
      : api.status === "down"
        ? "API offline"
        : api.offline
          ? "Demo data"
          : "Live FRED data";
  const tone = api.status === "up" ? (api.offline ? "warn" : "ok") : api.status === "down" ? "bad" : "idle";

  return (
    <header className="nav">
      <div className="container nav-inner">
        <Logo />
        <nav aria-label="Primary" className="nav-links">
          <a href="#top">Home</a>
          <a href="#ask">Ask</a>
          <a href="#explore">Explore</a>
          <a href="#how">How it works</a>
          <a href={`${API_BASE_URL}/docs`} target="_blank" rel="noreferrer">
            API
          </a>
          <a href={REPO_URL} target="_blank" rel="noreferrer">
            GitHub
          </a>
        </nav>
        <div className="nav-right">
          <span className={`status status-${tone}`} role="status">
            <i aria-hidden="true" />
            {label}
          </span>
          <a className="btn btn-lime" href="#explore">
            Start exploring
          </a>
        </div>
      </div>
    </header>
  );
}
