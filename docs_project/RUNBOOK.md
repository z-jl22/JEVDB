# JEVDB 股票研究与模拟交易原型 — 运维与操作手册 (`RUNBOOK.md`)

本手册面向开发者、研究员与测试人员，提供在 `/home/tao/JEVDB` 环境下启动、配置、演示与验证 **JEVDB 股票研究与模拟交易原型** 的完整可复现步骤。

---

## 1. 目录结构与核心边界

系统严格遵循**“核心不变、外围扩展”**原则：`/home/tao/JEVDB` 原有 C++/Python 核心源码、静态示例与基线测试保持 **100% 未修改**，所有新增功能均位于外围目录中。

```text
/home/tao/JEVDB/
├── src/, python/, examples/, tools/, docs/, scripts/   # [核心只读] JEVDB 原生 C++ 扩展、Python 封装与静态基线
├── CMakeLists.txt, extension_config.cmake, pyproject.toml, LICENSE
├── .pi/                                                # Pi Coding Agent 项目配置与开源 Skills
│   ├── mcp.json                                        # 注册 stock_research stdio MCP 服务
│   └── skills/                                         # 适配后的开源 SKILL.md 工作流
│       ├── stock-research/SKILL.md
│       ├── cross-verify/SKILL.md
│       ├── value-investing/SKILL.md
│       └── quant-strategy/SKILL.md
├── stock_research/                                     # [外围应用] 新增数据、量化、Agent、Web 全部源码
│   ├── pyproject.toml                                  # Python 包清单（锁定 duckdb==1.4.3）
│   ├── package.json                                    # Node 依赖清单（@earendil-works/pi-coding-agent）
│   ├── config.py                                       # 统一环境变量与路径配置
│   ├── cli.py                                          # 统一 CLI 入口（ask / ingest / backtest / paper / serve）
│   ├── storage/                                        # DuckDB Schema、LLM Shim、JEVDB 桥接与多源采集器
│   │   ├── schema.py
│   │   ├── llm_shim.py
│   │   ├── jevdb_bridge.py
│   │   └── ingest/
│   │       ├── akshare_cn.py                           # A 股多源采集（避免 push2）
│   │       ├── yfinance_us.py                          # 美股行情与财务
│   │       ├── sec_edgar.py                            # SEC EDGAR 直连（取代 AGPL 组件）
│   │       ├── finnhub_us.py                           # Finnhub 新闻与财报日历
│   │       └── scheduler.py                            # 可选定时自动化监控任务
│   ├── quant/                                          # 策略注册表、回测、风险分析与模拟交易账本
│   │   ├── strategies.py
│   │   ├── backtest.py
│   │   ├── risk.py
│   │   └── ledger.py
│   ├── mcp_server/                                     # stdio MCP 服务与 SSRF 防护联网工具
│   │   ├── server.py
│   │   └── web_tools.py
│   ├── agent/                                          # Pi Agent 执行器与交叉核验器
│   │   ├── pi_runner.py
│   │   └── verifier.py
│   ├── api/                                            # FastAPI REST + SSE 后端
│   │   └── app.py
│   ├── web/                                            # React + Vite + ECharts / Lightweight-Charts 前端
│   │   ├── package.json
│   │   ├── vite.config.ts
│   │   ├── src/
│   │   └── dist/                                       # 前端构建产物（由 FastAPI 托管于 /）
│   └── tests/                                          # 单元测试与 4-Tier E2E 测试套件
├── docs_project/                                       # 项目交付文档
│   ├── RUNBOOK.md
│   └── DESIGN.md
└── .tmp/stock_research/                                # 运行时 DuckDB 数据库、缓存与策略 JSON（Git 忽略）
```

---

## 2. 运行环境与前置校验

### 2.1 已验证的基准环境版本

| 组件 | 路径 / 命令 | 已验证版本 |
|---|---|---|
| 外围 Python 虚拟环境 | `/home/tao/JEVDB/stock_research/.venv/bin/python` | `Python 3.12.16`（基于 `/home/tao/miniconda3/envs/jevdb/bin/python` `--system-site-packages` 创建） |
| 核心扩展与数据库 | `jevdb` / `duckdb` | `jevdb==0.1.0`、`duckdb==1.4.3`（版本强一致约束） |
| 关键 Python 依赖 | `stock_research/pyproject.toml` | `akshare==1.20.70`, `yfinance==1.8.0`, `mcp==2.3.0`, `fastapi==0.165.0`, `uvicorn==0.54.0`, `ddgs==9.16.1`, `trafilatura==2.4.0`, `beautifulsoup4==4.15.0`, `httpx==0.28.1`, `pydantic==2.14.4`, `pandas==3.1.1`, `numpy==2.5.5`, `pytest==9.2.0`, `playwright==1.65.2` |
| Node.js | `node --version` | `v24.15.0` |
| Pi Coding Agent CLI | `pi --version` | `1.6.1`（`@earendil-works/pi-coding-agent@^1.6.1`） |
| 无头浏览器 | `/usr/bin/chromium` | `Chromium 147.0.7727.101`（Playwright `1.65.2`） |

### 2.2 一键检查当前环境

在 `/home/tao/JEVDB` 目录下执行以下只读校验命令：

```bash
cd /home/tao/JEVDB

# 1. 校验 Python 版本、duckdb 与 jevdb 扩展加载
/home/tao/JEVDB/stock_research/.venv/bin/python -c \
  "import duckdb, jevdb; con = jevdb.connect(':memory:'); print('python_duckdb=', duckdb.__version__, 'jevdb=', jevdb.__version__, 'test_sql=', con.execute('SELECT 1').fetchone())"

# 2. 校验 Node.js、pi CLI 与 Chromium
node --version
pi --version
/usr/bin/chromium --version

# 3. 校验外围 CLI 入口
/home/tao/JEVDB/stock_research/.venv/bin/python -m stock_research.cli --help
```

### 2.3 （可选）从零重建外围环境

若需在新机器上重建外围虚拟环境与前端产物（**不要**覆盖 `duckdb==1.4.3`）：

```bash
cd /home/tao/JEVDB
/home/tao/miniconda3/envs/jevdb/bin/python -m venv --system-site-packages stock_research/.venv
/home/tao/JEVDB/stock_research/.venv/bin/python -m pip install --no-deps -e stock_research
# 或安装全部外围依赖后重新确认 duckdb==1.4.3 与 jevdb==0.1.0 未被篡改

cd /home/tao/JEVDB/stock_research && npm install
cd /home/tao/JEVDB/stock_research/web && npm install && npm run build
```

---

## 3. 环境变量说明

所有环境变量在 `stock_research/config.py`（及部分模块级直读覆写）中解析。**严禁将真实 API Key 提交至版本库或写入文档**，以下示例均使用占位符。

### 3.1 必填与常用凭据变量

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 空（实时 Agent 与 `llm` 模式必填） | DeepSeek API 密钥，用于 `pi` 调用 `deepseek-flash` 及 `ShimServer` 语义查询 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | DeepSeek OpenAI 兼容接口地址 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | Pi Agent 与 `llm_shim` 使用的模型名 |
| `FINNHUB_API_KEY` | 空（可选，Finnhub 抓取必填） | Finnhub 免费 API Key，用于美股新闻与财报日历采集及定时监控 |
| `SEC_USER_AGENT` | `JEVDBStockResearch/0.1 (research@example.org)` | SEC EDGAR 强制要求的自定义请求头，建议设为 `"YourName your@email.com"` |

### 3.2 路径、数据库与 Shim 服务覆写变量

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `JEVDB_REPO_ROOT` | `/home/tao/JEVDB` | 仓库根目录 |
| `STOCK_RESEARCH_RUNTIME_DIR` | `<repo_root>/.tmp/stock_research` | 外围运行时根目录 |
| `STOCK_RESEARCH_CACHE_DIR` | `<runtime_dir>/cache` | SEC CIK 映射、AkShare 板块缓存 JSON 目录（默认位于 `.tmp/stock_research` 下的 `cache` 子目录） |
| `STOCK_RESEARCH_DB_PATH` | `<runtime_dir>/market_research.duckdb` | 行情与基本面 DuckDB 主库路径 |
| `STOCK_RESEARCH_LEDGER_DB_PATH` | `<runtime_dir>/paper_ledger.duckdb` | 模拟交易账本 DuckDB 路径 |
| `STOCK_RESEARCH_STRATEGY_DIR` | `<runtime_dir>/strategies` | 量化策略 JSON 持久化目录 |
| `STOCK_RESEARCH_STATIC_EXAMPLES_DIR` | `<repo_root>/examples/stock_trading` | 只读静态基线 CSV 目录 |
| `JEVDB_MARKET_DB` | 优先于 `STOCK_RESEARCH_DB_PATH` | FastAPI (`stock_research/api/app.py`) 与回测模块直接覆写行情库路径 |
| `JEVDB_PAPER_LEDGER_DB` | 优先于 `STOCK_RESEARCH_LEDGER_DB_PATH` | `PaperAccountService` (`stock_research/quant/ledger.py`) 直接覆写账本库路径 |
| `JEVDB_SHIM_HOST` | `127.0.0.1` | 本地 `llm_shim` HTTP 监听地址 |
| `JEVDB_SHIM_PORT` | `18089` | 本地 `llm_shim` HTTP 监听端口 |
| `JEVDB_LLM_BATCH_K` | `16` | JEVDB 语义批处理大小 |
| `JEVDB_LLM_THREADS` | `4` | JEVDB 并发线程数 |
| `JEVDB_LLM_TIMEOUT` | `120.0` | `llm_shim` 请求超时时间（秒） |

### 3.3 测试与 E2E 隔离变量

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `RUN_LIVE_TESTS` | `0` | 设为 `1` 以启用 `@pytest.mark.live` 外网集成测试 |
| `JEVDB_E2E_MODE` | `auto` | E2E 模式：`auto` / `live` / `contract` |
| `JEVDB_E2E_LIVE_NETWORK` | `0` | 设为 `1`（或传入 `--live-network`）启用联网 E2E 用例 |
| `JEVDB_E2E_SCRATCH_ROOT` | `/home/tao/.gemini/antigravity/scratch/teamwork_tmp/e2e` | E2E 临时隔离根目录（禁止使用 `/tmp`） |

导出凭据示例（在当前 shell 中执行）：

```bash
export DEEPSEEK_API_KEY="<YOUR_DEEPSEEK_API_KEY>"
export DEEPSEEK_BASE_URL="https://api.deepseek.com/v1"
export DEEPSEEK_MODEL="deepseek-flash"
export FINNHUB_API_KEY="<YOUR_FINNHUB_API_KEY>"
export SEC_USER_AGENT="YourName your@email.com"
```

---

## 4. 端到端实操与核心演示流程

以下所有命令默认在 `/home/tao/JEVDB` 根目录执行，使用 `/home/tao/JEVDB/stock_research/.venv/bin/python`（下文中简写为 `VENV_PY` 以确保可复制粘贴执行）：

```bash
cd /home/tao/JEVDB
VENV_PY=/home/tao/JEVDB/stock_research/.venv/bin/python
```

### 4.1 步骤 1：验证 JEVDB 核心静态基线（16 项测试）

确认 `/home/tao/JEVDB` 原生静态功能与语义操作符完好无损：

```bash
# 核心股票交易示例测试（预期 11 tests OK）
$VENV_PY -m unittest discover -s examples/stock_trading -p 'test_*.py' -v

# 核心 JEVDB standin 测试（预期 5 tests OK）
$VENV_PY tools/standin/test_jev_standin.py -v
```

### 4.2 步骤 2：按需动态数据采集与可选定时监控

外围采集器将 A 股（AkShare）与美股（yfinance / SEC EDGAR / Finnhub）数据写入 DuckDB 动态表，并与 `examples/stock_trading/` 静态基线共存：

```bash
# 1. A 股板块行情与成分股（示例：电力行业近 30 天）
$VENV_PY -m stock_research.cli ingest --sector 电力 --market CN --days 30

# 2. A 股个股日线、估值与公告新闻（示例：五粮液 000858）
$VENV_PY -m stock_research.cli ingest --symbol 000858 --market CN --days 120

# 3. 美股行情、基本面 + SEC EDGAR 申报与 XBRL 财务事实（示例：AAPL）
$VENV_PY -m stock_research.cli ingest --symbol AAPL --market US --period 6mo --edgar

# 4. 美股 Finnhub 公司新闻与即将公布的财报日历（需配置 FINNHUB_API_KEY）
$VENV_PY -m stock_research.cli ingest --symbol AAPL --market US --finnhub --days 7
$VENV_PY -m stock_research.cli ingest --market US --finnhub --from-date 2026-11-20 --to-date 2026-12-05
```

**可选：触发或启动后台定时监控 (`SchedulerService`)**

```bash
$VENV_PY -c "
from stock_research.storage.ingest.scheduler import SchedulerService
svc = SchedulerService()
print('trigger_once:', svc.trigger_once())
svc.start_background(interval_seconds=3600.0, run_immediately=False)
print('status:', svc.status())
svc.stop()
"
```

### 4.3 步骤 3：启动 / 验证 JEVDB MCP 服务与本地 LLM Shim

1. **MCP Stdio 服务**（由 `.pi/mcp.json` 自动拉起，也可独立运行验证协议握手）：

```bash
$VENV_PY -m stock_research.mcp_server.server
```

该服务基于 `mcp==2.3.0` (`MCPServer`) 提供全部 **12 个工具**：
- `jevdb_semantic_query`（SQL + SBF 布隆剪枝 `jev_keys_overlap` + `jev_holds`/`jev_probability`/`jev_choose`/`jev_score`）
- `fetch_market_data`、`query_stored_market_data`
- `web_search`（DuckDuckGo `ddgs`）、`web_fetch_url`（SSRF 防护 + Markdown 提取）
- `list_quant_strategies`、`save_quant_strategy`、`run_backtest_tool`、`compare_strategies_tool`、`analyze_portfolio_risk`
- `paper_account_status`、`submit_paper_order`

2. **独立启动本地 LLM Shim**（`stock_research/storage/llm_shim.py`，默认在需要 `llm` 模式语义查询时自动按需拉起，亦可手动启动调试）：

```bash
$VENV_PY -m stock_research.storage.llm_shim --host 127.0.0.1 --port 18089
```

### 4.4 步骤 4：运行命令行多轮投研 Agent（`cli ask`）

确认已设置 `DEEPSEEK_API_KEY`，执行多轮工具调用、交叉核验与带依据输出：

```bash
# 场景 1：近期板块动态研究（“用户需求 → 工具调用 → 核验分析 → 结果展示”）
$VENV_PY -m stock_research.cli ask "我想知道最近3天a股电力行业的情况"

# 场景 2：中长线价值投资分析（A 股 000858 或美股 AAPL）
$VENV_PY -m stock_research.cli ask "请从长期价值投资角度分析 000858（五粮液）的ROE、估值安全边际与历史表现"
$VENV_PY -m stock_research.cli ask "Analyze AAPL long-term value investing fundamentals, SEC filings, and risks"

# 场景 3：日频量化投资与策略对比
$VENV_PY -m stock_research.cli ask "请基于 A 股日频数据对比 cn_daily_momentum_sentiment 与 cn_mean_reversion_daily 的回测收益与风险指标"

# 场景 4：机器可读 JSON 输出（含 tool_calls、verification、sources、answer）
$VENV_PY -m stock_research.cli ask --json "我想知道最近3天a股电力行业的情况"
```

### 4.5 步骤 5：运行量化策略回测与持久化模拟交易（`cli backtest` / `cli paper`）

```bash
# 1. 运行可复现历史回测（自动写入/读取内置策略，输出净值曲线与风险指标 JSON）
$VENV_PY -m stock_research.cli backtest \
  --strategy-id cn_daily_momentum_sentiment \
  --symbols 000858,601318 \
  --start-date 2025-04-24 \
  --end-date 2025-05-15 \
  --initial-capital 100000 \
  --market CN \
  --seed 42

# 2. 查看持久化模拟账户状态
$VENV_PY -m stock_research.cli paper status

# 3. 重置账户并指定模拟起始日（示例初始资金 100,000）
$VENV_PY -m stock_research.cli paper reset --capital 100000 --date 2025-05-15

# 4. A 股模拟买入（100 股整数倍，触发 T+1 锁定与佣金/过户费/滑点计算）
$VENV_PY -m stock_research.cli paper buy --symbol 000858 --quantity 100 --market CN --date 2025-05-15

# 5. 同日卖出将被 T+1 规则拦截（返回非 0 退出码与 REJECTED 审计记录）
$VENV_PY -m stock_research.cli paper sell --symbol 000858 --quantity 100 --market CN --date 2025-05-15

# 6. 推进至下一交易日解锁 T+1 持仓并完成卖出（计算卖出印花税 0.05%）
$VENV_PY -m stock_research.cli paper advance --date 2025-05-16 --prices '{"000858": 130.5}'
$VENV_PY -m stock_research.cli paper sell --symbol 000858 --quantity 100 --market CN --date 2025-05-16
```

### 4.6 步骤 6：构建前端并启动 Web 服务与 4 大核心视图走查

1. **构建前端静态资源（产物位于 `stock_research/web/dist/`）**：

```bash
cd /home/tao/JEVDB/stock_research/web
npm run build
```

2. **启动 FastAPI 后端（同时托管前端静态页）**：

方式 A（通过统一 CLI 启动）：
```bash
cd /home/tao/JEVDB
$VENV_PY -m stock_research.cli serve --host 127.0.0.1 --port 8000
```

方式 B（直接通过 `uvicorn` 启动）：
```bash
cd /home/tao/JEVDB
$VENV_PY -m uvicorn stock_research.api.app:app --host 127.0.0.1 --port 8000
```

方式 C（可选前后端联调热更新开发模式）：
```bash
# 终端 1：启动 FastAPI 后端 (:8000)
cd /home/tao/JEVDB && $VENV_PY -m uvicorn stock_research.api.app:app --host 127.0.0.1 --port 8000
# 终端 2：启动 Vite 开发服务器 (:5173，自动代理 /api -> http://127.0.0.1:8000)
cd /home/tao/JEVDB/stock_research/web && npm run dev
```

3. **健康检查与 4 大视图走查**：

打开浏览器访问 `http://127.0.0.1:8000/`（或先执行 `curl -s http://127.0.0.1:8000/api/health` 确认返回 `{"status": "ok", "jevdb_version": "0.1.0", "duckdb_version": "1.4.3"}`），按顶部导航标签逐项演示：

| 标签页 (`data-tab`) | 页面名称 | 对应 API 路由 | 核心 DOM 锚点 (`data-testid`) 与操作 |
|---|---|---|---|
| `agent` | **1. 投研 Agent 工作台** | `GET /api/agent/stream` (SSE)、`POST /api/agent/query` | 在 `agent-input` 输入问题或点击预设示例，点击 `agent-submit`；依次观测 `tool-calls-panel` 实时工具轨迹、交叉核验徽标及 `agent-answer` 来源引用 |
| `market` | **2. 行情与基本面** | `GET /api/market/bars`、`/api/market/sector`、`/api/market/fundamentals`、`/api/market/news`、`POST /api/market/ingest` | 查询或按需拉取 `000858`/`AAPL` 及板块（如 `电力`），查看 `market-chart` K线/成交量图、估值指标卡片、成分股表及新闻/EDGAR 公告 |
| `quant` | **3. 量化回测与风控** | `GET/POST /api/quant/strategies`、`POST /api/quant/backtest`、`/api/quant/compare`、`/api/quant/portfolio-risk` | 勾选至少 2 个策略（如 `value_quality_margin_of_safety` 与 `cn_daily_momentum_sentiment`）或新建策略，查看 `backtest-chart` 多策略收益/回撤曲线、对比指标表及组合相关性热力图 |
| `paper` | **4. 模拟持仓与盈亏** | `GET /api/paper/account`、`POST /api/paper/order`、`/api/paper/advance-day`、`/api/paper/reset` | 提交买卖模拟订单、推进下一交易日解锁 T+1，查看 `paper-holdings` 持仓与可卖数量表、`paper-pnl-chart` 净值/盈亏曲线及委托/成交流水 |

### 4.7 步骤 7：运行单元测试与 4-Tier E2E 自动化验证

> 注意：请将 `TMPDIR` 与 `--basetemp` 指向可控临时目录（如 `/home/tao/.gemini/antigravity/scratch/teamwork_tmp`），避免占用 `/tmp` 及触发 Chromium Unix Socket 路径超长错误。

```bash
cd /home/tao/JEVDB
SCRATCH_TMP=/home/tao/.gemini/antigravity/scratch/teamwork_tmp
mkdir -p "$SCRATCH_TMP"

# 1. 运行 M1–M4 离线单元与集成测试（77 个用例中剔除 live 标记）
TMPDIR="$SCRATCH_TMP" $VENV_PY -m pytest \
  stock_research/tests/test_m1_storage_ingest.py \
  stock_research/tests/test_m2_quant_ledger.py \
  stock_research/tests/test_m3_mcp_agent_cli.py \
  stock_research/tests/test_m4_web_api.py \
  -m "not live" --basetemp="$SCRATCH_TMP/pytest_unit"

# 2. 运行含外网的集成测试（需导出 DEEPSEEK_API_KEY / FINNHUB_API_KEY / SEC_USER_AGENT）
TMPDIR="$SCRATCH_TMP" RUN_LIVE_TESTS=1 $VENV_PY -m pytest \
  stock_research/tests/test_m1_storage_ingest.py \
  stock_research/tests/test_m3_mcp_agent_cli.py \
  -m "live" --basetemp="$SCRATCH_TMP/pytest_live"

# 3. 运行 4-Tier E2E 套件（离线安全 live 模式：验证真实代码与无头 Chromium UI）
TMPDIR="$SCRATCH_TMP" $VENV_PY -m stock_research.tests.e2e.run_e2e \
  --tier all --mode live --no-live-network

# 4. 运行全量 E2E 套件（含真实外网 AkShare / yfinance / EDGAR / Finnhub / DeepSeek / ddgs）
TMPDIR="$SCRATCH_TMP" $VENV_PY -m stock_research.tests.e2e.run_e2e \
  --tier all --mode live --live-network

# 5. 单独验证 E9 文档交付校验（10 个用例）
TMPDIR="$SCRATCH_TMP" $VENV_PY -m pytest stock_research/tests/e2e \
  -c stock_research/tests/e2e/pytest.ini \
  -k "E9" --e2e-mode=live --basetemp="$SCRATCH_TMP/e9_basetemp" -v
```

---

## 5. 常见问题与故障排查 (Troubleshooting)

1. **DuckDB 单写者锁冲突（`duckdb.IOException: Could not set lock on file`）**
   - **原因**：同时运行多个写入同一 `.duckdb` 文件的进程（例如同时启动 FastAPI 后端与后台采集/CLI 写入默认库）。
   - **解决**：`schema.py` 与 `ledger.py` 已内置短生命周期连接、`fcntl.flock` 文件锁与指数退避重试；若需在服务运行期间执行独立压测或批量离线任务，请通过 `JEVDB_MARKET_DB` 与 `JEVDB_PAPER_LEDGER_DB` 指定隔离的数据库文件路径。

2. **A 股部分东方财富 `push2` 接口超时或限流**
   - **原因**：`push2.eastmoney.com` 在部分代理或高频访问下容易触发连接重置。
   - **解决**：`stock_research/storage/ingest/akshare_cn.py` 已主动绕开 `push2`，K 线采用 `腾讯 -> 新浪` 回退链，基本面采用 `东方财富数据中心/百度估值 -> 同花顺` 回退链，板块成分股采用 4 级本地/DuckDB 缓存及申万指数回退；偶发网络波动时可加 `--force` 重试。

3. **Playwright 启动 Chromium 报错 `SingletonSocket` / `sun_path` 过长**
   - **原因**：Linux `AF_UNIX` socket 路径上限为 108 字节，若 `TMPDIR` 或 `--user-data-dir` 路径过长会导致无头浏览器启动失败。
   - **解决**：确保通过短路径传入 `TMPDIR`（E2E 测试夹具已自动在短目录下创建 `p<hex4>` 临时目录并在 `finally` 中清理）。

4. **SEC EDGAR 返回 HTTP `403 Forbidden`**
   - **原因**：SEC 要求请求头必须包含格式合规的联系信息 `User-Agent`，否则直接拒绝访问。
   - **解决**：设置合规的 `export SEC_USER_AGENT="YourName your@email.com"` 后重新执行 `--edgar` 采集。

5. **Web `POST /api/agent/query` 返回 `503` 或 `cli ask` 报错缺少 Key**
   - **原因**：运行环境未设置 `DEEPSEEK_API_KEY`。
   - **解决**：在启动 `cli ask` 或 `uvicorn` 的同一个终端会话中导出 `DEEPSEEK_API_KEY` 后重启服务。
