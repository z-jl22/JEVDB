import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { apiJson, type HealthResponse, type ModelConnectionOptions } from "./api";
import { readLastConnection } from "./agentConnection";

const AgentWorkspace = lazy(() => import("./components/AgentWorkspace"));
const MarketView = lazy(() => import("./components/MarketView"));
const QuantView = lazy(() => import("./components/QuantView"));
const PaperView = lazy(() => import("./components/PaperView"));
const PAGES = {
  agent: { title: "投研", icon: "M4 5h13a3 3 0 0 1 3 3v6a3 3 0 0 1-3 3h-6l-5 4v-4H4a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Zm3 5h8m-8 3h5" },
  market: { title: "行情", icon: "M5 3v5m0 5v7m7-17v3m0 6v8m7-17v8m0 5v4M3 8h4v5H3zm7-2h4v6h-4zm7 5h4v5h-4z" },
  quant: { title: "策略回测", icon: "M3 3v17h18M7 14l4-5 4 3 5-7m-4 0h4v4" },
  paper: { title: "模拟交易", icon: "M3 7h15a2 2 0 0 1 2 2v10H4a2 2 0 0 1-2-2V7a3 3 0 0 1 3-3h12v3m3 5h-5a2 2 0 0 0 0 4h5" },
};
type Workspace = keyof typeof PAGES;
const pageKeys = ["market", "quant", "paper", "agent"] as const;

function readPage(): Workspace {
  const name = window.location.hash.slice(1);
  return pageKeys.find((key) => key === name) ?? "market";
}

export default function App() {
  const [active, setActive] = useState(readPage);
  const [visited, setVisited] = useState(() => new Set([active]));
  const [health, setHealth] = useState<HealthResponse | "offline" | null>(null);
  const [modelConnection, setModelConnection] = useState<ModelConnectionOptions>(() => readLastConnection() ?? {});
  const heading = useRef<HTMLHeadingElement>(null);
  const pageTitle = PAGES[active].title;

  useEffect(() => {
    const navigate = () => {
      const page = readPage();
      setVisited((prior) => new Set(prior).add(page));
      setActive(page);
      window.scrollTo(0, 0);
      requestAnimationFrame(() => heading.current?.focus({ preventScroll: true }));
    };
    window.addEventListener("hashchange", navigate);
    return () => window.removeEventListener("hashchange", navigate);
  }, []);

  useEffect(() => {
    document.title = pageTitle + " · JEVDB 研究终端";
  }, [pageTitle]);

  useEffect(() => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    void apiJson<HealthResponse>("/api/health", { signal: controller.signal })
      .then(setHealth)
      .catch(() => setHealth("offline"))
      .finally(() => window.clearTimeout(timeout));
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, []);

  const ready = health !== null && health !== "offline" && health.status?.toLowerCase() === "ok";
  const status = ready ? "服务已连接" : health === null ? "服务连接中" : "服务不可用";

  return (
    <div className="app-shell">
      <a className="skip-link" href="#workspace-content" onClick={(event) => {
        event.preventDefault();
        heading.current?.focus();
      }}>跳到工作区</a>
      <header className="workbench-bar">
        <a className="brand" href="#market" aria-label="JEVDB 行情首页">
          <span className="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 32 32" fill="none"><path d="M8 20V9h5v13l-4 4H5v-5h2l1-1Zm10-11h5v11l-5 5h-5l5-5V9Z" fill="currentColor" /></svg>
          </span>
          <strong>JEVDB</strong>
        </a>
        <nav className="workspace-nav" aria-label="工作区">
          {pageKeys.map((key) => (
            <a href={"#" + key} data-tab={key} key={key} aria-current={active === key ? "page" : undefined}>
              <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d={PAGES[key].icon} /></svg>
              <span>{PAGES[key].title}</span>
            </a>
          ))}
        </nav>
        <div className="service-state" data-state={ready ? "ready" : health === null ? "pending" : "offline"} role="status"
          title={health && health !== "offline" ? "JEVDB " + health.jevdb_version + " · DuckDB " + health.duckdb_version : undefined}>
          <span aria-hidden="true" />{status}
        </div>
      </header>
      <main className="workspace" id="workspace-content">
        <header className="page-heading">
          <h1 ref={heading} tabIndex={-1}>{pageTitle}</h1>
        </header>
        {pageKeys.filter((key) => visited.has(key)).map((key) => {
          return (
            <section key={key} id={"section-" + key} hidden={key !== active} aria-label={PAGES[key].title}>
              <Suspense fallback={<p className="muted" role="status">正在打开工作区</p>}>
                {key === "agent" ? (
                  <AgentWorkspace onConnectionChange={setModelConnection} />
                ) : key === "market" ? (
                  <MarketView modelConnection={modelConnection} />
                ) : key === "quant" ? (
                  <QuantView />
                ) : (
                  <PaperView />
                )}
              </Suspense>
            </section>
          );
        })}
      </main>
    </div>
  );
}
