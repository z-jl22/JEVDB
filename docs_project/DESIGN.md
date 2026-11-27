# JEVDB 股票研究与模拟交易原型 — 项目设计与组件审查文档 (`DESIGN.md`)

本文档系统阐述基于语义数据库 **JEVDB**（Purdue University PDAIS & University of Utah，《*Prune First, Decide Fast: Scalable Semantic Query Processing with JEVDB*》，arXiv:2610.02046）构建的股票研究与模拟交易原型的架构设计、目标用户与场景流程、集成组件与开源 Skills 全面审查、模拟交易规则与简化假设，以及关键技术决策与交叉核验机制。

---

## 1. 目标用户与典型场景功能设计

### 1.1 目标用户画像

| 用户群体 | 核心诉求 | 系统能力映射 |
|---|---|---|
| **个人投资者 (`个人投资者`)** | 快速了解近期市场/行业热点、查询个股估值与财报健康度、获得有数据支撑的投资思路、在无真金白银风险的环境下练习 A 股/美股交易规则 | Web 4 大可视化工作台、自然语言投研问答（`stock-research` + `cross-verify`）、内置价值与日频量化策略一键回测、贴合 A 股 `T+1` / `100` 股与费率规则的持久化模拟账本 |
| **机构研究员 (`机构研究员`)** | 高效整合结构化行情/财报与非结构化新闻/公告进行语义筛选、跨数据源核验矛盾口径、构建自定义因子策略并开展多策略对比与组合风险归因（VaR/CVaR/相关性/HHI） | JEVDB 混合查询（SQL + `jev_keys_overlap` 布隆剪枝 + `jev_holds`/`jev_score`/`jev_probability`）、SEC EDGAR XBRL 原生解析、可复现确定性回测引擎（`reproducibility_hash`）、声明式策略 AST 注册表、组合风险定量分析及完整工具调用核验轨迹 |

### 1.2 端到端系统架构与核心交互链路

系统摒弃“纯聊天问答”模式，建立**可观测、可核验、可回溯**的闭环工作流：

> **用户需求**（CLI `ask` / Web 输入框 / REST 请求） → **工具调用**（Pi Coding Agent 调度 `.pi/skills/` 与 12 个 JEVDB MCP 工具，或前端直连 FastAPI/Quant/Ledger 服务） → **核验分析**（`cross-verify` 多源对账 + `verifier.py` 数值/URL/公式落地检查与修复） → **结果展示**（CLI 结构化 4 段报告 / SSE 流式工具轨迹 + Web 4 大交互图表视图）

### 1.3 典型场景与输入/输出契约

#### 场景 A：A 股/美股近期市场与行业动态研究
- **典型输入**：`prompt = "我想知道最近3天a股电力行业的情况"`（支持 CLI `ask` 与 Web 视图 1 `POST /api/agent/query` / `GET /api/agent/stream`）。
- **端到端流程**：
  1. Agent 加载 `.pi/skills/stock-research/SKILL.md` 与 `cross-verify/SKILL.md`。
  2. 先调用 `query_stored_market_data(sector="电力", table="auto")` 检查本地 DuckDB，再调用 `fetch_market_data(source="akshare_sector", sector="电力", days=5)` 拉取同花顺/新浪板块快照、成分股涨跌幅及关联新闻。
  3. 调用 `jevdb_semantic_query` 对静态基线表与动态 `news_events` 执行 SQL 过滤 + `jev_keys_overlap` 剪枝 + 语义谓词筛选。
  4. 调用 `web_search`（及必要时 `web_fetch_url`）补充近 3 日政策与电价/装机催化。
  5. 经 `verifier.py` 对数值、时间窗口与引用 URL 进行核验后输出结构化结论。
- **输出结构 (`AgentQueryResponse`)**：
  ```json
  {
    "prompt": "我想知道最近3天a股电力行业的情况",
    "tool_calls": [{"name": "fetch_market_data", "args": {}, "result_summary": "...", "duration_ms": 420}],
    "verification": {"grounded": true, "passed": true, "ungrounded_numbers": [], "unverified_urls": [], "warnings": []},
    "answer": "含精确涨跌幅、龙头股表现、核验说明与数据来源的完整研究报告",
    "sources": ["akshare_ths", "akshare_sina", "akshare_cls", "https://..."],
    "duration_ms": 8500
  }
  ```

#### 场景 B：中长线价值投资分析（`价值投资`）
- **典型输入**：标的 `000858`（CN）或 `AAPL`（US），`mode = "value"` 或查询如 `"从价值投资角度分析 000858 / AAPL"`。
- **端到端流程**：
  1. 触发 `.pi/skills/value-investing/SKILL.md`，采集多源财务与估值：`akshare_stock`（PE(TTM)/PB/ROE/资产负债率）或 `yfinance` + `sec_edgar`（`10-K`/`10-Q` XBRL 营业收入、净利润、负债率）。
  2. 调用 `jevdb_semantic_query` 检索基本面与公告风险，交叉比对不同数据源的报告期与单位（元 vs 亿元 vs 百万美元）。
  3. 调用 `run_backtest_tool(strategy_id="value_quality_margin_of_safety", ...)` 与 `analyze_portfolio_risk(...)` 输出长期价值因子历史回测收益、最大回撤与集中度指标。
- **输出要点**：核心价值论点、带来源标注的财务估值对比表、价值策略历史表现与风险提示。

#### 场景 C：日频量化投资、多策略对比与组合风险分析（`量化投资`）
- **典型输入**：
  - 策略定义 `StrategySpec`：`strategy_id`、`name`、`style`（`"quant_daily"` / `"sentiment_hybrid"` / `"value"`）、`market`（`"CN"` / `"US"`）、参数 `params` 与受限声明式 AST `rules`（`buy` / `sell` / `ranking`）。
  - 回测与对比参数：`strategy_ids`（如 `["cn_daily_momentum_sentiment", "cn_mean_reversion_daily", "value_quality_margin_of_safety"]`）、`symbols`、`start_date`、`end_date`、`initial_capital`、`seed=42`。
- **端到端流程**：
  1. Agent 或 Web 视图 3 通过 `save_quant_strategy` / `POST /api/quant/strategies` 保存或读取策略。
  2. `compare_strategies_tool` / `POST /api/quant/compare` 调用确定性历史回测引擎（`stock_research/quant/backtest.py`），逐日因果计算指标、基本面与新闻情绪，输出等权基准曲线、策略净值曲线、回撤序列、`reproducibility_hash` 及风险指标（`total_return`, `cagr`, `annualized_volatility`, `sharpe_ratio`, `sortino_ratio`, `calmar_ratio`, `max_drawdown`, `max_drawdown_duration_days`, `var_95`, `cvar_95`, `win_rate`, `profit_factor`, `turnover`）。
  3. `analyze_portfolio_risk` / `POST /api/quant/portfolio-risk` 计算组合年化波动率、95% 历史 VaR/CVaR、两两相关性矩阵 (`correlation_matrix`)、赫芬达尔指数 (`hhi`)、有效持仓数 (`effective_n`) 与板块暴露 (`sector_exposure`)。

#### 场景 D：持久化模拟交易与盈亏跟踪
- **典型输入**：`POST /api/paper/order` 或 `cli paper buy/sell`（`symbol`, `side`, `quantity`, `order_type`, `limit_price`, `market`, `trade_date`, `strategy_id`）及 `advance_day(new_date, mark_prices)`。
- **端到端流程**：校验市场规则（A 股 `T+1`、100 股买入手数、±10% 涨跌幅、资金/持仓充足性），合法订单按含滑点价格成交并扣减费用、记录 FIFO 持仓批次；非法订单返回 `accepted: false` 与明确 `reject_code` 并留痕审计；跨日调用 `advance_day` 解锁前一日买入批次的 `sellable_qty` 并生成每日净值快照。

#### 场景 E：Web 可视化与 FastAPI REST / SSE 接口交互
FastAPI 后端（`stock_research/api/app.py`）托管构建后的 React SPA（`stock_research/web/dist/`）并提供以下全量接口与 4 个视图：

| 视图 / 模块 | API 路由与传输协议 | 核心输出与页面锚点 (`data-testid`) |
|---|---|---|
| 健康检查 | `GET /api/health` | `{"status": "ok", "jevdb_version": "0.1.0", "duckdb_version": "1.4.3"}` |
| **1. 投研 Agent 工作台** (`data-tab="agent"`) | `GET /api/agent/stream`（SSE 事件：`status` → `tool_start` → `tool_end` → `verification` → `final_answer` / `error`），失败自动降级 `POST /api/agent/query` | `agent-input`、`agent-submit`、`tool-calls-panel`、`agent-answer`、核验结果横幅与来源标签 |
| **2. 行情与基本面** (`data-tab="market"`) | `GET /api/market/bars`、`/api/market/sector`、`/api/market/fundamentals`、`/api/market/news`、`POST /api/market/ingest` | `market-chart`（Lightweight-Charts K 线 + 成交量）、PE/PB/ROE/负债率卡片、去重板块成分股、新闻与 EDGAR 公告 |
| **3. 量化回测与风控** (`data-tab="quant"`) | `GET/POST /api/quant/strategies`、`POST /api/quant/backtest`、`/api/quant/compare`、`/api/quant/portfolio-risk` | `backtest-chart`（ECharts 多策略净值/基准/回撤曲线）、策略对比表、组合风险 KPI 与相关性矩阵热力图 |
| **4. 模拟持仓与盈亏** (`data-tab="paper"`) | `GET /api/paper/account`、`POST /api/paper/order`、`/api/paper/advance-day`、`/api/paper/reset` | `paper-holdings` 持仓表（含 `sellable_qty`）、`paper-pnl-chart`（ECharts 资产与累计盈亏曲线）、委托/成交明细 |

---

## 2. 集成组件与开源 Skills 审查

以下对系统集成的所有核心组件、第三方库、外部数据源及开源 Skills 逐一审查其 **职责、输入/输出、依赖、权限与安全边界、来源/版本/Commit、开源许可 (License) 与潜在风险及缓解措施**。

### 2.1 运行时组件、依赖库与数据源审查矩阵

| 组件 | 职责 (Role) | 输入 / 输出 (Inputs / Outputs) | 依赖 (Dependencies) | 权限与安全边界 (Permissions & Security) | 来源与版本 (Source / Version) | 开源许可 (License) | 潜在风险与缓解措施 (Risks & Mitigations) |
|---|---|---|---|---|---|---|---|
| **JEVDB Core (`jevdb`)** | DuckDB C++ 可加载语义查询扩展，提供 `jev_holds`, `jev_probability`, `jev_choose`, `jev_score`, `jev_keys_overlap` (SBF 剪枝) 与 `jevdb_stats` | **In**: DuckDB SQL、`STRUCT`/文本字段、Endpoint Secret<br>**Out**: 语义布尔/概率/分类/评分结果与剪枝统计 | `duckdb==1.4.3`, C++17, OpenSSL (`CMakeLists.txt`) | `jevdb.connect()` 内部开启 `allow_unsigned_extensions=True`；读取 `JEV_API_KEY`/`OPENAI_API_KEY`；出站 HTTPS | `https://github.com/PDAIS/JEVDB` (`https://jevdb.org`, arXiv:2610.02046; fork `z-jl22/JEVDB`) `0.1.0` | **MIT** (`/home/tao/JEVDB/LICENSE`) | **风险**：原生 C++ 仅支持 OpenAI `json_schema (strict: true)` 与 DeepSeek 不兼容；默认核心不得修改。<br>**缓解**：保持核心 100% 只读不变，通过外围 `ShimServer` 完成协议桥接。 |
| **DuckDB (`duckdb`)** | 嵌入式列式存储引擎，持久化静态基线、动态行情/财务/新闻、语义缓存与模拟交易账本 | **In**: 参数化 SQL / DDL / 事务<br>**Out**: 查询行集、`.duckdb` 文件 | Python 3.12, `jevdb==0.1.0` | 本地读写 `.tmp/stock_research/*.duckdb` | `https://github.com/duckdb/duckdb-python` `1.4.3` | **MIT** | **风险**：单写者文件锁在多进程/多线程并发写入时易抛 `IOException`。<br>**缓解**：使用短生命周期连接、进程内 `RLock`、跨进程 `fcntl.flock` 与指数退避重试。 |
| **LLM Shim (`stock_research/storage/llm_shim.py`, `ShimServer`)** | 本地 OpenAI 兼容 HTTP 适配器，将 JEVDB `json_schema (strict: true)` 翻译为 DeepSeek `json_object` | **In**: `POST /v1/chat/completions`, `GET /health`<br>**Out**: 规范化 `chat.completion` JSON (`{"answers": [...]}`) | `httpx==0.28.1`, Python stdlib `http.server` | 仅绑定 `127.0.0.1:18089`（冲突时回退临时端口）；`trust_env=False`；向 `api.deepseek.com` 转发 `DEEPSEEK_API_KEY` | 本项目外围原创 (`stock_research/storage/llm_shim.py`) | 本项目许可（MIT 兼容） | **风险**：模型返回 Markdown 代码围栏、`reasoning_content` 泄漏或长度截断导致 JEVDB 解析失败。<br>**缓解**：注入严格 JSON 系统指令、剥离 `reasoning_content` 与代码围栏、按 Schema 校验/归一化并支持 `max_tokens` 倍增重试。 |
| **AkShare 适配器 (`akshare`, `stock_research/storage/ingest/akshare_cn.py`)** | 零注册采集中国 A 股日线、板块快照/成分股、财务估值与新闻 | **In**: 板块名（如 `电力`）或 6 位证券代码 + `days`<br>**Out**: 写入 `assets`, `market_bars`, `sector_*`, `fundamentals`, `news_events` | `akshare==1.20.70`, `pandas==3.1.1` | 出站 HTTP(S) 访问腾讯、新浪、同花顺、百度、东财数据中心、财联社；读写 `STOCK_RESEARCH_CACHE_DIR`（默认 `<runtime_dir>/cache`） | `https://github.com/akfamily/akshare` `1.20.70` | **MIT** | **风险**：东财 `push2` 接口频繁限流/断连；上游网页结构偶发变更。<br>**缓解**：彻底避开 `push2`，实现 `腾讯 -> 新浪` K 线回退、`东财数据中心/百度 -> 同花顺` 基本面回退、4 级板块成分股缓存及申万指数回退。 |
| **yfinance 适配器 (`yfinance`, `stock_research/storage/ingest/yfinance_us.py`)** | 零注册采集美股 OHLCV 行情、估值指标、财务报表与新闻 | **In**: 美股 Ticker（如 `AAPL`）与 `period`<br>**Out**: `assets`, `market_bars`, `fundamentals`, `news_events` | `yfinance==1.8.0`, `pandas==3.1.1`, `numpy==2.5.5` | 出站 HTTPS 访问 Yahoo Finance | `https://github.com/ranaroussi/yfinance` `1.8.0` | **Apache-2.0** | **风险**：非官方接口限流及 `yfinance 1.x` 嵌套 `content` 新闻结构/缺失负债字段变动。<br>**缓解**：兼容嵌套 `content` 解析、使用 `Liabilities/Assets` 与 `debtToEquity` 回退并记录非致命局部警告。 |
| **SEC EDGAR 直连客户端 (`stock_research/storage/ingest/sec_edgar.py`)** | 零注册拉取 SEC CIK 映射、`10-K`/`10-Q`/`8-K` 申报目录及 XBRL `companyfacts`（**替代被排除的 AGPL-3.0 组件**） | **In**: 美股 Ticker<br>**Out**: `edgar_filings`, `fundamentals` (`source='sec_edgar'`) | `httpx==0.28.1` | 仅访问 `www.sec.gov` 与 `data.sec.gov`；发送合规 `SEC_USER_AGENT`（`trust_env=False`）；24h 本地 CIK 缓存 | 本项目外围直连 SEC REST (`https://data.sec.gov/`) | 美国政府公共领域数据 + `httpx` (**BSD-3-Clause**) | **风险**：SEC 每秒 10 次速率上限及缺失 `User-Agent` 触发 403。<br>**缓解**：`_SecRateLimiter(0.12s)` 限速器、默认合法 User-Agent 回退、429/503 退避重试，以及优先选取 10-K 完整财年 (`>=300` 天) 最新 `filed` XBRL 标签。 |
| **Finnhub 客户端 (`stock_research/storage/ingest/finnhub_us.py`) & `scheduler.py`** | 可选美股新闻、市场快讯、财报日历采集及后台定时监控任务调度 | **In**: `FINNHUB_API_KEY`, symbol/category/日期区间<br>**Out**: `news_events`, `earnings_calendar`, `ingestion_runs` | `httpx==0.28.1` | 出站 HTTPS 至 `finnhub.io/api/v1`（`X-Finnhub-Token` 头）；未配置 Key 时自动跳过 | `https://finnhub.io/` API 封装 | 专有免费层 API + `httpx` (**BSD-3-Clause**) | **风险**：免费层 60 req/min 限流及 Key 泄漏风险。<br>**缓解**：`SlidingWindowRateLimiter(55, 60s)` 预留余量、`X-Ratelimit-Reset` 退避，凭据仅从环境变量读取且绝不写入日志/文档。 |
| **DuckDuckGo 搜索 (`ddgs`, `stock_research/mcp_server/web_tools.py`)** | 零注册开放互联网文本与新闻检索 | **In**: `query`, `max_results` (1..25), `search_type`<br>**Out**: `{title, url, snippet, published_at?, source?}` | `ddgs==9.16.1` | 出站 HTTPS 检索公开搜索引擎；过滤 `r.jina.ai` 与非 HTTP(S) 链接 | `https://github.com/deedy5/ddgs` `9.16.1` | **MIT** | **风险**：单一引擎超时、空结果或 Agent 过度外推摘要数字。<br>**缓解**：`timeout=8` 与多引擎回退（`yahoo,duckduckgo,brave,mojeek -> yahoo -> cn-zh -> news`），配合 Prompt 与 `verifier.py` 禁止无根据外推。 |
| **Trafilatura + BeautifulSoup4 + httpx (`web_tools.py`)** | 带严格 SSRF 与 DNS 重绑定防护的网页抓取及正文 Markdown 提取 | **In**: 公网 `http(s)` URL、`max_chars` (默认 12000)<br>**Out**: 清洗后 Markdown 文本与链接 | `trafilatura==2.4.0`, `beautifulsoup4==4.15.0`, `httpx==0.28.1` | 严禁内网/回环/链路本地/保留 IP、`.local`/`.internal` 域名、URL 嵌入凭据；响应上限 2 MiB；最大重定向 5 跳 | `adbar/trafilatura` (`2.4.0`), `bs4` (`4.15.0`), `encode/httpx` (`0.28.1`) | **Apache-2.0** (`trafilatura`) + **MIT** (`bs4`) + **BSD-3-Clause** (`httpx`) | **风险**：SSRF 内网探测、DNS-rebinding TOCTOU、脚本注入与网页 Prompt 注入。<br>**缓解**：`_PinnedNetworkBackend` 锁定已校验 IP 建立 TCP 连接、逐跳校验重定向、`decompose()` 移除 `<script>`/`<iframe>`/`<svg>`，并将网页文本视为不可信数据。 |
| **MCP Python SDK (`mcp`, `stock_research/mcp_server/server.py`)** | 标准 `stdio` MCP 服务端，统一暴露 12 个 JEVDB、采集、联网、量化与模拟交易工具 | **In**: JSON-RPC stdio 请求<br>**Out**: 结构化 JSON 载荷（回测载荷压缩至 `<=16KB`） | `mcp==2.3.0`, `pydantic==2.14.4` | 本地子进程 stdio 通信；`_ALLOWED_TABLES` 表名白名单防 SQL 注入；`_MARKET_DB_LOCK` 串行化库访问 | `https://github.com/modelcontextprotocol/python-sdk` `2.3.0` | **MIT** | **风险**：Pi 对超过 20KB 的工具输出自动中间截断导致数值丢失。<br>**缓解**：`_compact_backtest_payload()` 与 `query_stored_market_data` 摘要裁剪确保关键指标与曲线均在 16KB 阈值内。 |
| **Pi Coding Agent (`@earendil-works/pi-coding-agent`, `pi_runner.py`)** | 多轮投研 Agent 执行器，调度 DeepSeek (`deepseek-flash`)、`.pi/mcp.json` 与 `.pi/skills/` | **In**: 研究 Prompt、`.pi/mcp.json`、`.pi/skills/`、`DEEPSEEK_API_KEY`<br>**Out**: NDJSON 事件流、工具轨迹与回答 | Node.js `v24.15.0`, `DEEPSEEK_API_KEY`, MCP Server | 使用 `--mode json --no-session --approve --tools read,mcp__* --no-context-files` 执行，**显式禁用 `bash`/`edit`/`write`** | `https://github.com/earendil-works/pi` (`packages/coding-agent`) `1.6.1` | **MIT** | **风险**：本地命令/写文件越权风险、子进程挂死或工具成功但最终文本为空。<br>**缓解**：强制 `--tools read,mcp__*` 最小权限集、`selectors` 超时与进程组 `SIGKILL` 清理、`_synthesize_answer_from_tools()` 兜底合成。 |
| **Pydantic / Pandas / NumPy** | FastAPI 与 MCP 参数严格校验、技术指标与数值序列计算 | **In**: JSON 请求与 OHLCV DataFrame<br>**Out**: 强类型模型、有限浮点数组 | Python 3.12 | 进程内纯内存校验，拒绝 `NaN`/`Inf` 与 `bool` 伪装整数 | `pydantic` (`2.14.4`), `pandas` (`3.1.1`), `numpy` (`2.5.5`) | **MIT** (`pydantic`) / **BSD-3-Clause** (`pandas`, `numpy`) | **风险**：`NaN`/`Inf` 破坏标准 JSON 序列化或指标计算。<br>**缓解**：Pydantic 校验器与 `_json_safe()` 有限值过滤，拒绝非法浮点传入。 |
| **FastAPI + Uvicorn (`stock_research/api/app.py`)** | 提供 REST API、Agent SSE 流式接口与静态 SPA 托管的 Web 后端 | **In**: HTTP JSON 与 `GET /api/agent/stream`<br>**Out**: JSON、SSE 事件流、`web/dist/` 静态文件 | `fastapi==0.165.0`, `uvicorn==0.54.0`, `anyio` | 默认仅监听 `127.0.0.1:8000`；静态文件路由通过 `.resolve().relative_to()` 防止目录穿越 | `fastapi/fastapi` (`0.165.0`), `uvicorn` (`0.54.0`) | **MIT** (`fastapi`) + **BSD-3-Clause** (`uvicorn`) | **风险**：同步 Agent/DuckDB 阻塞异步事件循环或路径穿越访问。<br>**缓解**：使用 `anyio.to_thread.run_sync` 卸载阻塞任务，严格限制静态文件根目录与 `/api/*` 404 分离。 |
| **React + Vite + TS + ECharts + Lightweight-Charts (`stock_research/web/`)** | 4 视图交互式投研与模拟交易前端 SPA | **In**: `/api/*` REST 与 SSE 响应<br>**Out**: K 线蜡烛图 (`lightweight-charts`)、净值/相关性/盈亏图 (`echarts`)、GFM Markdown 投研结论渲染 (`react-markdown` + `remark-gfm`) | 浏览器 DOM；构建期依赖 Node.js | 纯静态浏览器端运行，不持有任何第三方 API Key；开发态 Vite 仅代理 `/api` | `react@19.3.1`, `react-dom@19.3.1`, `react-markdown@10.1.0`, `remark-gfm@4.0.1`, `vite@6.4.6`, `@vitejs/plugin-react@4.7.0`, `@types/react@19.3.3`, `@types/react-dom@19.3.0`, `typescript@5.9.3`, `echarts@5.6.0`, `lightweight-charts@5.3.0` | **MIT** (`react`, `react-dom`, `react-markdown`, `remark-gfm`, `vite`, `@vitejs/plugin-react`, `@types/react`, `@types/react-dom`) + **Apache-2.0** (`echarts`, `lightweight-charts`, `typescript`) | **风险**：SSE 连接中途断开或渲染外部 Markdown/链接触发 XSS。<br>**缓解**：`EventSource` 异常自动降级至 `POST /api/agent/query`，使用 `react-markdown` AST 渲染（无 `dangerouslySetInnerHTML`）并限制超链接仅允许 `http:`/`https:` 协议。 |
| **Playwright & pytest (`stock_research/tests/`)** | 无头 Chromium UI 验证及单元/集成/4-Tier E2E 自动化测试框架 | **In**: 测试用例与真实运行实例<br>**Out**: 测试与 E2E 报告 | `/usr/bin/chromium`, Python `.venv` | 仅声明于 `[project.optional-dependencies] dev`，隔离于 scratch 目录运行 | `Microsoft/playwright-python` (`1.65.2`), `pytest` (`9.2.0`) | **Apache-2.0** (`playwright`) + **MIT** (`pytest`) | **风险**：WSL2/Linux 长 `TMPDIR` 导致 Chromium `SingletonSocket` 启动失败。<br>**缓解**：测试夹具在短路径下分配 `p<hex4>` 临时目录并在 `finally` 中回收。 |

### 2.2 开源 Skills 审查矩阵（`.pi/skills/*/SKILL.md`）

| Skill 名称 | 职责 (Role) | 输入 / 输出 (Inputs / Outputs) | 依赖 (Dependencies) | 权限与安全边界 (Permissions & Security) | 上游来源与 Commit SHA (Source / Commit) | 开源许可 (License) | 潜在风险与缓解措施 (Risks & Mitigations) |
|---|---|---|---|---|---|---|---|
| **`stock-research`** (`.pi/skills/stock-research/SKILL.md`) | A 股/美股个股与行业近期动态（如近 3 日电力板块）多轮研判工作流 | **In**: 行业或股票研究问题<br>**Out**: 含真实数值与来源的研报 | `query_stored_market_data`, `fetch_market_data`, `jevdb_semantic_query`, `web_search`, `web_fetch_url` | 仅限只读 `read` + MCP 工具；无 `bash`/`edit`/`write`；遵循 Apache-2.0 第 6 条不构成官方背书 | `https://github.com/prof-little-bear/cc-equity-research` @ `7c428944a6718c35461f839c618ae66334b6371b`（源起 `anthropics/financial-services` @ `853f755a61f7bbb045c681327f46b354419030a1`） | **Apache-2.0** | **风险**：原版依赖 FactSet/S&P 等付费 MCP；抓取网页含 Prompt 注入风险。<br>**缓解**：已全部替换为零注册 JEVDB MCP 工具；声明外部文本不可信，结果受 `verifier.py` 强制落地检查。 |
| **`cross-verify`** (`.pi/skills/cross-verify/SKILL.md`) | 多源财务/行情/网页交叉核验与认知标签纪律（`FACT` / `CALCULATION` / `ESTIMATE / VIEW` / `UNKNOWN / N/A`） | **In**: 多工具返回的数值、日期、URL 与陈述<br>**Out**: 核验对账后的客观分析 | JEVDB MCP 全量查询/抓取工具、`web_search`、`web_fetch_url` | 仅限只读 `read` + MCP；禁用本地写入与命令行执行 | 1) `https://github.com/jinhyeonseo01/stock-trade-analysis-skills` @ `66ebac37bfc51a4918fc1edc0a6d84760851d52d`<br>2) `https://github.com/badlogic/pi-skills` @ `90bb51cae36515a648515b633a81c0c6efc8c74d` | **MIT** | **风险**：不同数据源间单位（`亿元` vs 百万美元）、币种与报告期（`9M` vs 全年）不一致引发误读。<br>**缓解**：移除付费 Brave Search API 改用免费 `ddgs` + SSRF 防护抓取；强制显式标注来源、时间窗与单位冲突。 |
| **`value-investing`** (`.pi/skills/value-investing/SKILL.md`) | 中长线基本面价值分析（ROE、利润率、负债、PE/PB 安全边际与价值策略回测） | **In**: 目标标的（如 `000858`, `AAPL`）<br>**Out**: 价值论点、财务对比表、价值回测与风险指标 | `query_stored_market_data`, `fetch_market_data`, `jevdb_semantic_query`, `list_quant_strategies`, `run_backtest_tool`, `analyze_portfolio_risk` | 仅限只读 `read` + MCP；输出仅供研究参考，不构成持牌投资建议 | `prof-little-bear/cc-equity-research` @ `7c428944a6718c35461f839c618ae66334b6371b` + `jinhyeonseo01/stock-trade-analysis-skills` @ `66ebac37bfc51a4918fc1edc0a6d84760851d52d` | **Apache-2.0 AND MIT** | **风险**：财报发布滞后及中美会计准则口径差异。<br>**缓解**：交叉比对 `fundamentals` 与 `edgar_filings` 原始申报日期，并结合历史价值策略回测与组合风险度量。 |
| **`quant-strategy`** (`.pi/skills/quant-strategy/SKILL.md`) | 声明式日频量化策略设计、回测对比、组合风险评估与合规模拟下单工作流 | **In**: 策略 AST、标的列表、区间与下单意图<br>**Out**: 多策略对比表、风控指标、模拟成交结果 | `list_quant_strategies`, `save_quant_strategy`, `run_backtest_tool`, `compare_strategies_tool`, `analyze_portfolio_risk`, `paper_account_status`, `submit_paper_order` | 仅持久化声明式 JSON AST（无任意代码执行）；仅操作本地模拟账本，绝不连接真实券商 | `https://github.com/jinhyeonseo01/stock-trade-analysis-skills` @ `66ebac37bfc51a4918fc1edc0a6d84760851d52d` | **MIT** | **风险**：策略过拟合、代码注入或未经授权误下模拟单。<br>**缓解**：`strategies.py` 强制受限 AST 白名单；仅在用户明确提出下单请求时调用 `submit_paper_order`，并受账本 A 股规则拦截保护。 |

### 2.3 明确排除的高风险开源组件（许可证合规审查）

- **`sec-edgar-mcp`（已排除 / 未使用 — Excluded / Not Used）**：早期技术调研曾关注社区项目 `sec-edgar-mcp`，但审查发现其采用 **AGPL-3.0** 强传染性开源许可（对网络服务/Web 后端存在源码传染风险）。因此本项目**完全排除了 `sec-edgar-mcp`**（`stock_research/pyproject.toml` 与运行虚拟环境中均零引入），转而使用 `httpx`（BSD-3-Clause）在 `stock_research/storage/ingest/sec_edgar.py` 中直接对接 SEC 官方免费 REST 接口。

---

## 3. 模拟交易市场规则与简化假设

持久化模拟交易账本（`stock_research/quant/ledger.py`，`PaperAccountService`）与历史回测引擎（`stock_research/quant/backtest.py`，`MarketRule`）共同实现了显式、可验证、可自省（`get_simplification_assumptions()`）的市场规则模型。

### 3.1 A 股与美股核心市场规则参数对照

| 规则维度 | 中国 A 股 (`CN`) | 美国股市 (`US`) | 代码依据 (`ledger.py` / `backtest.py`) |
|---|---|---|---|
| **交收制度 (`t_plus`)** | **`T+1`**：当日买入股数计入总持仓但**不可当日卖出**；`sellable_qty` 仅统计 `buy_date < trade_date` 的批次，调用 `advance_day()` 推进至下一交易日后解锁 | **`T+0`**：当日买入份额可于同日卖出 | `ledger.py` `get_simplification_assumptions()`、`submit_order()`、`advance_day()`；`DEFAULT_CN_RULE` / `DEFAULT_US_RULE` |
| **交易单位 (`lot size`)** | 买入须为 **`100` 股** 正整数倍；卖出允许 `100` 股整数倍，或当可用零股 `sellable % 100 != 0` 时允许一次性卖出 `sellable` 全部或 `sellable % 100` 零股尾数 | 买入与卖出最小单位均为 **`1` 股** 正整数 | `ledger.py` `invalid_cn_buy_lot` / `invalid_cn_sell_lot`；`backtest.py` `buy_lot=100, sell_lot=1` |
| **涨跌幅限制 (`price_limit`)** | 默认 **`±10%`**（`0.10`，基于 `prev_close` 四舍五入至最小报价单位）；支持通过 `price_limit_rate` 参数覆写（如 ST 股 `0.05`、创业板/科创板 `0.20`） | **无涨跌幅限制 (`None`)** | `ledger.py` `price_limit_default: 0.10`；`DEFAULT_CN_RULE.price_limit_rate=0.10` |
| **最小报价单位 (`tick_size`)** | **`0.01`**（采用 `Decimal` `ROUND_HALF_UP` 精确舍入） | **`0.01`**（`ROUND_HALF_UP`） | `ledger.py` `_TICK_SIZE = 0.01` |
| **滑点模型 (`slippage`)** | 默认 **`5 bps` (`0.0005`)**：买入成交价 = `ref * (1 + 0.0005)`，卖出成交价 = `ref * (1 - 0.0005)`，按 `tick_size` 舍入；限价单分别受 `min(limit, slipped)` 与 `max(limit, slipped)` 钳位 | 默认 **`5 bps` (`0.0005`)**，计算逻辑同左 | `ledger.py` `slippage_bps=5.0`；`backtest.py` `slippage_rate=0.0005` |
| **佣金 (`commission`)** | **`0.03%` (`0.0003`)**，买卖双边收取，**最低 `¥5.00` / 笔** | 账本默认 **`0.0`**（支持配置 `us_commission_rate` / `us_min_commission` / `us_per_share_fee`）；回测 `DEFAULT_US_RULE` 采用 `0.02%` (`0.0002`)、最低 `$1.00` | `ledger.py` `_compute_fees()`；`backtest.py` `DEFAULT_CN_RULE` / `DEFAULT_US_RULE` |
| **过户费 (`transfer_fee`)** | **`0.001%` (`0.00001`)**，买卖双边收取 | **`0.0`** | `ledger.py`；`DEFAULT_CN_RULE.transfer_fee_rate=0.00001` |
| **印花税 (`stamp_duty`)** | **`0.05%` (`0.0005`)**，**仅卖出单边收取** | **`0.0`** | `ledger.py`；`DEFAULT_CN_RULE.stamp_duty_rate=0.0005` |
| **成本核算 (`cost_basis`)** | **FIFO（先进先出）批次核销**：`ORDER BY buy_date ASC, created_at ASC`，买入批次单位成本含入场手续费 (`total_outflow / quantity`) | **FIFO** 批次核销，含入场手续费 | `ledger.py` `position_lots` 表 |

### 3.2 订单有效性校验与拒绝留痕

`PaperAccountService.submit_order()` 在变更任何资金或持仓前执行全链路前置校验，任何非法委托均返回 `{"accepted": false, "status": "REJECTED", "reject_code": "...", "reason": "..."}` 并将拒单审计记录写入 `orders` 表（现金与持仓保持不变）：
- 日期与字段校验：`invalid_trade_date`（严禁 `trade_date < current_date` 时间回退）、`invalid_symbol`、`invalid_market`、`invalid_side`、`invalid_order_type`、`invalid_quantity`、`invalid_limit_price`、`invalid_price_limit_rate`。
- 手数与持仓校验：`invalid_cn_buy_lot`、`insufficient_position`、`t_plus_one_restricted`（当日买入触发 T+1 拦截）、`invalid_cn_sell_lot`。
- 价格与区间可行性校验：`missing_reference_price`、`invalid_reference_bar`、`price_limit_exceeded`（含封涨停买入 / 封跌停卖出拦截）、`limit_price_not_met`、`outside_bar_range`（含滑点成交价必须落在参考 K 线 `[low, high]` 区间内）。
- 资金充足性校验：`insufficient_cash`（校验 `notional + total_fee <= cash`）。

### 3.3 历史回测与交互式账本的执行模型差异

| 对比维度 | 历史回测引擎 (`stock_research/quant/backtest.py`) | 交互式模拟账本 (`stock_research/quant/ledger.py`) |
|---|---|---|
| **信号与成交时序** | **严格因果**：在 `day_t` 收盘后基于截至 `day_t` 的K线/财务/新闻生成 `pending_signals`，于 `day_{t+1}` **开盘价**撮合（先卖后买） | **提交即同步撮合**：基于当前传入或最新 `reference_bar` 校验 `[low, high]`、限价与涨跌停后立即成交 |
| **流动性与仓位约束** | 受 `max_participation_rate=0.01`（单笔不超过日成交量 1%）与 `max_weight`（CN `0.20`、US `0.25`）限制 | 不按历史成交量拆分部分成交，仅以 `[low, high]` 校验可执行性 |

### 3.4 显式简化假设 (`get_simplification_assumptions()`)

1. **无部分成交与盘口挂单簿**：模拟委托通过全部有效性及 K 线区间校验后即视为一次性全额同步成交（不支持挂单队列与部分成交）。
2. **不含公司行动与杠杆做空**：不模拟分红派息、送转拆股、配股增发、融资融券及裸卖空。
3. **单一基础货币资金池**：同一模拟账户共用单一现金池（未引入跨币种实时汇率换算与外汇结算延迟）。
4. **未建模盘口冲击成本**：除 K 线 `[low, high]` 可行域及 `5 bps` 固定滑点外，交互式下单不模拟大单对盘口深度的动态冲击。

---

## 4. 关键技术决策与交叉核验机制

### 4.1 非侵入式外围架构设计
- **决策**：`/home/tao/JEVDB` 原有 C++ 扩展（`src/`）、Python 包装（`python/jevdb/`）、股票示例（`examples/stock_trading/`）及现有测试保持 100% 只读，所有新增逻辑全部封装在 `stock_research/`、`.pi/` 与 `docs_project/` 中；运行时在 DuckDB 中通过 `source='static_baseline'` 标签联合查询原有静态 CSV 与动态表。
- **收益**：保证核心 16 项基线测试零退化，同时完整继承 JEVDB 论文中的 SBF 布隆剪枝与语义 SQL 能力。

### 4.2 `ShimServer` 解决 JEVDB C++ 与 DeepSeek API 协议不兼容
- **问题**：JEVDB C++ (`src/llm_client.cpp`) 向大模型发送 OpenAI `response_format: {"type": "json_schema", "strict": true}`，而 DeepSeek 官方接口仅支持 `{"type": "json_object"}`，且推理模型可能附带 `reasoning_content` 或 Markdown 代码块围栏。
- **决策**：构建轻量级本地 HTTP 代理 `ShimServer`（`stock_research/storage/llm_shim.py`，默认 `127.0.0.1:18089`），在不修改 C++ 源码的前提下完成：
  1. 将 `json_schema` 映射为 `json_object`，并向系统消息注入 `[STRICT JSON OUTPUT REQUIREMENT]` 字段约束；
  2. 清理输出中的 `reasoning_content` 与 ` ```json ` 围栏；
  3. 对照提取的 `SchemaSpec`（`number` / `enum` / `boolean` / `numbered_boolean`）校验并归一化 `{"answers": [...]}`，失败时携带诊断反馈与翻倍 `max_tokens`（上限 32768）自动重试一次。

### 4.3 AkShare 多源回退路由规避 `push2` 限流
- **问题**：东方财富 `push2` 行情接口在自动化高频测试或特定网络环境下极易发生连接丢包与限流。
- **决策**：`stock_research/storage/ingest/akshare_cn.py` 彻底放弃 `push2` 接口，采用确定性多源降级路由：
  - 日线行情：腾讯 (`stock_zh_a_hist_tx`) → 新浪 (`stock_zh_a_hist_sina`)；
  - 估值与财务：百度估值 (`stock_zh_valuation_baidu`) + 东财数据中心 (`stock_financial_analysis_indicator_em`) → 同花顺 (`stock_financial_key_indicator_ths`) → 百度纯估值兜底；
  - 板块成分股 4 级保障：6 小时新鲜本地 JSON 缓存 → 新浪实时 `stock_sector_detail` → 30 天陈旧缓存 → DuckDB 历史快照 + 申万行业 (`index_component_sw`) 回退。

### 4.4 DuckDB 并发锁治理与单写者纪律
- **问题**：DuckDB 单文件数据库不允许跨进程并发写锁，FastAPI、MCP Server、CLI 与后台任务交替读写容易触发锁冲突。
- **决策**：`schema.py` 与 `ledger.py` 统一采用**短生命周期连接 + 事务即用即关**模式，叠加进程内 `threading.RLock`（`_SCHEMA_LOCK` / `_PATH_LOCKS`）、跨进程 `fcntl.flock(..., LOCK_EX)` 文件锁以及 `duckdb.IOException` / `TransactionException` 指数退避重试，并在同一调用链内透传 `con=con` 避免自我死锁。

### 4.5 安全边界与防越权机制
1. **Pi Agent 工具沙箱（`pi_runner.py`）**：Web 与 CLI 调用 `pi` 时强制传入 `--tools read,mcp__* --no-context-files`，**完全禁用 `bash`、`edit`、`write`**，杜绝提示词注入导致本地任意命令执行或源码篡改。
2. **策略声明式 AST（`strategies.py`）**：拒绝 `eval`/`exec` 与任意 Python 脚本，策略规则仅允许最大深度 16、最大节点数 256 的受限 JSON AST（白名单操作符与组合子 `all`/`any`/`not`/`const`）。
3. **联网防 SSRF 与 DNS 重绑定（`web_tools.py`）**：`web_fetch_url` 封锁内网/回环/链路本地 IP、本地后缀及 `r.jina.ai`，并通过 `_PinnedNetworkBackend` 将 `socket.connect` 固定到已校验的解析 IP 消除 DNS-rebinding TOCTOU，同时逐跳校验最多 5 次重定向并剥离 HTML 脚本标签。

### 4.6 多层交叉核验机制（`cross-verify` + `verifier.py` + `pi_runner.py`）
为防止 LLM 在金融数据总结中出现数字编造、错配或虚假链接，系统构建了**“提示规范 + 隔离提取 + 自动审计 + 多轮修复”**四层防线：
1. **研究纪律规范（`.pi/skills/cross-verify/SKILL.md`）**：要求 Agent 对重要结论至少交叉比对 2 个独立工具源，严格区分 `FACT` / `CALCULATION` / `ESTIMATE / VIEW` / `UNKNOWN / N/A`，并核对齐币种、量纲与日期窗。
2. **错误工具输出隔离（`pi_runner.py` / `verifier.py`）**：凡 `is_error=True` 或以 `"Error executing tool"` 开头的失败工具输出，均被排除在 `non_error_tool_outputs` 与核验证据语料之外，防止错误报文里的数字被误当成有效依据。
3. **程序化落地核验（`verifier.py` `verify_answer_grounding`）**：
   - 检查非空回答、非错误工具调用存在性及来源引用；
   - 通过 `_SortedFloatIndex` 对回答中的全部数值进行精度容差比对，支持中文/英文量纲换算（`亿` / `万` / `T` / `B` / `M` / `K`）、百分比互转、表格列头及行内四则算式推导（`operand1 op operand2 = result`），同时精准豁免 `3M/6M/9M/12M` 财报期标签等结构性标识；
   - 对回答中出现的每一条 `http(s)://` URL 执行**精确成员比对**（必须真实存在于非错误工具输出或已提取来源集合中，防止前缀伪造主页链接）。
4. **自动修复与空回答兜底（`pi_runner.py`）**：若初版回答 `grounded=False`，自动触发最多 **3 轮** DeepSeek 修复循环（第 2 轮起启用 `strict_delete=True` 删除未落地数字与链接，且仅在 `grounded` 通过或未落地项总数严格减少时采纳）；若 Pi 工具执行成功但意外返回空文本，由 `_synthesize_answer_from_tools()` 基于已采集的真实工具结果兜底生成结构化报告并再次经过 `verify_answer_grounding` 核验。
