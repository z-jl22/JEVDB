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
  type EdgarFilingRow,
  type FundamentalRow,
  type MarketBar,
  type NewsItem,
  type SectorConstituent,
  type SectorSnapshot,
} from "../api";

export default function MarketView() {
  const [symbol, setSymbol] = useState("000858");
  const [market, setMarket] = useState<"CN" | "US">("CN");
  const [sectorName, setSectorName] = useState("电力");
  const [days, setDays] = useState(120);

  const [bars, setBars] = useState<MarketBar[]>([]);
  const [fundamentals, setFundamentals] = useState<FundamentalRow[]>([]);
  const [edgarFilings, setEdgarFilings] = useState<EdgarFilingRow[]>([]);
  const [news, setNews] = useState<NewsItem[]>([]);
  const [snapshots, setSnapshots] = useState<SectorSnapshot[]>([]);
  const [constituents, setConstituents] = useState<SectorConstituent[]>([]);
  const [statusMsg, setStatusMsg] = useState("");
  const [errorMsg, setErrorMsg] = useState("");
  const [loading, setLoading] = useState(false);
  const [ingesting, setIngesting] = useState(false);

  const chartElRef = useRef<HTMLDivElement | null>(null);
  const chartApiRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);

  const loadMarketData = useCallback(
    async (sym: string, mkt: "CN" | "US", sec: string, lookback: number) => {
      const cleanSym = sym.trim().toUpperCase();
      if (!cleanSym) return;
      setLoading(true);
      setErrorMsg("");
      try {
        const [barsRes, fundRes, newsRes, secRes] = await Promise.all([
          apiJson<{ bars: MarketBar[] }>(
            `/api/market/bars?symbol=${encodeURIComponent(cleanSym)}&market=${encodeURIComponent(mkt)}&days=${lookback}`
          ),
          apiJson<{
            fundamentals: FundamentalRow[];
            edgar_filings: EdgarFilingRow[];
          }>(
            `/api/market/fundamentals?symbol=${encodeURIComponent(cleanSym)}&market=${encodeURIComponent(mkt)}`
          ),
          apiJson<{ news: NewsItem[] }>(
            `/api/market/news?symbol=${encodeURIComponent(cleanSym)}&limit=20`
          ),
          apiJson<{
            snapshots: SectorSnapshot[];
            constituents: SectorConstituent[];
          }>(`/api/market/sector?name=${encodeURIComponent(sec.trim() || "电力")}`),
        ]);
        setBars(barsRes.bars || []);
        setFundamentals(fundRes.fundamentals || []);
        setEdgarFilings(fundRes.edgar_filings || []);
        setNews(newsRes.news || []);
        setSnapshots(secRes.snapshots || []);
        setConstituents(secRes.constituents || []);
      } catch (err) {
        setErrorMsg(err instanceof Error ? err.message : "行情数据加载失败");
      } finally {
        setLoading(false);
      }
    },
    []
  );

  useEffect(() => {
    void loadMarketData("000858", "CN", "电力", 120);
  }, [loadMarketData]);

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

  const handleIngest = async (kind: "stock" | "sector") => {
    setIngesting(true);
    setErrorMsg("");
    setStatusMsg("");
    try {
      if (kind === "sector") {
        const res = await apiJson<{ rows_written?: number; status?: string }>(
          "/api/market/ingest",
          {
            method: "POST",
            body: JSON.stringify({
              kind: "a_share_sector",
              sector: sectorName.trim() || "电力",
              days: 10,
            }),
          }
        );
        setStatusMsg(`行业板块「${sectorName}」同步完成 (${res.status || "ok"}, 写入 ${res.rows_written ?? 0} 行)`);
      } else {
        const ingestKind = market === "US" ? "us_stock" : "a_share_stock";
        const res = await apiJson<{ rows_written?: number; status?: string }>(
          "/api/market/ingest",
          {
            method: "POST",
            body: JSON.stringify({
              kind: ingestKind,
              symbol: symbol.trim().toUpperCase(),
              days,
              period: "6mo",
            }),
          }
        );
        setStatusMsg(`标的 ${symbol.toUpperCase()} 拉取完成 (${res.status || "ok"}, 写入 ${res.rows_written ?? 0} 行)`);
      }
      await loadMarketData(symbol, market, sectorName, days);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "按需数据拉取失败");
    } finally {
      setIngesting(false);
    }
  };

  const latestFund = fundamentals[0];
  const latestBar = bars.length > 0 ? bars[bars.length - 1] : null;

  return (
    <div>
      <div className="controls-row">
        <input
          className="input"
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          placeholder="代码如 000858 / AAPL"
          aria-label="股票代码"
        />
        <select
          className="select"
          value={market}
          onChange={(e) => setMarket(e.target.value === "US" ? "US" : "CN")}
          aria-label="市场选择"
        >
          <option value="CN">A股 (CN)</option>
          <option value="US">美股 (US)</option>
        </select>
        <input
          className="input"
          style={{ width: 90 }}
          type="number"
          min={5}
          max={1000}
          value={days}
          onChange={(e) => setDays(Number(e.target.value) || 120)}
          aria-label="K线天数"
        />
        <input
          className="input"
          style={{ width: 130 }}
          value={sectorName}
          onChange={(e) => setSectorName(e.target.value)}
          placeholder="行业如 电力"
          aria-label="行业板块"
        />
        <button
          type="button"
          className="btn"
          disabled={loading}
          onClick={() => void loadMarketData(symbol, market, sectorName, days)}
        >
          {loading ? "加载中..." : "查询行情与基本面"}
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          disabled={ingesting}
          onClick={() => void handleIngest("stock")}
        >
          {ingesting ? "拉取中..." : "按需拉取个股"}
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          disabled={ingesting}
          onClick={() => void handleIngest("sector")}
        >
          同步板块
        </button>
      </div>

      {statusMsg && <div className="ok-banner">{statusMsg}</div>}
      {errorMsg && <div className="err">{errorMsg}</div>}

      <div className="kpi-grid">
        <div className="kpi-card">
          <div className="kpi-label">最新收盘价 ({symbol.toUpperCase()})</div>
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
            {bars.length} 根 · {latestFund?.report_date || "静态/按需"}
          </div>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <h3 className="card-title">
          日线 K 线与成交量 ({symbol.toUpperCase()} · {market})
        </h3>
        <div ref={chartElRef} data-testid="market-chart" className="chart-box" />
      </div>

      <div className="grid-2">
        <div className="card">
          <h3 className="card-title">
            行业概览与成分股 ({sectorName} · 去重 {constituents.length} 只)
          </h3>
          {snapshots.length > 0 && (
            <div className="info-banner">
              最近板块快照：{snapshots[0].trade_date} · 涨跌幅{" "}
              {snapshots[0].change_pct !== null && snapshots[0].change_pct !== undefined
                ? `${formatNumber(snapshots[0].change_pct)}%`
                : "—"}{" "}
              · 领涨 {snapshots[0].leader_symbol || "—"}
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
                      暂无板块成分股（可点击“同步板块”按需抓取）
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
              <div className="muted">暂无新闻或申报记录（可点击“按需拉取个股”更新）</div>
            ) : (
              news.map((n) => (
                <div key={n.event_id} className="news-item">
                  <div className="news-title">{n.title}</div>
                  <div className="news-meta">
                    {n.published_at || "—"} · {n.source || "JEVDB"} ·{" "}
                    {n.sentiment_label || "neutral"}
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
