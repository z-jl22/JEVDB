import { useCallback, useEffect, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from "lightweight-charts";
import {
  apiJson,
  formatNumber,
  formatPct,
  isSafeHttpUrl,
  type EdgarFilingRow,
  type FundamentalRow,
  type MarketBar,
  type MarketBarsResponse,
  type ModelConnectionOptions,
  type NewsItem,
  type SectorConstituent,
} from "../api";

const DEFAULT_KLINE_DAYS = 120;
const MIN_KLINE_DAYS = 1;
const MAX_KLINE_DAYS = 1000;
const NEWS_SENTIMENT_LABELS = { positive: "正面", negative: "负面", neutral: "中性", insufficient: "信息不足" };

type MarketQuery = { symbol: string; market: "CN" | "US"; sector: string; days: number };

const DEFAULT_MARKET_QUERY: MarketQuery = { symbol: "000858", market: "CN", sector: "电力", days: DEFAULT_KLINE_DAYS };
const LAST_MARKET_QUERY_KEY = "jevdb.market.lastQuery";

function readLastMarketQuery(): MarketQuery {
  try {
    const saved = JSON.parse(localStorage.getItem(LAST_MARKET_QUERY_KEY) ?? "null") as Partial<MarketQuery> | null;
    if (
      typeof saved?.symbol === "string" && saved.symbol.trim() &&
      (saved.market === "CN" || saved.market === "US") &&
      typeof saved.sector === "string" && saved.sector.trim() &&
      typeof saved.days === "number" && Number.isInteger(saved.days) &&
      saved.days >= MIN_KLINE_DAYS && saved.days <= MAX_KLINE_DAYS
    ) {
      return { symbol: saved.symbol.trim(), market: saved.market, sector: saved.sector.trim(), days: saved.days };
    }
  } catch {
    // Restricted browser contexts may not expose local storage.
  }
  return DEFAULT_MARKET_QUERY;
}

function fetchSector(name: string, options?: RequestInit) {
  return apiJson<{ sector: string; constituents: SectorConstituent[]; suggestions: string[] }>(
    `/api/market/sector?name=${encodeURIComponent(name)}`,
    options,
  );
}

export default function MarketView({ modelConnection }: { modelConnection: ModelConnectionOptions }) {
  const [initialQuery] = useState(readLastMarketQuery);
  const [symbol, setSymbol] = useState(initialQuery.symbol);
  const [market, setMarket] = useState(initialQuery.market);
  const [sectorName, setSectorName] = useState(initialQuery.sector);
  const [queriedSector, setQueriedSector] = useState(initialQuery.sector);
  const [daysInput, setDaysInput] = useState(String(initialQuery.days));
  const [queriedStock, setQueriedStock] = useState<{
    symbol: string; market: "CN" | "US"; days: number; name: string | null;
  }>({
    symbol: initialQuery.symbol,
    market: initialQuery.market,
    days: initialQuery.days,
    name: null,
  });

  const [bars, setBars] = useState<MarketBar[]>([]);
  const [fundamentals, setFundamentals] = useState<FundamentalRow[]>([]);
  const [edgarFilings, setEdgarFilings] = useState<EdgarFilingRow[]>([]);
  const [news, setNews] = useState<NewsItem[]>([]);
  const [constituents, setConstituents] = useState<SectorConstituent[]>([]);
  const [statusMsg, setStatusMsg] = useState("");
  const [errorMsg, setErrorMsg] = useState("");
  const [sectorFeedback, setSectorFeedback] = useState<{ message: string; failed?: boolean } | null>(null);
  const [sectorSuggestions, setSectorSuggestions] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [ingesting, setIngesting] = useState<"stock" | "sector" | null>(null);
  const marketRequestRef = useRef<AbortController | null>(null);

  const chartElRef = useRef<HTMLDivElement | null>(null);
  const chartApiRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);

  const loadMarketData = useCallback(
    async (
      { symbol: sym, market: mkt, sector: sec, days: lookback }: MarketQuery,
      { fetchMissing = false, remember = false } = {},
    ) => {
      const cleanSym = sym.trim().toUpperCase();
      if (!cleanSym) {
        setErrorMsg("请输入股票代码。");
        return;
      }
      marketRequestRef.current?.abort();
      const controller = new AbortController();
      marketRequestRef.current = controller;
      const options = { signal: controller.signal };
      const cleanSector = sec.trim() || DEFAULT_MARKET_QUERY.sector;
      setLoading(true);
      setErrorMsg("");
      const [barsRes, fundRes, newsRes, secRes] = await Promise.allSettled([
        apiJson<MarketBarsResponse>(
          `/api/market/bars?symbol=${encodeURIComponent(cleanSym)}&market=${mkt}&days=${lookback}&fetch_missing=${fetchMissing}`,
          options,
        ),
        apiJson<{
          fundamentals: FundamentalRow[];
          edgar_filings: EdgarFilingRow[];
        }>(
          `/api/market/fundamentals?symbol=${encodeURIComponent(cleanSym)}&market=${mkt}`,
          options,
        ),
        apiJson<{ news: NewsItem[] }>(
          `/api/market/news?symbol=${encodeURIComponent(cleanSym)}&market=${mkt}&limit=20`,
          options,
        ),
        fetchSector(cleanSector, options),
      ]);
      if (controller.signal.aborted) return;
      const marketBars = barsRes.status === "fulfilled" ? barsRes.value : null;
      const stock = { symbol: marketBars?.symbol ?? cleanSym, name: marketBars?.name ?? null, market: mkt, days: lookback };
      setQueriedStock(stock);
      setBars(marketBars?.bars ?? []);
      setFundamentals(fundRes.status === "fulfilled" ? fundRes.value.fundamentals : []);
      setEdgarFilings(fundRes.status === "fulfilled" ? fundRes.value.edgar_filings : []);
      setNews(newsRes.status === "fulfilled" ? newsRes.value.news : []);
      const sector = secRes.status === "fulfilled" ? secRes.value : null;
      setQueriedSector(sector?.sector || cleanSector);
      setConstituents(sector?.constituents ?? []);
      setSectorSuggestions(sector?.suggestions ?? []);
      const hasStockData = Boolean(
        marketBars?.bars.length ||
        (fundRes.status === "fulfilled" && (fundRes.value.fundamentals.length || fundRes.value.edgar_filings.length)) ||
        (newsRes.status === "fulfilled" && newsRes.value.news.length)
      );
      if (remember && hasStockData) {
        const query: MarketQuery = { symbol: stock.symbol, market: mkt, sector: sector?.sector || cleanSector, days: lookback };
        try {
          localStorage.setItem(LAST_MARKET_QUERY_KEY, JSON.stringify(query));
        } catch {
          // Query results can still be displayed when local storage is unavailable.
        }
      }
      if (barsRes.status === "rejected") {
        setErrorMsg(barsRes.reason instanceof Error ? barsRes.reason.message : "日线数据加载失败。");
      } else {
        const failed = [[fundRes, "基本面"], [newsRes, "新闻"], [secRes, "行业"]] as const;
        const names = failed.filter(([result]) => result.status === "rejected").map(([, name]) => name);
        if (names.length) setErrorMsg(`${names.join("、")}数据加载失败。`);
      }
      setLoading(false);
    },
    []
  );

  useEffect(() => {
    void loadMarketData(initialQuery);
    return () => marketRequestRef.current?.abort();
  }, [initialQuery, loadMarketData]);

  useEffect(() => {
    const el = chartElRef.current;
    if (!el) return;

    const chart = createChart(el, {
      width: Math.max(el.clientWidth, 320),
      height: Math.max(el.clientHeight, 340),
      layout: {
        background: { type: ColorType.Solid, color: "#0a111f" },
        textColor: "#cbd5e1",
      },
      grid: {
        vertLines: { color: "rgba(32, 49, 79, 0.55)" },
        horzLines: { color: "rgba(32, 49, 79, 0.55)" },
      },
      timeScale: { borderColor: "#20314f", timeVisible: true },
      rightPriceScale: { borderColor: "#20314f" },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#ef4444",
      downColor: "#22c55e",
      borderVisible: false,
      wickUpColor: "#ef4444",
      wickDownColor: "#22c55e",
    });

    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "",
    });
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    chartApiRef.current = chart;
    candleRef.current = candleSeries;
    volRef.current = volumeSeries;

    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const w = Math.floor(entry.contentRect.width);
        const h = Math.floor(entry.contentRect.height);
        if (w > 0 && h > 0) {
          chart.applyOptions({ width: w, height: h });
        }
      }
    });
    ro.observe(el);

    return () => {
      ro.disconnect();
      chart.remove();
      chartApiRef.current = null;
      candleRef.current = null;
      volRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!candleRef.current || !volRef.current) return;
    const dedup = new Map<string, MarketBar>();
    for (const b of bars) {
      const dt = (b.date || b.trade_date || "").slice(0, 10);
      if (dt && Number.isFinite(b.open) && Number.isFinite(b.high) && Number.isFinite(b.low) && Number.isFinite(b.close)) {
        dedup.set(dt, b);
      }
    }
    const sorted = Array.from(dedup.entries()).sort((a, b) => a[0].localeCompare(b[0]));

    candleRef.current.setData(
      sorted.map(([dt, b]) => ({
        time: dt as Time,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      }))
    );

    volRef.current.setData(
      sorted.map(([dt, b]) => ({
        time: dt as Time,
        value: typeof b.volume === "number" && Number.isFinite(b.volume) ? b.volume : 0,
        color: b.close >= b.open ? "rgba(239, 68, 68, 0.45)" : "rgba(34, 197, 94, 0.45)",
      }))
    );

    if (sorted.length > 0 && chartApiRef.current) {
      chartApiRef.current.timeScale().fitContent();
    }
  }, [bars]);

  const readDays = () => {
    const count = daysInput === "" ? DEFAULT_KLINE_DAYS : Number(daysInput);
    if (!Number.isInteger(count) || count < MIN_KLINE_DAYS || count > MAX_KLINE_DAYS) {
      setErrorMsg(`K线天数请输入 ${MIN_KLINE_DAYS} 到 ${MAX_KLINE_DAYS} 之间的整数。`);
      return null;
    }
    return count;
  };

  const handleIngest = async (kind: "stock" | "sector") => {
    const lookback = kind === "stock" ? readDays() : null;
    if (kind === "stock" && lookback === null) return;
    setIngesting(kind);
    if (kind === "sector") {
      setSectorFeedback(null);
      setSectorSuggestions([]);
    } else {
      setErrorMsg("");
      setStatusMsg("");
    }
    try {
      if (kind === "sector") {
        const cleanSector = sectorName.trim() || "电力";
        const res = await apiJson<{
          rows_written?: number;
          status?: string;
          sector?: string;
          match_status?: "matched" | "unmatched" | "suggestions";
          suggestions?: string[];
          missing_constituent_quotes?: number;
        }>(
          "/api/market/ingest",
          {
            method: "POST",
            body: JSON.stringify({
              kind: "a_share_sector",
              sector: cleanSector,
              days: 10,
            }),
          }
        );
        if (res.match_status === "suggestions" || res.match_status === "unmatched") {
          setQueriedSector(cleanSector);
          setConstituents([]);
          setSectorSuggestions(res.suggestions ?? []);
          if (res.match_status === "unmatched") {
            setSectorFeedback({ message: `暂未找到「${cleanSector}」的板块数据。`, failed: true });
          }
          return;
        }
        const data = await fetchSector(res.sector || cleanSector);
        const matched = data.sector || res.sector || cleanSector;
        setQueriedSector(matched);
        setConstituents(data.constituents);
        const failed = res.status === "error";
        const missing = res.missing_constituent_quotes ?? 0;
        const description = matched === cleanSector ? `行业板块「${matched}」` : `「${cleanSector}」已匹配为「${matched}」`;
        setSectorFeedback({
          message: `${description}，同步${failed ? "失败" : "完成"} (${res.status || "ok"}, 写入 ${res.rows_written ?? 0} 行)` +
            (missing ? `。本次有 ${missing} 只成分股的行情或 PE 未取全。` : ""),
          failed,
        });
      } else if (lookback !== null) {
        const ingestKind = market === "US" ? "us_stock" : "a_share_stock";
        const res = await apiJson<{
          rows_written?: number;
          status?: string;
          news_analysis?: { status: "complete" | "unconfigured" | "unavailable"; analyzed: number };
        }>(
          "/api/market/ingest",
          {
            method: "POST",
            body: JSON.stringify({
              kind: ingestKind,
              symbol: symbol.trim().toUpperCase(),
              days: lookback,
              analyze_news: true,
              news_connection: modelConnection,
            }),
          }
        );
        const analysis = res.news_analysis;
        let analysisMessage = "";
        if (analysis?.status === "unconfigured") {
          analysisMessage = "。请在投研工作台填写 API Key 后重新拉取，以启用新闻模型分析。";
        } else if (analysis?.status === "unavailable") {
          analysisMessage = "。新闻已保存，模型分析暂未完成，再次点击按需拉取个股以完成分析。";
        } else if (analysis?.analyzed) {
          analysisMessage = `。已分析 ${analysis.analyzed} 条新闻。`;
        }
        setStatusMsg(`标的 ${symbol.toUpperCase()} 拉取完成 (${res.status || "ok"}, 写入 ${res.rows_written ?? 0} 行)${analysisMessage}`);
        await loadMarketData({ symbol, market, sector: sectorName, days: lookback }, { remember: true });
      }
    } catch (err) {
      if (kind === "sector") {
        setSectorFeedback({ message: err instanceof Error ? err.message : "板块同步失败。", failed: true });
      } else {
        setErrorMsg(err instanceof Error ? err.message : "按需数据拉取失败");
      }
    } finally {
      setIngesting(null);
    }
  };

  const latestFund = fundamentals[0];
  const latestBar = bars.length > 0 ? bars[bars.length - 1] : null;
  const stockTitle = [queriedStock.name, queriedStock.symbol, queriedStock.market].filter(Boolean).join(" · ");

  return (
    <div>
      <div className="market-query-fields">
        <label>
          <span>股票代码</span>
          <input
            className="input"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            placeholder="如 000858 / AAPL"
          />
        </label>
        <label>
          <span>市场选择</span>
          <select
            className="select"
            value={market}
            onChange={(e) => setMarket(e.target.value === "US" ? "US" : "CN")}
          >
            <option value="CN">A股 (CN)</option>
            <option value="US">美股 (US)</option>
          </select>
        </label>
        <label>
          <span>K线天数</span>
          <input
            className="input"
            type="number"
            min={MIN_KLINE_DAYS}
            max={MAX_KLINE_DAYS}
            value={daysInput}
            placeholder={String(DEFAULT_KLINE_DAYS)}
            onChange={(e) => setDaysInput(e.target.value)}
          />
        </label>
      </div>
      <div className="controls-row">
        <button
          type="button"
          className="btn"
          disabled={loading || Boolean(ingesting)}
          onClick={() => {
            setStatusMsg("");
            const lookback = readDays();
            if (lookback !== null) {
              void loadMarketData({ symbol, market, sector: sectorName, days: lookback }, { fetchMissing: true, remember: true });
            }
          }}
        >
          {loading ? "加载中..." : "查询行情与基本面"}
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          disabled={Boolean(ingesting) || loading}
          onClick={() => void handleIngest("stock")}
        >
          {ingesting === "stock" ? "拉取中..." : "按需拉取个股"}
        </button>
      </div>

      {statusMsg && <div className="ok-banner">{statusMsg}</div>}
      {errorMsg && <div className="err">{errorMsg}</div>}

      <div className="kpi-grid">
        <div className="kpi-card">
          <div className="kpi-label">最新收盘价 ({queriedStock.symbol})</div>
          <div className="kpi-val">{latestBar ? formatNumber(latestBar.close) : "—"}</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">市盈率 PE (TTM)</div>
          <div className="kpi-val">{formatNumber(latestFund?.pe_ttm)}</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">市净率 PB</div>
          <div className="kpi-val">{formatNumber(latestFund?.pb)}</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">净资产收益率 ROE</div>
          <div className="kpi-val">
            {latestFund?.roe !== undefined && latestFund?.roe !== null
              ? Math.abs(latestFund.roe) <= 1.5
                ? formatPct(latestFund.roe)
                : `${formatNumber(latestFund.roe)}%`
              : "—"}
          </div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">资产负债率</div>
          <div className="kpi-val">
            {latestFund?.debt_ratio !== undefined && latestFund?.debt_ratio !== null
              ? Math.abs(latestFund.debt_ratio) <= 1.5
                ? formatPct(latestFund.debt_ratio)
                : `${formatNumber(latestFund.debt_ratio)}%`
              : "—"}
          </div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">样本K线 / 报告期</div>
          <div className="kpi-val" style={{ fontSize: 13 }}>
            {bars.length} 根 / 请求 {queriedStock.days} 根 · {latestFund?.report_date || "静态/按需"}
          </div>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <h3 className="card-title">
          日线 K 线与成交量 ({stockTitle})
        </h3>
        {!loading && bars.length === 0 && (
          <div className="info-banner" data-testid="market-bars-empty">
            暂无日线数据。请核对股票代码和市场，或稍后重试。
          </div>
        )}
        <div ref={chartElRef} data-testid="market-chart" className="chart-box" />
      </div>

      <div className="grid-2 market-details">
        <div className="card">
          <h3 className="card-title">
            行业概览与成分股 ({queriedSector} · 去重 {constituents.length} 只)
          </h3>
          <div className="controls-row market-sector-controls">
            <label>
              <span>行业板块</span>
              <input
                className="input"
                value={sectorName}
                onChange={(e) => {
                  setSectorName(e.target.value);
                  setSectorFeedback(null);
                  setSectorSuggestions([]);
                }}
                placeholder="如 电力"
              />
            </label>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={Boolean(ingesting) || loading}
              onClick={() => void handleIngest("sector")}
            >
              {ingesting === "sector" ? "同步中..." : "同步板块"}
            </button>
          </div>
          {sectorFeedback && (
            <div className={sectorFeedback.failed ? "err" : "ok-banner"} role="status">{sectorFeedback.message}</div>
          )}
          {sectorSuggestions.length > 0 && (
            <div className="controls-row">
              <span className="muted" role="status">请选择更具体的板块：</span>
              {sectorSuggestions.map((candidate) => (
                <button
                  key={candidate}
                  type="button"
                  className="btn-chip"
                  aria-pressed={sectorName === candidate}
                  onClick={() => setSectorName(candidate)}
                >
                  {candidate}
                </button>
              ))}
            </div>
          )}
          <div className="table-wrap" style={{ maxHeight: 240 }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>代码</th>
                  <th>名称</th>
                  <th>最新价</th>
                  <th>涨跌幅%</th>
                  <th>PE</th>
                  <th>来源</th>
                </tr>
              </thead>
              <tbody>
                {constituents.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="muted">
                      {sectorSuggestions.length > 0 ? "选定候选板块后，点击「同步板块」。" : "暂无板块成分股（可点击「同步板块」按需抓取）"}
                    </td>
                  </tr>
                ) : (
                  constituents.slice(0, 15).map((c) => (
                    <tr key={`${c.sector_name}-${c.symbol}`}>
                      <td>{c.symbol}</td>
                      <td>{c.name || "—"}</td>
                      <td>{formatNumber(c.price)}</td>
                      <td className={(c.change_pct ?? 0) >= 0 ? "pos-num" : "neg-num"}>
                        {c.change_pct !== null && c.change_pct !== undefined
                          ? `${formatNumber(c.change_pct)}%`
                          : "—"}
                      </td>
                      <td>{formatNumber(c.pe)}</td>
                      <td>{c.source || "—"}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card">
          <h3 className="card-title">新闻公告与 SEC EDGAR 申报 ({news.length + edgarFilings.length})</h3>
          <p className="news-update-hint">
            点击「按需拉取个股」更新新闻并分析情绪。模型使用投研工作台的连接配置，待分析的新闻显示关键词判断结果。
          </p>
          <div className="list-scroll">
            {edgarFilings.map((f) => (
              <div key={f.accession_no} className="news-item">
                <div className="news-title">
                  [{f.form || "EDGAR"}] {f.title || f.primary_doc || f.accession_no}
                </div>
                <div className="news-meta">
                  申报日 {f.filing_date || "—"} · 报告期 {f.report_date || f.accession_no}
                </div>
              </div>
            ))}
            {news.length === 0 && edgarFilings.length === 0 ? (
              <div className="muted">暂无新闻或申报记录</div>
            ) : (
              news.map((n) => (
                <div key={n.event_id} className="news-item">
                  <div className="news-title">
                    {isSafeHttpUrl(n.url) ? (
                      <a href={n.url.trim()} target="_blank" rel="noopener noreferrer">{n.title}</a>
                    ) : n.title}
                  </div>
                  <div className="news-meta">
                    {n.published_at || "—"} · {n.source || "JEVDB"} ·{" "}
                    <span className="news-sentiment" data-sentiment={n.sentiment}>
                      {n.sentiment_source === "keyword" && n.sentiment === "neutral"
                        ? "待分析"
                        : n.sentiment ? NEWS_SENTIMENT_LABELS[n.sentiment] : "待分析"}
                    </span>
                    {n.sentiment_source === "model" && " · 模型分析"}
                    {n.sentiment_source === "keyword" && n.sentiment !== "neutral" && " · 关键词判断"}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
