import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import * as echarts from "echarts";
import {
  apiJson,
  formatNumber,
  type PaperAccountResponse,
} from "../api";

function nextIsoDay(baseIso: string): string {
  const parts = baseIso.slice(0, 10).split("-").map(Number);
  if (parts.length !== 3 || parts.some((n) => !Number.isFinite(n))) {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    return d.toISOString().slice(0, 10);
  }
  const dt = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2]));
  dt.setUTCDate(dt.getUTCDate() + 1);
  return dt.toISOString().slice(0, 10);
}

export default function PaperView() {
  const [account, setAccount] = useState<PaperAccountResponse | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  const [symbol, setSymbol] = useState("000858");
  const [market, setMarket] = useState<"CN" | "US">("CN");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [orderType, setOrderType] = useState<"market" | "limit">("market");
  const [quantity, setQuantity] = useState(100);
  const [limitPrice, setLimitPrice] = useState("");
  const [advanceDate, setAdvanceDate] = useState("");

  const [feedback, setFeedback] = useState<{ ok: boolean; msg: string } | null>(null);

  const pnlElRef = useRef<HTMLDivElement | null>(null);
  const pnlChartRef = useRef<echarts.ECharts | null>(null);

  const fetchAccount = useCallback(async () => {
    try {
      const data = await apiJson<PaperAccountResponse>("/api/paper/account");
      setAccount(data);
      if (data.current_date) {
        setAdvanceDate(nextIsoDay(data.current_date));
      }
    } catch (err) {
      setFeedback({
        ok: false,
        msg: err instanceof Error ? err.message : "模拟账户加载失败",
      });
    } finally {
      setInitialLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchAccount();
  }, [fetchAccount]);

  useEffect(() => {
    const el = pnlElRef.current;
    if (!el) return;
    const chart = echarts.init(el);
    pnlChartRef.current = chart;
    const ro = new ResizeObserver(() => {
      chart.resize();
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.dispose();
      pnlChartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = pnlChartRef.current;
    if (!chart) return;
    const history = account?.equity_history || [];
    const dates =
      history.length > 0
        ? history.map((h) => h.trade_date || h.date || account?.current_date || "当前")
        : [account?.current_date || "初始"];
    const equities =
      history.length > 0
        ? history.map((h) => Number((h.total_equity ?? 0).toFixed(2)))
        : [Number((account?.total_equity ?? 100000).toFixed(2))];
    const pnls =
      history.length > 0
        ? history.map((h) =>
            Number(((h.realized_pnl ?? 0) + (h.unrealized_pnl ?? 0)).toFixed(2))
          )
        : [Number((account?.total_pnl ?? 0).toFixed(2))];

    chart.setOption(
      {
        backgroundColor: "transparent",
        tooltip: { trigger: "axis" },
        legend: {
          top: 6,
          textStyle: { color: "#cbd5e1", fontSize: 11 },
        },
        grid: { left: 62, right: 54, top: 40, bottom: 26 },
        xAxis: {
          type: "category",
          data: dates,
          axisLabel: { color: "#94a3b8", fontSize: 11 },
        },
        yAxis: [
          {
            type: "value",
            name: "总资产",
            scale: true,
            nameTextStyle: { color: "#94a3b8", fontSize: 11 },
            axisLabel: { color: "#94a3b8", fontSize: 11 },
            splitLine: { lineStyle: { color: "rgba(32, 49, 79, 0.5)" } },
          },
          {
            type: "value",
            name: "累计盈亏",
            nameTextStyle: { color: "#94a3b8", fontSize: 11 },
            axisLabel: { color: "#94a3b8", fontSize: 11 },
            splitLine: { show: false },
          },
        ],
        series: [
          {
            name: "总权益 (Equity)",
            type: "line",
            smooth: true,
            yAxisIndex: 0,
            lineStyle: { width: 2.2, color: "#38bdf8" },
            itemStyle: { color: "#38bdf8" },
            data: equities,
          },
          {
            name: "累计盈亏 (PnL)",
            type: "bar",
            yAxisIndex: 1,
            itemStyle: {
              color: (params: { value: number }) =>
                Number(params.value) >= 0 ? "rgba(239, 68, 68, 0.7)" : "rgba(34, 197, 94, 0.7)",
            },
            data: pnls,
          },
        ],
      },
      true
    );
  }, [account]);

  const handleOrder = async (e: FormEvent) => {
    e.preventDefault();
    const cleanSym = symbol.trim().toUpperCase();
    if (!cleanSym) {
      setFeedback({ ok: false, msg: "请输入交易标的代码。" });
      return;
    }
    if (!Number.isInteger(quantity) || quantity <= 0) {
      setFeedback({ ok: false, msg: "下单数量必须为正整数。" });
      return;
    }
    if (market === "CN" && side === "BUY" && quantity % 100 !== 0) {
      setFeedback({
        ok: false,
        msg: "A股规则校验拦截：买入数量必须为 100 股（1手）的整数倍。",
      });
      return;
    }
    setSubmitting(true);
    setFeedback(null);
    try {
      const payload: Record<string, unknown> = {
        symbol: cleanSym,
        side,
        quantity,
        order_type: orderType,
        market,
      };
      if (account?.current_date) {
        payload.trade_date = account.current_date;
      }
      if (orderType === "limit" && limitPrice.trim()) {
        payload.limit_price = Number(limitPrice);
      }
      const res = await apiJson<{
        accepted?: boolean;
        status?: string;
        reason?: string;
        reject_reason?: string;
        fill_price?: number;
        commission?: number;
        transfer_fee?: number;
        stamp_duty?: number;
      }>("/api/paper/order", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (res.accepted) {
        setFeedback({
          ok: true,
          msg: `成交成功 (${cleanSym} ${side} ${quantity}股 @ ${formatNumber(res.fill_price)}，佣金 ${formatNumber(res.commission)}，过户费 ${formatNumber(res.transfer_fee)}，印花税 ${formatNumber(res.stamp_duty)})`,
        });
      } else {
        setFeedback({
          ok: false,
          msg: `订单被规则拦截 (${res.status || "REJECTED"}): ${res.reason || res.reject_reason || "不符合交易规则"}`,
        });
      }
      await fetchAccount();
    } catch (err) {
      setFeedback({
        ok: false,
        msg: err instanceof Error ? err.message : "模拟下单失败",
      });
    } finally {
      setSubmitting(false);
    }
  };

  const handleAdvanceDay = async () => {
    if (!advanceDate) return;
    setSubmitting(true);
    setFeedback(null);
    try {
      await apiJson("/api/paper/advance-day", {
        method: "POST",
        body: JSON.stringify({ new_date: advanceDate }),
      });
      setFeedback({
        ok: true,
        msg: `交易日已推进至 ${advanceDate}（昨日买入 A 股持仓已解锁可卖数量）。`,
      });
      await fetchAccount();
    } catch (err) {
      setFeedback({
        ok: false,
        msg: err instanceof Error ? err.message : "推进交易日失败",
      });
    } finally {
      setSubmitting(false);
    }
  };

  const handleReset = async () => {
    setSubmitting(true);
    setFeedback(null);
    try {
      await apiJson("/api/paper/reset", {
        method: "POST",
        body: JSON.stringify({ initial_capital: 100000.0 }),
      });
      setFeedback({ ok: true, msg: "模拟账户已重置为初始资金 ¥100,000.00。" });
      await fetchAccount();
    } catch (err) {
      setFeedback({
        ok: false,
        msg: err instanceof Error ? err.message : "账户重置失败",
      });
    } finally {
      setSubmitting(false);
    }
  };

  const positions = account?.positions || [];

  return (
    <div>
      <div className="kpi-grid">
        <div className="kpi-card">
          <div className="kpi-label">当前模拟交易日</div>
          <div className="kpi-val">{account?.current_date || "—"}</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">可用现金 (Cash)</div>
          <div className="kpi-val">¥{formatNumber(account?.cash)}</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">持仓市值 (Market Value)</div>
          <div className="kpi-val">¥{formatNumber(account?.market_value)}</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">账户总资产 (Total Equity)</div>
          <div className="kpi-val">¥{formatNumber(account?.total_equity)}</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">已实现 / 浮动盈亏</div>
          <div
            className={`kpi-val ${(account?.total_pnl ?? 0) >= 0 ? "pos-num" : "neg-num"}`}
            style={{ fontSize: 13 }}
          >
            {formatNumber(account?.realized_pnl)} / {formatNumber(account?.unrealized_pnl)}
          </div>
        </div>
      </div>

      <form onSubmit={handleOrder} noValidate className="controls-row">
        <input
          className="input"
          style={{ width: 110 }}
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          placeholder="代码 000858"
          aria-label="下单代码"
        />
        <select
          className="select"
          value={market}
          onChange={(e) => setMarket(e.target.value === "US" ? "US" : "CN")}
          aria-label="下单市场"
        >
          <option value="CN">A股 (CN T+1)</option>
          <option value="US">美股 (US T+0)</option>
        </select>
        <select
          className="select"
          value={side}
          onChange={(e) => setSide(e.target.value === "SELL" ? "SELL" : "BUY")}
          aria-label="买卖方向"
        >
          <option value="BUY">买入 (BUY)</option>
          <option value="SELL">卖出 (SELL)</option>
        </select>
        <select
          className="select"
          value={orderType}
          onChange={(e) => setOrderType(e.target.value === "limit" ? "limit" : "market")}
          aria-label="订单类型"
        >
          <option value="market">市价单</option>
          <option value="limit">限价单</option>
        </select>
        <input
          className="input"
          style={{ width: 110 }}
          type="number"
          min={1}
          step={1}
          value={quantity}
          onChange={(e) => setQuantity(Number(e.target.value) || 0)}
          aria-label="下单数量"
        />
        {orderType === "limit" && (
          <input
            className="input"
            style={{ width: 100 }}
            type="number"
            step="0.01"
            value={limitPrice}
            onChange={(e) => setLimitPrice(e.target.value)}
            placeholder="限价"
            aria-label="限价价格"
          />
        )}
        <button type="submit" className="btn" disabled={submitting}>
          提交模拟订单
        </button>

        <input
          className="input"
          type="date"
          value={advanceDate}
          onChange={(e) => setAdvanceDate(e.target.value)}
          aria-label="推进交易日"
        />
        <button
          type="button"
          className="btn btn-secondary"
          disabled={submitting}
          onClick={() => void handleAdvanceDay()}
        >
          推进下一交易日 (解锁T+1)
        </button>
        <button
          type="button"
          className="btn btn-danger"
          disabled={submitting}
          onClick={() => void handleReset()}
        >
          重置账户
        </button>
      </form>

      <div className="info-banner">
        规则与假设：A 股实行 T+1（当日买入当日不可卖）、买入 100 股整数倍、±10% 涨跌幅限制；默认 5bps 滑点，佣金 0.03%（最低 ¥5），过户费 0.001%，卖出印花税 0.05%。
      </div>

      {feedback && (
        <div className={feedback.ok ? "ok-banner" : "err"}>{feedback.msg}</div>
      )}

      <div className="grid-2">
        <div className="card">
          <h3 className="card-title">模拟持仓明细 (Positions)</h3>
          <div data-testid="paper-holdings" className="table-wrap" style={{ minHeight: 160 }}>
            {initialLoading ? (
              <div style={{ padding: 14 }}>加载持仓中...</div>
            ) : positions.length === 0 ? (
              <div className="muted" style={{ padding: 14 }}>
                当前暂无持仓（可使用上方交易面板提交模拟订单）
              </div>
            ) : (
              <table className="data-table">
                <thead>
                  <tr>
                    <th>代码</th>
                    <th>市场</th>
                    <th>持仓</th>
                    <th>可卖(T+1)</th>
                    <th>成本价</th>
                    <th>现价</th>
                    <th>市值</th>
                    <th>浮动盈亏</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((pos) => (
                    <tr key={`${pos.market}-${pos.symbol}`}>
                      <td>{pos.symbol}</td>
                      <td>{pos.market}</td>
                      <td>{pos.quantity}</td>
                      <td>{pos.sellable_qty}</td>
                      <td>{formatNumber(pos.avg_cost)}</td>
                      <td>{formatNumber(pos.mark_price ?? pos.market_price)}</td>
                      <td>{formatNumber(pos.market_value)}</td>
                      <td className={pos.unrealized_pnl >= 0 ? "pos-num" : "neg-num"}>
                        {formatNumber(pos.unrealized_pnl)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        <div className="card">
          <h3 className="card-title">账户权益与累计盈亏曲线 (Equity & PnL)</h3>
          <div ref={pnlElRef} data-testid="paper-pnl-chart" className="chart-box-sm" />
        </div>
      </div>

      <div className="grid-2" style={{ marginTop: 14 }}>
        <div className="card">
          <h3 className="card-title">委托记录 (Orders · {(account?.orders || []).length})</h3>
          <div className="table-wrap" style={{ maxHeight: 200 }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>日期</th>
                  <th>代码</th>
                  <th>方向</th>
                  <th>数量</th>
                  <th>状态</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {(account?.orders || []).length === 0 ? (
                  <tr>
                    <td colSpan={6} className="muted">
                      暂无委托记录
                    </td>
                  </tr>
                ) : (
                  (account?.orders || []).slice(0, 15).map((ord, idx) => (
                    <tr key={ord.order_id || idx}>
                      <td>{ord.trade_date || "—"}</td>
                      <td>{ord.symbol}</td>
                      <td>{ord.side}</td>
                      <td>{ord.requested_qty ?? ord.quantity}</td>
                      <td>{ord.status}</td>
                      <td>
                        {ord.reject_reason ||
                          ord.reason ||
                          (ord.status === "REJECTED" ? ord.reject_code || "已拒绝" : "已成交")}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card">
          <h3 className="card-title">成交明细 (Fills · {(account?.fills || []).length})</h3>
          <div className="table-wrap" style={{ maxHeight: 200 }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>日期</th>
                  <th>代码</th>
                  <th>方向</th>
                  <th>数量</th>
                  <th>成交价</th>
                  <th>总费用</th>
                </tr>
              </thead>
              <tbody>
                {(account?.fills || []).length === 0 ? (
                  <tr>
                    <td colSpan={6} className="muted">
                      暂无成交记录
                    </td>
                  </tr>
                ) : (
                  (account?.fills || []).slice(0, 15).map((f, idx) => {
                    const fees =
                      (f.commission ?? 0) + (f.transfer_fee ?? 0) + (f.stamp_duty ?? 0);
                    return (
                      <tr key={f.fill_id || idx}>
                        <td>{f.trade_date || "—"}</td>
                        <td>{f.symbol}</td>
                        <td>{f.side}</td>
                        <td>{f.quantity}</td>
                        <td>{formatNumber(f.fill_price)}</td>
                        <td>{formatNumber(fees)}</td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
