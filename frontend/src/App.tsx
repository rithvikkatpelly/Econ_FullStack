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

export default function App() {
  const [api, setApi] = useState<ApiState>({ status: "checking" });
  const [request, setRequest] = useState<ExplorerRequest | null>(null);
  const { items, loading } = useSnapshots();

  useEffect(() => {
    fetch(`${API_BASE_URL}/health`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((b) => setApi({ status: "up", offline: Boolean(b.offline) }))
      .catch(() => setApi({ status: "down" }));
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
      {api.status === "down" && (
        <div className="banner banner-bad" role="alert">
          <strong>Can't reach the data service.</strong> If you're running locally, start the API:{" "}
          <code>cd backend &amp;&amp; uvicorn app.main:app</code>
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
