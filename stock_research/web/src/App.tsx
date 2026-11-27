import { useEffect, useRef, useState } from "react";
import { apiJson, type HealthResponse, type ModelConnectionOptions } from "./api";
import AgentWorkspace from "./components/AgentWorkspace";
import MarketView from "./components/MarketView";
import QuantView from "./components/QuantView";
import PaperView from "./components/PaperView";

type TabId = "agent" | "market" | "quant" | "paper";

const TABS: Array<{ id: TabId; label: string }> = [
  { id: "agent", label: "1. 投研 Agent 工作台" },
  { id: "market", label: "2. 行情与基本面" },
  { id: "quant", label: "3. 量化回测与风控" },
  { id: "paper", label: "4. 模拟持仓与盈亏" },
];

export default function App() {
  const [activeTab, setActiveTab] = useState<TabId>("agent");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [modelConnection, setModelConnection] = useState<ModelConnectionOptions>({});

  const sectionRefs = {
    agent: useRef<HTMLElement | null>(null),
    market: useRef<HTMLElement | null>(null),
    quant: useRef<HTMLElement | null>(null),
    paper: useRef<HTMLElement | null>(null),
  };

  useEffect(() => {
    void apiJson<HealthResponse>("/api/health")
      .then((res) => setHealth(res))
      .catch(() => {
        // non-fatal status pill fallback
      });
  }, []);

  const handleSelectTab = (tab: TabId) => {
    setActiveTab(tab);
    const el = sectionRefs[tab].current;
    if (el && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  };

  return (
    <div className="app-shell">
      <header className="top-nav">
        <div className="brand">
          <span className="brand-badge">JEVDB</span>
          <div>
            <h1 className="brand-title">股票研究与模拟交易工作台</h1>
            <div className="brand-sub">
              语义数据库 · Pi Agent + MCP · 量化回测 · A股 T+1 / 美股模拟账本
            </div>
          </div>
        </div>

        <nav className="nav-tabs" aria-label="核心功能视图导航">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              data-tab={t.id}
              className={`nav-tab ${activeTab === t.id ? "active" : ""}`}
              onClick={() => handleSelectTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>

        <div className="health-pill">
          {health
            ? `API ${health.status.toUpperCase()} · JEVDB ${health.jevdb_version} · DuckDB ${health.duckdb_version}`
            : "API 连接中..."}
        </div>
      </header>

      <main className="dashboard-main">
        <section
          ref={sectionRefs.agent}
          id="section-agent"
          className={`panel-section ${activeTab === "agent" ? "focused" : ""}`}
        >
          <div className="section-header">
            <h2 className="section-title">
              智能投研与交叉核验工作台
              <span className="section-tag">Pi Agent + JEVDB MCP</span>
            </h2>
            <span className="muted">端到端流程：用户需求 → 工具调用 → 核验分析 → 依据展示</span>
          </div>
          <AgentWorkspace onConnectionChange={setModelConnection} />
        </section>

        <section
          ref={sectionRefs.market}
          id="section-market"
          className={`panel-section ${activeTab === "market" ? "focused" : ""}`}
        >
          <div className="section-header">
            <h2 className="section-title">
              行情、基本面与行业信息看板
              <span className="section-tag">AkShare · yfinance · EDGAR · Finnhub</span>
            </h2>
            <span className="muted">支持按需抓取 A 股 / 美股日线、财务估值、行业成分股与公告</span>
          </div>
          <MarketView modelConnection={modelConnection} />
        </section>

        <section
          ref={sectionRefs.quant}
          id="section-quant"
          className={`panel-section ${activeTab === "quant" ? "focused" : ""}`}
        >
          <div className="section-header">
            <h2 className="section-title">
              量化策略回测与投资组合风控对比
              <span className="section-tag">Value & Daily Quant · VaR / HHI</span>
            </h2>
            <span className="muted">确定性日频回测、多策略对比、95% VaR/CVaR 与相关性分析</span>
          </div>
          <QuantView />
        </section>

        <section
          ref={sectionRefs.paper}
          id="section-paper"
          className={`panel-section ${activeTab === "paper" ? "focused" : ""}`}
        >
          <div className="section-header">
            <h2 className="section-title">
              持久化模拟交易账户与盈亏监控
              <span className="section-tag">CN T+1 · 100股整数倍 · 费率与滑点</span>
            </h2>
            <span className="muted">支持跨日持仓追踪、T+1 解锁推进及规则拦截审计</span>
          </div>
          <PaperView />
        </section>
      </main>
    </div>
  );
}
