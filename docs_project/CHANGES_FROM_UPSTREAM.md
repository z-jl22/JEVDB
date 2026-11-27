# 与原版 JEVDB (`main`) 的变更总结与合并指南 (`CHANGES_FROM_UPSTREAM.md`)

本文档用于对比本分支 **`feat/stock-research-platform`** 与上游基线 **`main`**（commit `64cf312`）的全部改动，标明每一处新增文件在项目中的模块归属、职责及与原版 JEVDB 的衔接点，便于后续代码审查与无冲突合并。

---

## 1. 总体变更概览

- **基线分支**：`origin/main` (`64cf312`, *"Modify the config"*)
- **开发分支**：`feat/stock-research-platform`
- **示例目录调整**：工作台使用的三个 CSV 位于 `stock_research/data/baseline/`。Python 包和可选 scorer 构建所需的测试用资源位于 `tools/fixtures/`。
- **应用目录**：
  1. `.pi/` — `pi coding agent` 项目级 MCP 服务注册与 4 套开源投研 Skills（5 个文件）
  2. `stock_research/` — 外围股票研究与模拟交易系统全部后端、Agent、MCP、量化、前端与测试代码（57 个文件）
  3. `docs_project/` — 运维手册、项目设计与组件合规审查文档、以及本变更总结（3 个文件）

---

## 2. 按模块划分的改动与对应项目位置

### 2.1 Pi Coding Agent 配置与开源投研 Skills（`.pi/`）

对应需求：**R2（Agent 执行器与开源 Skills 集成）**

| 文件路径 | 行数 | 模块归属 / 作用 |
|---|---:|---|
| `.pi/mcp.json` | 12 | 向本地 `pi coding agent` 注册 `stock_research` stdio MCP 服务入口（`python -m stock_research.mcp_server.server`） |
| `.pi/skills/stock-research/SKILL.md` | 49 | 行业与个股多源动态调研工作流（如“最近 3 天 A 股电力行业”） |
| `.pi/skills/value-investing/SKILL.md` | 54 | 中长线价值投资分析工作流（ROE、PE/PB、估值安全边际、SEC/财报核查） |
| `.pi/skills/quant-strategy/SKILL.md` | 50 | 日频/高频量化策略提出、保存、可复现回测与多策略对比工作流 |
| `.pi/skills/cross-verify/SKILL.md` | 52 | 多源数值、公式推导与引用 URL 的强制交叉核验规范 |

---

### 2.2 外围应用基础配置与统一入口（`stock_research/` 根级）

对应需求：**R1–R4（外围包隔离、统一 CLI 与环境配置）**

| 文件路径 | 行数 | 模块归属 / 作用 |
|---|---:|---|
| `stock_research/.gitignore` | 14 | 忽略外围运行时产物（`.venv/`、`node_modules/`、`dist/`、`*.duckdb`、`__pycache__/` 等） |
| `stock_research/pyproject.toml` | 45 | 外围 Python 包依赖清单（严格锁定 `duckdb==1.4.3` 以兼容原生 `jevdb==0.1.0`） |
| `stock_research/package.json` | 5 | Node 依赖清单（`@earendil-works/pi-coding-agent`） |
| `stock_research/__init__.py` | 3 | 包导出初始化 |
| `stock_research/config.py` | 84 | 统一解析环境变量（DeepSeek、Finnhub、SEC User-Agent、DuckDB / Shim / 策略目录路径） |
| `stock_research/cli.py` | 321 | 统一命令行入口：`ask`（多轮投研）、`ingest`（数据采集）、`backtest`（量化回测）、`paper`（模拟盘交易）、`serve`（启动 Web 服务） |

---

### 2.3 JEVDB 外围动态数据层与多源采集器（`stock_research/storage/`）

对应需求：**R1（不破坏原版静态 JEVDB 的外围动态增量数据与联合语义检索层）**

| 文件路径 | 行数 | 模块归属 / 作用 |
|---|---:|---|
| `stock_research/storage/__init__.py` | 20 | 存储层公共导出 |
| `stock_research/storage/schema.py` | 477 | DuckDB 表结构（`assets`、`market_bars`、`sector_snapshots`、`sector_constituents`、`fundamentals`、`edgar_filings`、`news_events`、`strategies`、`ingestion_runs`）、静态基线加载（读取 `stock_research/data/baseline/`）与非 TTY 进度条禁用 |
| `stock_research/storage/llm_shim.py` | 563 | 本地 OpenAI 兼容 HTTP Shim，桥接原生 JEVDB 语义算子（`jev_holds` / `jev_score` / `jev_choose` / `jev_probability`）至 DeepSeek `deepseek-flash`（含离线 mock 模式） |
| `stock_research/storage/jevdb_bridge.py` | 579 | 联合查询桥接层：融合原版静态基线与动态采集数据，支持 SBF 布隆剪枝（`jev_keys_overlap`）与 JEVDB 语义查询 |
| `stock_research/storage/ingest/__init__.py` | 169 | 采集公共工具（幂等事件 ID、关键词提取、JSON 缓存、重试与入库记录） |
| `stock_research/storage/ingest/akshare_cn.py` | 1,195 | **A 股采集器**（零注册）：板块快照与成分股、个股历史日线（支持相对天数与任意起止日期区间，含 30 天指标预热窗口）、估值/财务指标与东方财富个股新闻 |
| `stock_research/storage/ingest/yfinance_us.py` | 483 | **美股行情/基本面采集器**（零注册）：支持 `period` 与任意 `start_date`/`end_date` 区间拉取、财务报表与新闻 |
| `stock_research/storage/ingest/sec_edgar.py` | 427 | **SEC EDGAR 采集器**（零注册）：美股 CIK 映射、10-K/10-Q/8-K 申报文件与 XBRL 财务事实 |
| `stock_research/storage/ingest/finnhub_us.py` | 359 | **Finnhub 美股监控采集器**：公司/市场新闻与财报日历采集（通过 `FINNHUB_API_KEY` 配置） |
| `stock_research/storage/ingest/scheduler.py` | 279 | 可选定时自动化监控与增量更新调度器（支持单次触发 `trigger_once()` 与后台循环） |

---

### 2.4 量化策略、可复现回测、风险分析与模拟交易账本（`stock_research/quant/`）

对应需求：**R3（价值 + 量化策略库、历史回测、多策略对比、组合风险与持久化模拟盘）**

| 文件路径 | 行数 | 模块归属 / 作用 |
|---|---:|---|
| `stock_research/quant/__init__.py` | 54 | 量化与模拟交易公共接口导出 |
| `stock_research/quant/strategies.py` | 933 | 内置与自定义策略规格模型（涵盖价值投资、日频量化、情绪混合风格）、JSON + DuckDB 双持久化与规则信号求值引擎 |
| `stock_research/quant/backtest.py` | 1,736 | 因果确定性历史回测与多策略对比器（`t` 日收盘信号 → `t+1` 开盘成交；支持 A 股 T+1、100 股买入手数、±10% 涨跌停、精确十进制佣金/过户费/印花税/滑点及可复现哈希） |
| `stock_research/quant/risk.py` | 724 | 策略与投资组合风险指标计算（CAGR、夏普/索提诺/卡玛比率、最大回撤、95% VaR/CVaR、集中度 HHI、相关性矩阵） |
| `stock_research/quant/ledger.py` | 1,597 | 持久化 DuckDB 模拟交易账户与账本（现金与持仓更新、订单有效性检查、T+1 可卖数量锁定与跨日推进解锁、成交价与费用滑点核算） |

---

### 2.5 JEVDB MCP 服务与联网工具链（`stock_research/mcp_server/`）

对应需求：**R2（JEVDB MCP 封装与开放联网检索）**

| 文件路径 | 行数 | 模块归属 / 作用 |
|---|---:|---|
| `stock_research/mcp_server/__init__.py` | 5 | MCP 包初始化 |
| `stock_research/mcp_server/server.py` | 1,051 | 基于 `mcp==2.3.0` 的 stdio MCP 服务端，向 Agent 暴露 12 个工具（JEVDB 语义查询、行情采集/查询、联网检索/网页抓取、策略管理/回测/风控对比、模拟盘查询/下单），具备进程级 DuckDB 并发保护与响应体积控制 |
| `stock_research/mcp_server/web_tools.py` | 473 | 联网搜索（`ddgs` 多引擎并发回退）与网页正文提取（`trafilatura`/`BeautifulSoup`，内置内网/环回地址 SSRF 防护与超时控制） |

---

### 2.6 Pi Coding Agent 执行器与交叉核验器（`stock_research/agent/`）

对应需求：**R2（多轮投研执行、事实核验与自纠偏闭环）**

| 文件路径 | 行数 | 模块归属 / 作用 |
|---|---:|---|
| `stock_research/agent/__init__.py` | 6 | Agent 包初始化 |
| `stock_research/agent/pi_runner.py` | 692 | 调度本地 `pi coding agent` 执行多轮工具调用、解析 JSONL 事件流、向 CLI/Web SSE 推送工具进度，并在核验未通过时触发自动纠偏轮次 |
| `stock_research/agent/verifier.py` | 1,861 | 多源数值/单位换算/百分比计算/URL 链接溯源交叉核验器（`O(log M)` 索引匹配，防止大模型幻觉数字或未抓取引文） |

---

### 2.7 Web 后端与 React/Vite 可视化前端（`stock_research/api/`、`stock_research/web/`）

对应需求：**R4（行情/基本面、多策略回测/风控、模拟持仓/盈亏、Agent 输入框与完整流程呈现）**

| 文件路径 | 行数 | 模块归属 / 作用 |
|---|---:|---|
| `stock_research/api/__init__.py` | 5 | API 包初始化 |
| `stock_research/api/app.py` | 1,262 | FastAPI REST + SSE 后端：提供 `/api/health`、`/api/agent/stream`（SSE）与 `/api/agent/query`、`/api/market/*`、`/api/quant/*`（含缺失时间段 `auto_fetch` 自动补齐历史行情与按时间段计算组合风险）、`/api/paper/*`，并直接托管 `stock_research/web/dist/` |
| `stock_research/web/package.json` & `package-lock.json` | 26 / 3,399 | 前端依赖清单（React 19、Vite 6、TypeScript 5、ECharts 5、Lightweight-Charts 5、`react-markdown`、`remark-gfm`） |
| `stock_research/web/index.html`、`tsconfig.json`、`vite.config.ts` | 13 / 20 / 20 | 前端构建与开发代理配置（`/api` → `127.0.0.1:8000`） |
| `stock_research/web/src/main.tsx` & `App.tsx` | 10 / 141 | SPA 主入口与 4 模块导航布局 |
| `stock_research/web/src/api.ts` | 337 | TypeScript API 契约类型定义与统一请求/格式化工具 |
| `stock_research/web/src/styles.css` | 776 | 暗色投研终端样式系统（含 `color-scheme: dark` 原生日期选择器适配与 GFM Markdown 表格/标题/代码块排版） |
| `stock_research/web/src/components/AgentWorkspace.tsx` | 475 | **视图 1：投研 Agent 工作台**（输入框、模式切换、SSE 实时工具调用轨迹、`react-markdown`+`remark-gfm` 富文本结论渲染、原文切换/复制、核验徽标与可点击来源） |
| `stock_research/web/src/components/MarketView.tsx` | 426 | **视图 2：行情与基本面**（`lightweight-charts` K 线/成交量图、估值 KPI 卡片、板块成分股表、新闻与 SEC 公告、一键按需拉取） |
| `stock_research/web/src/components/QuantView.tsx` | 698 | **视图 3：量化回测与风控**（策略多选与新建保存、自定义起止日期与 5 组快捷时间段预设、缺失区间自动联网补齐开关、多策略净值/回撤 ECharts 曲线、与时间段同步的组合风险 KPI 与相关性热力图） |
| `stock_research/web/src/components/PaperView.tsx` | 530 | **视图 4：模拟持仓与盈亏**（买卖模拟下单、A 股 100 股/T+1 校验反馈、推进交易日解锁持仓、盈亏曲线与委托/成交流水表） |

---

### 2.8 自动化测试与多层级验收套件（`stock_research/tests/`）

| 文件路径 | 行数 | 测试覆盖范围 |
|---|---:|---|
| `stock_research/tests/test_m1_storage_ingest.py` | 1,547 | M1：DuckDB Schema、LLM Shim、JEVDB 桥接、A 股/美股/SEC/Finnhub 采集与定时监控 |
| `stock_research/tests/test_m2_quant_ledger.py` | 1,212 | M2：策略持久化、可复现回测、风险指标与模拟盘 T+1/费率/订单拦截 |
| `stock_research/tests/test_m3_mcp_agent_cli.py` | 1,786 | M3：JEVDB MCP 工具、联网检索、Pi Runner、交叉核验器与 CLI 端到端 |
| `stock_research/tests/test_m4_web_api.py` | 1,153 | M4：FastAPI 全接口、自定义回测区间与风控窗口同步、并发安全、Playwright 无头 Chromium UI（含 Markdown 渲染与 T+1 持久化校验） |
| `stock_research/tests/test_tier5_adversarial.py` | 2,586 | Tier 5：A–K 类白盒对抗回归与边界加固测试（含 `DESIGN.md` 依赖清单检查） |
| `stock_research/tests/e2e/*`（共 10 个文件） | 6,784 | 黑盒 4-Tier E2E 测试基础设施与全链路验收套件（Tier 1 功能 51 项、Tier 2 边界 46 项、Tier 3 跨模块 14 项、Tier 4 真实用户场景 5 项及自检 6 项） |

---

### 2.9 交付与审查文档（`docs_project/`）

对应需求：**R5（可实操运行说明与课程大作业题目二规范文档）**

| 文件路径 | 行数 | 文档说明 |
|---|---:|---|
| `docs_project/RUNBOOK.md` | 391 | 环境校验、环境变量说明、CLI / Web 端到端演示步骤与测试命令手册 |
| `docs_project/DESIGN.md` | 196 | 目标用户与场景输入输出设计、全部组件与开源 Skills 的职责/依赖/权限/来源/License/风险审查表、模拟交易规则与简化假设、关键技术决策与核验机制 |
| `docs_project/CHANGES_FROM_UPSTREAM.md` | 本文档 | 本分支相对原版 `main` 的改动清单、模块映射与合并说明 |

---

## 3. 合并到 `main` 的注意事项

1. **合并检查**：合并时检查静态数据、测试资源的路径以及根目录运行说明。
2. **首次拉取后的环境构建**（以下运行期目录均已通过 `.gitignore` 排除，未入库）：
   - Python 外围依赖：在保留原生 `duckdb==1.4.3` 与 `jevdb==0.1.0` 的前提下安装 `stock_research/pyproject.toml` 依赖；
   - Node / Pi 与前端构建：在 `stock_research/` 安装 `pi coding agent`，并在 `stock_research/web/` 执行 `npm install && npm run build` 生成 `dist/` 静态资源；
   - API 密钥仅通过环境变量（`DEEPSEEK_API_KEY`、`FINNHUB_API_KEY`）在运行时注入，仓库中不含任何硬编码密钥。
3. **已知非阻塞小项（供后续迭代参考）**：
   - `POST /api/market/ingest` 在上游 SEC EDGAR / Finnhub 发生底层 `httpx.HTTPError` 时目前返回 HTTP `500`（后续可细化映射为 `502`/`503`）；
   - 内部进程通信用的 `ShimServer`（`stock_research/storage/llm_shim.py`）收到非法非对象 JSON（如裸数组 `[]`）时会直接断开本地 socket 而非返回 HTTP `400`。
