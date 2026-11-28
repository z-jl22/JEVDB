import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import * as echarts from "../terminalCharts";
import {
  apiJson,
  formatNumber,
  formatPct,
  type CompareResult,
  type PortfolioRiskResult,
  type StrategySpec,
} from "../api";
import { chartTheme } from "../chartTheme";

const SERIES_COLORS = chartTheme.series;

function isoDaysAgo(days: number): string {
  const dt = new Date();
  dt.setDate(dt.getDate() - days);
  const yyyy = dt.getFullYear();
  const mm = String(dt.getMonth() + 1).padStart(2, "0");
  const dd = String(dt.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

export default function QuantView() {
  const [strategies, setStrategies] = useState<StrategySpec[]>([]);
  const [selectedIds, setSelectedIds] = useState<string[]>([
    "value_quality_margin_of_safety",
    "cn_daily_momentum_sentiment",
  ]);
  const [symbolsText, setSymbolsText] = useState("000858,601318");
  const [startDate, setStartDate] = useState("2025-04-24");
  const [endDate, setEndDate] = useState("2025-05-15");
  const [capital, setCapital] = useState(100000);
  const [market, setMarket] = useState<"CN" | "US">("CN");
  const [autoFetch, setAutoFetch] = useState(true);

  const [compareRes, setCompareRes] = useState<CompareResult | null>(null);
  const [riskRes, setRiskRes] = useState<PortfolioRiskResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [completedRun, setCompletedRun] = useState<{
    symbols: string; start: string; end: string; capital: number; market: "CN" | "US"; strategies: string[];
  } | null>(null);
  const resultsHeading = useRef<HTMLHeadingElement>(null);
  const [errorMsg, setErrorMsg] = useState("");
  const [statusMsg, setStatusMsg] = useState("");

  // Create strategy inline form
  const [showCreate, setShowCreate] = useState(false);
  const [newStratId, setNewStratId] = useState("custom_daily_mom");
  const [newStratName, setNewStratName] = useState("自定义日频动量策略");
  const [newStratStyle, setNewStratStyle] = useState<"quant_daily" | "value" | "sentiment_hybrid">(
    "quant_daily"
  );
  const [newStratDesc, setNewStratDesc] = useState("基于日内动量因子的量化交易策略");

  const chartElRef = useRef<HTMLDivElement | null>(null);
  const chartInstRef = useRef<echarts.ECharts | null>(null);
  const corrElRef = useRef<HTMLDivElement | null>(null);
  const corrInstRef = useRef<echarts.ECharts | null>(null);

  const runComparisonAndRisk = useCallback(
    async (
      stratIds: string[],
      symsRaw: string,
      sDate: string,
      eDate: string,
      initCap: number,
      mkt: "CN" | "US",
      shouldAutoFetch = true
    ) => {
      const syms = symsRaw
        .split(",")
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean);
      if (stratIds.length === 0 || syms.length === 0) {
        setErrorMsg("请至少选择 1 个策略并输入 1 个股票代码。");
        return;
      }
      if (!sDate || !eDate) {
        setErrorMsg("请同时选择回测开始日期与结束日期。");
        return;
      }
      if (sDate > eDate) {
        setErrorMsg(`开始日期 (${sDate}) 不能晚于结束日期 (${eDate})。`);
        return;
      }
      setLoading(true);
      setErrorMsg("");
      setStatusMsg("");
      try {
        const equalWeight = Number((1 / syms.length).toFixed(4));
        const holdings = syms.map((sym) => ({
          symbol: sym,
          market: mkt,
          weight: equalWeight,
          sector: mkt === "CN" ? "A股核心" : "US Equity",
        }));

        // Run compare first so any auto-fetched bars in [sDate, eDate] are saved before risk calc
        const cmp = await apiJson<CompareResult>("/api/quant/compare", {
          method: "POST",
          body: JSON.stringify({
            strategy_ids: stratIds,
            symbols: syms,
            start_date: sDate,
            end_date: eDate,
            initial_capital: initCap,
            market: mkt,
            auto_fetch: shouldAutoFetch,
          }),
        });
        const risk = await apiJson<PortfolioRiskResult>("/api/quant/portfolio-risk", {
          method: "POST",
          body: JSON.stringify({
            holdings,
            start_date: sDate,
            end_date: eDate,
            confidence: 0.95,
          }),
        });
        setCompletedRun({ symbols: syms.join(","), start: sDate, end: eDate, capital: initCap, market: mkt, strategies: stratIds });
        setCompareRes(cmp);
        setRiskRes(risk);
        if (cmp.auto_fetched_symbols && cmp.auto_fetched_symbols.length > 0) {
          setStatusMsg(
            `已自动联网补齐 ${cmp.auto_fetched_symbols.join(", ")} 在 ${sDate} ~ ${eDate} 区间的历史行情并完成回测。`
          );
        }
      } catch (err) {
        setErrorMsg(err instanceof Error ? err.message : "策略回测或风险计算失败");
      } finally {
        setLoading(false);
      }
    },
    []
  );

  useEffect(() => {
    const bootstrap = async () => {
      try {
        const res = await apiJson<{ strategies: StrategySpec[] }>("/api/quant/strategies");
        const list = res.strategies || [];
        setStrategies(list);
        const defaultIds = list
          .filter((s) =>
            ["value_quality_margin_of_safety", "cn_daily_momentum_sentiment"].includes(
              s.strategy_id
            )
          )
          .map((s) => s.strategy_id);
        const idsToRun =
          defaultIds.length > 0 ? defaultIds : list.slice(0, 2).map((s) => s.strategy_id);
        setSelectedIds(idsToRun);
        if (idsToRun.length > 0) {
          await runComparisonAndRisk(
            idsToRun,
            "000858,601318",
            "2025-04-24",
            "2025-05-15",
            100000,
            "CN"
          );
        }
      } catch (err) {
        setErrorMsg(err instanceof Error ? err.message : "策略列表加载失败");
      }
    };
    void bootstrap();
  }, [runComparisonAndRisk]);

  // Initialize ECharts instances with ResizeObserver
  useEffect(() => {
    const el = chartElRef.current;
    if (!el) return;
    const chart = echarts.init(el);
    chartInstRef.current = chart;
    const ro = new ResizeObserver(() => {
      chart.resize();
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.dispose();
      chartInstRef.current = null;
    };
  }, []);

  useEffect(() => {
    const el = corrElRef.current;
    if (!el) return;
    const chart = echarts.init(el);
    corrInstRef.current = chart;
    const ro = new ResizeObserver(() => {
      chart.resize();
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.dispose();
      corrInstRef.current = null;
    };
  }, []);

  // Update backtest equity & drawdown chart
  useEffect(() => {
    const chart = chartInstRef.current;
    if (!chart) return;

    const eqRows = Array.isArray(compareRes?.aligned_equity_curves)
      ? compareRes.aligned_equity_curves
      : [];
    const ddRows = Array.isArray(compareRes?.aligned_drawdown_curves)
      ? compareRes.aligned_drawdown_curves
      : [];
    const stratKeys = compareRes?.strategy_ids || [];
    const strategyName = (id: string) => compareRes?.results?.[id]?.strategy_name || id;
    const dates =
      eqRows.length > 0
        ? eqRows.map((p) => String(p.date))
        : (compareRes?.benchmark_curve || []).map((p) => p.date);

    const series: echarts.SeriesOption[] = stratKeys.map((sid, idx) => ({
      name: `${strategyName(sid)} 净值`,
      type: "line",
      smooth: true,
      showSymbol: false,
      yAxisIndex: 0,
      lineStyle: { width: 2.2, color: SERIES_COLORS[idx % SERIES_COLORS.length] },
      itemStyle: { color: SERIES_COLORS[idx % SERIES_COLORS.length] },
      data: eqRows.map((p) => {
        if (p[sid] == null) return null;
        const val = Number(p[sid]);
        return Number.isFinite(val) ? Number(val.toFixed(2)) : null;
      }),
    }));

    if (compareRes?.benchmark_curve && compareRes.benchmark_curve.length > 0) {
      series.push({
        name: "等权基准",
        type: "line",
        smooth: true,
        showSymbol: false,
        yAxisIndex: 0,
        lineStyle: { width: 1.5, type: "dashed", color: chartTheme.muted },
        itemStyle: { color: chartTheme.muted },
        data: compareRes.benchmark_curve.map((p) => Number(p.equity.toFixed(2))),
      });
    }

    if (stratKeys.length > 0 && ddRows.length > 0) {
      const primarySid = stratKeys[0];
      series.push({
        name: `${strategyName(primarySid)} 回撤`,
        type: "line",
        yAxisIndex: 1,
        showSymbol: false,
        lineStyle: { width: 1.2, color: chartTheme.down },
        itemStyle: { color: chartTheme.down },
        areaStyle: { color: chartTheme.drawdownFill },
        data: ddRows.map((p) => {
          if (p[primarySid] == null) return null;
          const val = Number(p[primarySid]);
          return Number.isFinite(val) ? Number((val * 100).toFixed(2)) : null;
        }),
      });
    }

    chart.setOption(
      {
        animation: false,
        backgroundColor: "transparent",
        textStyle: { fontFamily: "system-ui, sans-serif" },
        tooltip: { ...chartTheme.tooltip, trigger: "axis" },
        legend: {
          type: "scroll",
          top: 6,
          left: 12,
          right: 12,
          pageTextStyle: { color: chartTheme.muted },
          pageIconColor: chartTheme.accent,
          pageIconInactiveColor: chartTheme.border,
          textStyle: { color: chartTheme.text, fontSize: 11 },
        },
        grid: { left: 58, right: 54, top: 55, bottom: 28 },
        xAxis: {
          type: "category",
          data: dates,
          axisLabel: { color: chartTheme.muted, fontSize: 11 },
          axisLine: { lineStyle: { color: chartTheme.border } },
        },
        yAxis: [
          {
            type: "value",
            name: "净值",
            scale: true,
            nameTextStyle: { color: chartTheme.muted, fontSize: 11 },
            axisLabel: { color: chartTheme.muted, fontSize: 11 },
            splitLine: { lineStyle: { color: chartTheme.grid } },
          },
          {
            type: "value",
            name: "回撤%",
            nameTextStyle: { color: chartTheme.muted, fontSize: 11 },
            axisLabel: { color: chartTheme.muted, fontSize: 11 },
            splitLine: { show: false },
          },
        ],
        series:
          series.length > 0
            ? series
            : [
                {
                  name: "待回测",
                  type: "line",
                  data: [0],
                },
              ],
      },
      true
    );
  }, [compareRes]);

  // Update portfolio correlation heatmap
  useEffect(() => {
    const chart = corrInstRef.current;
    if (!chart) return;
    const matrix = riskRes?.correlation_matrix || {};
    const syms = Object.keys(matrix);
    const data: Array<[number, number, number]> = [];
    syms.forEach((rSym, rIdx) => {
      syms.forEach((cSym, cIdx) => {
        const val = matrix[rSym]?.[cSym] ?? 0;
        data.push([cIdx, rIdx, Number(val.toFixed(2))]);
      });
    });

    chart.setOption(
      {
        animation: false,
        backgroundColor: "transparent",
        textStyle: { fontFamily: "system-ui, sans-serif" },
        tooltip: { ...chartTheme.tooltip, position: "top" },
        grid: { left: 68, right: 24, top: 20, bottom: 38 },
        xAxis: {
          type: "category",
          data: syms.length > 0 ? syms : ["暂无"],
          axisLabel: { color: chartTheme.text, fontSize: 11 },
        },
        yAxis: {
          type: "category",
          data: syms.length > 0 ? syms : ["暂无"],
          axisLabel: { color: chartTheme.text, fontSize: 11 },
        },
        visualMap: {
          min: -1,
          max: 1,
          show: false,
          inRange: {
            color: [chartTheme.down, chartTheme.grid, chartTheme.up],
          },
        },
        series: [
          {
            name: "相关性",
            type: "heatmap",
            data: data.length > 0 ? data : [[0, 0, 1]],
            label: { show: true, color: "#f8fafc", fontSize: 12 },
          },
        ],
      },
      true
    );
  }, [riskRes]);

  const toggleStrategy = (sid: string) => {
    setSelectedIds((prev) =>
      prev.includes(sid) ? prev.filter((x) => x !== sid) : [...prev, sid]
    );
  };

  const handleCreateStrategy = async (e: FormEvent) => {
    e.preventDefault();
    setErrorMsg("");
    setStatusMsg("");
    try {
      const saved = await apiJson<StrategySpec>("/api/quant/strategies", {
        method: "POST",
        body: JSON.stringify({
          strategy_id: newStratId.trim(),
          name: newStratName.trim(),
          style: newStratStyle,
          market,
          description: newStratDesc.trim(),
          params: { position_weight: 0.35 },
          rules: {
            buy: { field: "momentum_return", op: ">", value: 0.0 },
            sell: { field: "momentum_return", op: "<", value: 0.0 },
          },
          source: "user",
        }),
      });
      setStrategies((prev) => {
        const filtered = prev.filter((s) => s.strategy_id !== saved.strategy_id);
        return [...filtered, saved];
      });
      setSelectedIds((prev) =>
        prev.includes(saved.strategy_id) ? prev : [...prev, saved.strategy_id]
      );
      setStatusMsg(`已保存策略「${saved.strategy_id}」`);
      setShowCreate(false);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "策略创建失败");
    }
  };

  const applyDatePreset = (nextStart: string, nextEnd: string) => {
    setStartDate(nextStart);
    setEndDate(nextEnd);
    void runComparisonAndRisk(
      selectedIds,
      symbolsText,
      nextStart,
      nextEnd,
      capital,
      market,
      autoFetch
    );
  };

  const handleRunSubmit = (e: FormEvent) => {
    e.preventDefault();
    void runComparisonAndRisk(
      selectedIds,
      symbolsText,
      startDate,
      endDate,
      capital,
      market,
      autoFetch
    );
  };

  const handleMarketChange = (nextMkt: "CN" | "US") => {
    setMarket(nextMkt);
    const trimmed = symbolsText.trim().toUpperCase();
    if (nextMkt === "US" && trimmed === "000858,601318") {
      setSymbolsText("AAPL,MSFT");
    } else if (nextMkt === "CN" && trimmed === "AAPL,MSFT") {
      setSymbolsText("000858,601318");
    }
  };

  const eqCount = Array.isArray(compareRes?.aligned_equity_curves)
    ? compareRes.aligned_equity_curves.length
    : 0;
  const activeStart =
    eqCount > 0 ? String(compareRes!.aligned_equity_curves[0].date) : compareRes?.start_date || startDate;
  const activeEnd =
    eqCount > 0
      ? String(compareRes!.aligned_equity_curves[eqCount - 1].date)
      : compareRes?.end_date || endDate;

  const resultIsPrevious = completedRun && (
    loading || Boolean(errorMsg) || completedRun.start !== startDate || completedRun.end !== endDate ||
    completedRun.market !== market || completedRun.capital !== capital ||
    completedRun.symbols !== symbolsText.split(",").map((symbol) => symbol.trim().toUpperCase()).filter(Boolean).join(",") ||
    completedRun.strategies.join(",") !== selectedIds.join(",")
  );

  const styleNames: Record<string, string> = {
    value: "价值投资", quant_daily: "日频量化", sentiment_hybrid: "情绪混合",
  };

  const datePresets = [
    { label: "静态演示 (25/04-05)", start: "2025-04-24", end: "2025-05-15" },
    { label: "近 1 月", start: isoDaysAgo(30), end: isoDaysAgo(0) },
    { label: "近 3 月", start: isoDaysAgo(90), end: isoDaysAgo(0) },
    { label: "近 6 月", start: isoDaysAgo(180), end: isoDaysAgo(0) },
    { label: "近 1 年", start: isoDaysAgo(365), end: isoDaysAgo(0) },
  ];

  return (
    <div className="terminal-split quant-page">
      <aside className="panel setup-panel flow" aria-label="回测配置">
        <div className="panel-heading"><h2>回测配置</h2>
          <button type="button" className="btn-mini mobile-jump"
            onClick={() => resultsHeading.current?.focus()}>查看结果</button>
        </div>
        <div className="checkbox-row strategy-list" role="group" aria-label="对比策略">
        {strategies.map((st) => (
          <label key={st.strategy_id} className="checkbox-pill">
            <input
              type="checkbox"
              checked={selectedIds.includes(st.strategy_id)}
              onChange={() => toggleStrategy(st.strategy_id)}
            />
            <span>
              {st.name}
            </span>
          </label>
        ))}
      </div>

      <form onSubmit={handleRunSubmit} className="setup-form">
        <label className="field wide"><span>标的代码</span>
        <input
          className="input"
          value={symbolsText}
          onChange={(e) => setSymbolsText(e.target.value)}
          placeholder="回测标的逗号分隔，如 000858,601318"
          aria-label="回测标的列表"
        /></label>
        <label className="field"><span>市场</span>
        <select
          className="select"
          value={market}
          onChange={(e) => handleMarketChange(e.target.value === "US" ? "US" : "CN")}
          aria-label="回测市场"
        >
          <option value="CN">A股 (CN)</option>
          <option value="US">美股 (US)</option>
        </select></label>
        <label className="date-field">
          <span>开始</span>
          <input
            className="input"
            type="date"
            data-testid="quant-start-date"
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
            aria-label="回测开始日期"
          />
        </label>
        <label className="date-field">
          <span>结束</span>
          <input
            className="input"
            type="date"
            data-testid="quant-end-date"
            value={endDate}
            onChange={(e) => setEndDate(e.target.value)}
            aria-label="回测结束日期"
          />
        </label>
        <label className="field"><span>初始资金</span>
        <input
          className="input"
          required
          type="number"
          min={1000}
          step="any"
          value={Number.isNaN(capital) ? "" : capital}
          onChange={(e) => setCapital(e.target.value === "" ? NaN : Number(e.target.value))}
          aria-label="初始资金"
        /></label>
        <label className="checkbox-pill wide" title="若本地 DuckDB 尚未缓存该时间段行情，自动按需从 AkShare / yfinance 拉取">
          <input
            type="checkbox"
            checked={autoFetch}
            onChange={(e) => setAutoFetch(e.target.checked)}
          />
          <span>缺失区间自动补齐行情</span>
        </label>
        <button type="submit" className="btn wide" disabled={loading}>
          {loading ? "回测计算中..." : "运行多策略回测对比"}
        </button>
        <button
          type="button"
          className="btn btn-secondary wide"
          aria-expanded={showCreate}
          onClick={() => setShowCreate((v) => !v)}
        >
          {showCreate ? "收起新建策略" : "+ 新建量化策略"}
        </button>
      </form>

      <div className="preset-row">
        <span className="muted">快捷回测</span>
        {datePresets.map((p) => {
          const isActive = startDate === p.start && endDate === p.end;
          return (
            <button
              key={p.label}
              type="button"
              disabled={loading || !Number.isFinite(capital) || capital < 1000}
              aria-pressed={isActive}
              className={`btn-chip${isActive ? " active" : ""}`}
              onClick={() => applyDatePreset(p.start, p.end)}
            >
              {p.label}
            </button>
          );
        })}
      </div>

      {showCreate && (
        <form onSubmit={handleCreateStrategy} className="setup-create flow">
          <h3 className="card-title">新建策略</h3>
          <div className="controls-row">
            <input
              className="input"
              value={newStratId}
              onChange={(e) => setNewStratId(e.target.value)}
              placeholder="策略 ID（英文与下划线）"
              aria-label="策略 ID"
              required
            />
            <input
              className="input"
              value={newStratName}
              onChange={(e) => setNewStratName(e.target.value)}
              placeholder="策略名称"
              aria-label="策略名称"
              required
            />
            <select
              className="select"
              value={newStratStyle}
              aria-label="策略风格"
              onChange={(e) =>
                setNewStratStyle(
                  e.target.value as "quant_daily" | "value" | "sentiment_hybrid"
                )
              }
            >
              <option value="quant_daily">日频量化 (quant_daily)</option>
              <option value="value">价值投资 (value)</option>
              <option value="sentiment_hybrid">情绪混合 (sentiment_hybrid)</option>
            </select>
            <input
              className="input input-grow"
              value={newStratDesc}
              onChange={(e) => setNewStratDesc(e.target.value)}
              placeholder="策略说明"
              aria-label="策略说明"
            />
            <button type="submit" className="btn">
              保存策略
            </button>
          </div>
        </form>
      )}

      </aside>
      <div className="results-column flow" aria-label="回测结果">
      {statusMsg && <div className="ok-banner" role="status">{statusMsg}</div>}
      {errorMsg && <div className="err" role="alert">{errorMsg}</div>}
      {resultIsPrevious && completedRun && (
        <p className="result-notice" role="status">
          显示上次回测：{completedRun.symbols} · {completedRun.start} 至 {completedRun.end} · {formatNumber(completedRun.capital)}
        </p>
      )}

      <div className="card" id="quant-chart">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
          <h2 ref={resultsHeading} tabIndex={-1} className="card-title" style={{ margin: 0 }}>净值与回撤</h2>
          <span className="muted" data-testid="quant-active-range">
            {eqCount > 0 ? `实际区间：${activeStart} ~ ${activeEnd}（${eqCount} 个交易日）` : "暂无交易日数据"}
          </span>
        </div>
        <div className="chart-frame">
          <div ref={chartElRef} data-testid="backtest-chart" className="chart-box" role="img" aria-label="策略净值与回撤图" />
          {!compareRes && <div className="chart-empty">{loading ? "正在计算回测" : "运行回测后查看净值与回撤"}</div>}
        </div>
      </div>

      <div className="flow">
        <div className="card" id="quant-performance">
          <h3 className="card-title">策略绩效</h3>
          <div className="table-wrap" tabIndex={0} role="region" aria-label="策略绩效表">
            <table className="data-table numeric-after-two">
              <thead>
                <tr>
                  <th>策略</th>
                  <th>风格</th>
                  <th>总收益</th>
                  <th>年化CAGR</th>
                  <th>夏普</th>
                  <th>最大回撤</th>
                  <th>胜率</th>
                  <th>95% VaR</th>
                </tr>
              </thead>
              <tbody>
                {Object.values(compareRes?.results || {}).length === 0 ? (
                  <tr>
                    <td colSpan={8} className="muted">
                      暂无对比结果
                    </td>
                  </tr>
                ) : (
                  Object.values(compareRes?.results || {}).map((r) => (
                    <tr key={r.strategy_id}>
                      <td>{r.strategy_name || r.strategy_id}</td>
                      <td>{styleNames[r.style] ?? r.style}</td>
                      <td
                        className={
                          (r.metrics.total_return ?? 0) >= 0 ? "pos-num" : "neg-num"
                        }
                      >
                        {formatPct(r.metrics.total_return)}
                      </td>
                      <td>{formatPct(r.metrics.cagr)}</td>
                      <td>{formatNumber(r.metrics.sharpe_ratio)}</td>
                      <td className="neg-num">{formatPct(r.metrics.max_drawdown)}</td>
                      <td>{formatPct(r.metrics.win_rate)}</td>
                      <td>{formatPct(r.metrics.var_95)}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card" id="quant-risk">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
            <h3 className="card-title" style={{ margin: 0 }}>
              组合风险与相关性
            </h3>
            <span className="muted">
              {completedRun && `统计窗口：${completedRun.start} ~ ${completedRun.end}`}
            </span>
          </div>
          <div className="kpi-grid">
            <div className="kpi-card">
              <div className="kpi-label">组合年化波动率</div>
              <div className="kpi-val">{formatPct(riskRes?.portfolio_volatility)}</div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">95% 日 VaR / CVaR</div>
              <div className="kpi-val" style={{ fontSize: 13 }}>
                {formatPct(riskRes?.var_95)} / {formatPct(riskRes?.cvar_95)}
              </div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">集中度 HHI</div>
              <div className="kpi-val">
                {formatNumber(riskRes?.hhi, 3)} ({riskRes?.concentration_level || "—"})
              </div>
            </div>
          </div>
          <div ref={corrElRef} className="chart-box-sm" style={{ height: 210 }} role="img" aria-label="组合相关性热图" />
        </div>
      </div>
      </div>
    </div>
  );
}
