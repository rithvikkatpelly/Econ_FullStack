import { useCallback, useEffect, useState } from "react";
import { API_BASE_URL } from "./api/client";
import { AskAgent } from "./components/AskAgent";
import { Comparisons } from "./components/Comparisons";
import { CtaBanner } from "./components/CtaBanner";
import { Explorer } from "./components/Explorer";
import { Footer } from "./components/Footer";
import { Hero } from "./components/Hero";
import { HowItWorks } from "./components/HowItWorks";
import { Marquee } from "./components/Marquee";
import { Nav, type ApiState } from "./components/Nav";
import type { ExplorerRequest, OpenExplorer } from "./explorer";
import { useSnapshots } from "./useSnapshots";

const LOCAL_API = /\/\/(localhost|127\.0\.0\.1)[:/]/.test(API_BASE_URL);

export default function App() {
  const [api, setApi] = useState<ApiState>({ status: "checking" });
  const [request, setRequest] = useState<ExplorerRequest | null>(null);
  const [slowStart, setSlowStart] = useState(false);
  const { items, loading } = useSnapshots();

  useEffect(() => {
    // The API scales to zero when idle (Cloud Run --min-instances=0); the first
    // request after that waits for a cold start. Say so instead of looking broken.
    const timer = setTimeout(() => setSlowStart(true), 3000);
    fetch(`${API_BASE_URL}/health`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((b) => setApi({ status: "up", offline: Boolean(b.offline) }))
      .catch(() => setApi({ status: "down" }))
      .finally(() => clearTimeout(timer));
    return () => clearTimeout(timer);
  }, []);

  const open: OpenExplorer = useCallback((req) => {
    setRequest({ ...req, nonce: Date.now() } as ExplorerRequest);
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    document.getElementById("explore")?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  }, []);

  return (
    <>
      <a className="skip" href="#explore">
        Skip to the explorer
      </a>
      {api.status === "up" && api.offline && (
        <div className="banner" role="note">
          <strong>Demo mode</strong> — you're seeing built-in sample numbers, not real economic figures.
        </div>
      )}
      {api.status === "checking" && slowStart && (
        <div className="banner" role="status">
          <strong>Waking the server</strong> — the API scales to zero when nobody's using it. This takes a few
          seconds, once.
        </div>
      )}
      {api.status === "down" && (
        <div className="banner banner-bad" role="alert">
          <strong>Can't reach the data service.</strong>{" "}
          {LOCAL_API ? (
            <>
              If you're running locally, start the API: <code>cd backend &amp;&amp; uvicorn app.main:app</code>
            </>
          ) : (
            "Try reloading in a minute."
          )}
        </div>
      )}
      <Nav api={api} />
      <main>
        <Hero items={items} loading={loading} open={open} />
        <div className="container">
          <AskAgent open={open} />
          <Comparisons items={items} loading={loading} open={open} />
          <Explorer request={request} open={open} />
        </div>
        <Marquee open={open} />
        <div className="container">
          <HowItWorks />
          <CtaBanner />
        </div>
      </main>
      <Footer />
    </>
  );
}
