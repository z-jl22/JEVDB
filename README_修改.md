# README 修改说明（2.0）

本文件记录对分支 `feat/stock-research-platform`（2.0 快照）新增的**三项评估改进**。三项都不需要额外数据，只使用现有的 13 个交易日、2 只股票。

原 `README.md` 未修改，所有说明只写在本文件。

---

## 一、双基准：满仓 + 同仓位

### 原来的问题

基准是一条**价格指数**：以区间首日收盘价为锚，`初始资金 × 平均(当日收盘/锚点收盘)`。它不计交易费用、不做整手、始终满仓，也没有“同仓位”版本，因此：

- 无法区分“策略跑输”是判断不准，还是仓位更低；
- 基准不扣费，会系统性地高估基准收益。

### 现在的做法

用与策略完全相同的撮合规则模拟两条被动基准：

| 基准 | 每票预算 | 说明 |
|---|---|---|
| 满仓基准 | 初始资金 ÷ 股票数 | 代表“这几天行情本身” |
| 同仓位基准 | `min(1/N, 策略自身 max_weight)` | 代表“同样的仓位、不做判断” |

- 建仓时点：第一个**可执行**开盘（t+1），与策略执行时序一致；建仓前按现金计；
- 成本：滑点、整手、佣金（含最低 5 元）、过户费、涨跌停、成交量参与率全部套用；
- 字段：`equity_curve[].benchmark_aligned_equity`、`BacktestResult.benchmark_aligned_metrics`、`ComparisonResult.benchmark_aligned_curve`；
- 前端：净值为三条线（策略实线、满仓灰虚线、同仓位绿点线）。

### 实测（2025-04-24 ~ 05-15，10 万本金）

| 对象 | 收益 |
|---|---:|
| 价值策略 | +0.39% |
| 满仓基准 | +3.98% |
| 同仓位基准（价值策略 max_weight=25%） | +1.62% |
| 同仓位基准（均值回归 max_weight=20%） | +1.32% |

同仓位基准随策略自己的 `max_weight` 变化，这是有意的：它代表“在相同仓位约束下，不做判断会怎样”。

### 代码位置

- `stock_research/quant/backtest.py`：新增 `_simulate_passive_benchmark()`；`run_backtest()` 改为调用它生成两条曲线；`_align_comparison_curves()` 增加同仓位曲线；
- `BacktestResult` 增加 `benchmark_aligned_metrics`，`ComparisonResult` 增加 `benchmark_aligned_curve`；
- `assumptions` 写明两条基准的定义与成本口径。

---

## 二、成本归因与费率敏感性

### 新增指标

每个策略的 `metrics`（以及对比排行榜 `ranking`）新增：

| 指标 | 含义 |
|---|---|
| `gross_return` | 不计费用时的收益（期末资产 + 累计费用 还原） |
| `fee_drag` | 累计费用 ÷ 初始资金 |
| `total_fees` | 累计费用（原有） |
| `turnover` | 换手率（原有） |

### 新增接口

```
POST /api/quant/fee-sensitivity
{
  "strategy_id": "cn_daily_momentum_sentiment",
  "symbols": ["000858", "601318"],
  "start_date": "2025-04-24",
  "end_date": "2025-05-15"
}
```

默认跑四组情景（费率 + 最低佣金），也可通过 `scenarios: [[rate, min_commission], ...]` 自定义。

### 实测（动量策略，10 万本金）

| 费率 | 最低佣金 | 净收益 | 毛收益 | 累计费用 | 费用占比 | 成交笔数 |
|---|---:|---:|---:|---:|---:|---:|
| 0.030% | 5.00 元 | 0.26% | 0.34% | 80.03 | 0.08% | 10 |
| 0.020% | 2.00 元 | 0.28% | 0.34% | 58.77 | 0.06% | 10 |
| 0.010% | 0.00 元 | 0.30% | 0.34% | 44.40 | 0.04% | 10 |
| 0.000% | 0.00 元 | 0.31% | 0.34% | 30.03 | 0.03% | 10 |

### 一个值得写进报告的发现

**只降费率几乎没用。** 每笔成交额约 1.3 万元，0.03% 只有 3.9 元，永远按“最低 5 元”收。第一次只把费率从 0.03% 改成 0.01%，三组结果完全一样。

所以成本敏感性的关键变量是**最低佣金**，不是费率。13 天 6～10 笔交易，费用占初始资金 0.05%～0.08%，对短线策略不是小数。

### 前端

「成本归因与费率敏感性」卡片：上半部分是 KPI（毛收益 / 净收益 / 累计费用 / 费用占比 / 换手率），下半部分是四行情景表。

---

## 三、执行时点敏感性

### 做法

`run_backtest()` 新增 `execution` 参数：

| 取值 | 含义 |
|---|---|
| `next_open` | t 日收盘信号 → t+1 开盘成交（默认，与原行为一致） |
| `next_close` | t 日收盘信号 → t+1 收盘成交 |
| `t2_open` | t 日收盘信号 → t+2 开盘成交 |

实现上把“前一天信号次日执行”改成**可配置延迟的信号队列**，`delay=1` 时行为与原来完全一致；成交价按 `next_close` 取收盘价，涨跌停与 `[low, high]` 校验改用参考价。执行假设同时写入 `assumptions` 和 `reproducibility_hash`。

### 新增接口

```
POST /api/quant/execution-sensitivity
{
  "strategy_id": "cn_daily_momentum_sentiment",
  "symbols": ["000858", "601318"],
  "start_date": "2025-04-24",
  "end_date": "2025-05-15"
}
```

### 实测（动量策略）

| 执行假设 | 净收益 | 最大回撤 | 夏普 | 成交笔数 | 累计费用 |
|---|---:|---:|---:|---:|---:|
| t+1 开盘 | 0.26% | 0.31% | 1.10 | 10 | 80.03 |
| t+1 收盘 | 0.91% | 0.27% | 3.86 | 10 | 80.47 |
| t+2 开盘 | 1.16% | 0.23% | 4.60 | 8 | 70.29 |

**同一策略、同一份数据，只改成交时点，收益差 0.9 个百分点。** 这说明在这份样本上，结论对执行假设的敏感度高于策略本身的差异，报告里必须写明假设。

### 前端

「执行时点敏感性」表，回测后自动填充三行。

---

## 四、如何验证

启动服务：

```bash
cd /home/hw/JEVDB-platform2
JEVDB_REPO_ROOT=$PWD stock_research/.venv/bin/python -m stock_research.cli serve --port 8000
```

页面：`http://localhost:8000` → **量化回测与风控**，回测后可以看到：

1. 净值图三条线（策略 / 满仓基准 / 同仓位基准）；
2. 「成本归因与费率敏感性」卡片与四行表；
3. 「执行时点敏感性」三行表。

接口自测：

```bash
curl -X POST http://127.0.0.1:8000/api/quant/fee-sensitivity \
  -H 'Content-Type: application/json' \
  -d '{"strategy_id":"cn_daily_momentum_sentiment","symbols":["000858","601318"],"start_date":"2025-04-24","end_date":"2025-05-15"}'

curl -X POST http://127.0.0.1:8000/api/quant/execution-sensitivity \
  -H 'Content-Type: application/json' \
  -d '{"strategy_id":"cn_daily_momentum_sentiment","symbols":["000858","601318"],"start_date":"2025-04-24","end_date":"2025-05-15"}'
```

测试：

```bash
stock_research/.venv/bin/python -m pytest stock_research/tests/test_m2_quant_ledger.py -q
```

当前结果：**23 passed**。

---

## 五、已知限制与未做项

1. **第 4 项（非线性冲击成本、部分成交）未做**：需要改撮合逻辑、会改变现有成交结果并牵动测试；
2. 同仓位基准随策略 `max_weight` 变化，不同策略之间不能直接横向比较基准数值；
3. 执行时点敏感性只改成交时点，不改滑点、流动性与盘口假设；
4. 样本只有 2 只股票、13 个交易日，所有收益数字都不具统计意义；
5. 费率情景固定四组，未做券商实际费率（规费、经手费）的细分；
6. 回测仍以日线开盘/收盘价成交，无法还原盘口与部分成交。

---

## 附：改动文件清单

| 文件 | 改动 |
|---|---|
| `stock_research/quant/backtest.py` | 双基准模拟、执行队列、`fee_sensitivity`、`execution_sensitivity`、新增字段 |
| `stock_research/quant/risk.py` | `gross_return`、`fee_drag` |
| `stock_research/api/app.py` | 两个新接口与请求模型、导入 |
| `stock_research/web/src/api.ts` | 新类型与指标字段 |
| `stock_research/web/src/components/QuantView.tsx` | 成本归因卡片、执行时点表、双基准曲线 |
| `stock_research/web/src/styles.css` | 长表格滚动与表头吸顶 |
| `stock_research/tests/test_m2_quant_ledger.py` | `_align_comparison_curves` 解包 3 → 4 个返回值 |
