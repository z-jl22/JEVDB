export interface HealthResponse {
  status: string;
  jevdb_version: string;
  duckdb_version: string;
}

export interface ToolCallItem {
  tool_call_id?: string;
  name: string;
  args?: Record<string, unknown>;
  result_summary?: string;
  duration_ms?: number;
  is_error?: boolean;
  status?: "running" | "done" | "error";
}

export interface AgentVerification {
  grounded?: boolean;
  passed?: boolean;
  verified?: boolean;
  ungrounded_numbers?: unknown[];
  unverified_urls?: unknown[];
  warnings?: string[];
  sources?: string[];
  verified_sources?: string[];
}

export interface AgentQueryResponse {
  prompt: string;
  tool_calls: ToolCallItem[];
  verification: AgentVerification;
  answer: string;
  sources: string[];
  duration_ms: number;
  connection?: AgentConnection;
}

export interface AgentSettings {
  base_url: string;
  has_api_key: boolean;
  model: string;
}

export type AgentConnection = Pick<AgentSettings, "base_url" | "model">;
export type ModelConnectionOptions = Partial<AgentConnection> & { api_key?: string };

export interface AgentQueryRequest extends ModelConnectionOptions {
  prompt: string;
  mode: string;
}

export async function* agentEvents(request: AgentQueryRequest, signal: AbortSignal) {
  const res = await fetch("/api/agent/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(request),
    signal,
  });
  if (!res.ok || !res.body || !res.headers.get("Content-Type")?.includes("text/event-stream")) {
    throw new Error("无法建立 Agent 实时连接");
  }

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += value;
      // The API writes each event as an LF-delimited frame with one JSON data line.
      let end = buffer.indexOf("\n\n");
      while (end !== -1) {
        const [event, data] = buffer.slice(0, end).split("\n");
        buffer = buffer.slice(end + 2);
        end = buffer.indexOf("\n\n");
        if (event?.startsWith("event: ") && data?.startsWith("data: ")) {
          yield { name: event.slice(7), data: JSON.parse(data.slice(6)) as unknown };
        }
      }
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export interface MarketBar {
  date: string;
  trade_date?: string;
  symbol: string;
  market: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
  source?: string | null;
}

export interface MarketBarsResponse {
  symbol: string;
  name: string | null;
  bars: MarketBar[];
}

export interface SectorConstituent {
  sector_name: string;
  symbol: string;
  name?: string | null;
  price?: number | null;
  change_pct?: number | null;
  pe?: number | null;
  pb?: number | null;
  market_cap?: number | null;
  updated_at?: string | null;
  source?: string | null;
}

export interface FundamentalRow {
  symbol: string;
  market: string;
  report_date: string;
  pe_ttm?: number | null;
  pb?: number | null;
  roe?: number | null;
  revenue?: number | null;
  net_income?: number | null;
  debt_ratio?: number | null;
  source?: string | null;
}

export interface EdgarFilingRow {
  cik?: string;
  symbol: string;
  accession_no: string;
  form?: string;
  filing_date?: string;
  report_date?: string;
  primary_doc?: string;
  title?: string;
  url?: string;
}

export interface NewsItem {
  event_id: string;
  symbol?: string | null;
  market?: string | null;
  sector?: string | null;
  published_at?: string | null;
  title: string;
  summary?: string | null;
  url?: string | null;
  source?: string | null;
  sentiment_label?: string | null;
  sentiment?: "positive" | "negative" | "neutral" | "insufficient";
  sentiment_source?: "model" | "stored" | "keyword";
}

export interface StrategySpec {
  strategy_id: string;
  name: string;
  style: string;
  market: string;
  description: string;
  params: Record<string, unknown>;
  rules: Record<string, unknown>;
  created_at?: string;
  source?: string;
}

export interface BacktestMetrics {
  total_return?: number;
  cagr?: number;
  annualized_volatility?: number;
  sharpe_ratio?: number;
  sortino_ratio?: number;
  calmar_ratio?: number;
  max_drawdown?: number;
  max_drawdown_duration_days?: number;
  var_95?: number;
  cvar_95?: number;
  win_rate?: number;
  profit_factor?: number;
  turnover?: number;
  total_fees?: number;
  gross_return?: number;
  fee_drag?: number;
}

export interface FeeScenarioRow {
  commission_rate: number;
  min_commission: number;
  final_equity: number;
  total_return?: number | null;
  gross_return?: number | null;
  fee_drag?: number | null;
  total_fees?: number | null;
  turnover?: number | null;
  total_trades?: number | null;
}

export interface FeeSensitivityResponse {
  strategy_id: string;
  rows: FeeScenarioRow[];
}

export interface ExecutionScenarioRow {
  execution: string;
  final_equity: number;
  total_return?: number | null;
  max_drawdown?: number | null;
  sharpe_ratio?: number | null;
  total_trades?: number | null;
  total_fees?: number | null;
}

export interface ExecutionSensitivityResponse {
  strategy_id: string;
  rows: ExecutionScenarioRow[];
}

export interface BacktestResult {
  strategy_id: string;
  strategy_name: string;
  style: string;
  market: string;
  symbols: string[];
  start_date: string;
  end_date: string;
  initial_capital: number;
  final_equity: number;
  equity_curve: Array<{
    date: string;
    equity: number;
    benchmark_equity?: number;
    benchmark_aligned_equity?: number;
    drawdown?: number;
  }>;
  trades: Array<Record<string, unknown>>;
  metrics: BacktestMetrics;
  reproducibility_hash: string;
}

export interface CompareResult {
  strategy_ids: string[];
  symbols: string[];
  start_date: string;
  end_date: string;
  results: Record<string, BacktestResult>;
  ranking: Array<{
    strategy_id: string;
    strategy_name: string;
    style: string;
    sharpe_ratio?: number;
    total_return?: number;
    max_drawdown?: number;
    annualized_return?: number;
    win_rate?: number;
  }>;
  aligned_equity_curves: Array<{ date: string; benchmark_equity?: number; benchmark_aligned_equity?: number } & Record<string, number | string>>;
  aligned_drawdown_curves: Array<{ date: string } & Record<string, number | string>>;
  benchmark_curve: Array<{ date: string; equity: number }>;
  benchmark_aligned_curve: Array<{ date: string; equity: number }>;
  auto_fetched_symbols?: string[];
}

export interface PortfolioRiskResult {
  portfolio_volatility: number;
  var_95: number;
  cvar_95: number;
  hhi: number;
  effective_n?: number;
  concentration_level?: string;
  correlation_matrix: Record<string, Record<string, number>>;
  weights?: Record<string, number>;
  sector_exposure?: Record<string, number>;
  warnings?: string[];
}

export interface PaperPosition {
  symbol: string;
  market: string;
  quantity: number;
  sellable_qty: number;
  avg_cost: number;
  mark_price?: number;
  market_price?: number;
  market_value: number;
  unrealized_pnl: number;
}

export interface PaperOrderRecord {
  order_id?: string;
  symbol: string;
  side: string;
  order_type?: string;
  requested_qty?: number;
  quantity?: number;
  status: string;
  reject_code?: string | null;
  reject_reason?: string | null;
  reason?: string | null;
  trade_date?: string;
  created_at?: string;
}

export interface PaperFillRecord {
  fill_id?: string;
  symbol: string;
  side: string;
  quantity: number;
  fill_price: number;
  commission?: number;
  transfer_fee?: number;
  stamp_duty?: number;
  trade_date?: string;
}

export interface PaperEquityPoint {
  trade_date?: string;
  date?: string;
  total_equity: number;
  cash: number;
  market_value: number;
  realized_pnl: number;
  unrealized_pnl: number;
}

export interface PaperAccountResponse {
  account_id: string;
  initial_capital: number;
  cash: number;
  market_value: number;
  total_equity: number;
  realized_pnl: number;
  unrealized_pnl: number;
  total_pnl: number;
  current_date: string;
  positions: PaperPosition[];
  market_rules?: Record<string, unknown>;
  simplification_assumptions?: string[];
  equity_history?: PaperEquityPoint[];
  orders?: PaperOrderRecord[];
  fills?: PaperFillRecord[];
}

function formatApiErrorDetail(rawDetail: unknown, fallbackStatus: number): string {
  if (typeof rawDetail === "string") {
    return rawDetail.trim() || `HTTP ${fallbackStatus}`;
  }
  if (Array.isArray(rawDetail)) {
    const parts = rawDetail
      .map((item) => {
        if (typeof item === "string") return item.trim();
        if (item && typeof item === "object") {
          const rec = item as { msg?: unknown; loc?: unknown };
          const msg = typeof rec.msg === "string" ? rec.msg.trim() : JSON.stringify(item);
          const loc = Array.isArray(rec.loc)
            ? rec.loc
                .slice(1)
                .map((p) => String(p))
                .filter(Boolean)
                .join(".")
            : "";
          return loc ? `${loc}: ${msg}` : msg;
        }
        return String(item);
      })
      .filter(Boolean);
    return parts.length > 0 ? parts.join("; ") : `HTTP ${fallbackStatus}`;
  }
  if (rawDetail && typeof rawDetail === "object") {
    try {
      return JSON.stringify(rawDetail);
    } catch {
      return `HTTP ${fallbackStatus}`;
    }
  }
  return rawDetail == null ? `HTTP ${fallbackStatus}` : String(rawDetail);
}

export async function apiJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text };
    }
  }
  if (!res.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data
        ? formatApiErrorDetail((data as { detail: unknown }).detail, res.status)
        : `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return data as T;
}

export function formatNumber(val: number | null | undefined, digits = 2): string {
  if (val === null || val === undefined || !Number.isFinite(val)) return "—";
  return val.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function isSafeHttpUrl(raw: string | null | undefined): raw is string {
  if (!raw) return false;
  try {
    const { protocol } = new URL(raw.trim());
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}

export function formatPct(val: number | null | undefined, digits = 2): string {
  if (val === null || val === undefined || !Number.isFinite(val)) return "—";
  return `${(val * 100).toFixed(digits)}%`;
}
