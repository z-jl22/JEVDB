# JEVDB 股票模拟交易项目：中文运行指南

这份指南供课程小组成员在自己的电脑上安装、运行和复现实验。请先完成环境与连接检查，再运行股票交易程序。

本项目基于 JEVDB：它在 DuckDB 的 SQL 查询中加入语义算子，可以调用模型理解文本。股票示例用 `jev_choose` 判断新闻对公司业务的影响，再由 Python 模拟引擎结合行情、资金和交易规则计算订单及资产变化，形成“用户需求 → 工具调用 → 结果展示”的流程。程序只做本地模拟，不连接券商账户。

## 1. 获取完整的团队版本

请从小组共享仓库、指定分支或压缩包获取本项目。上游仓库为 [PDAIS/JEVDB](https://github.com/PDAIS/JEVDB)，但仅克隆上游仓库不一定包含小组新增的股票交易程序。

拿到文件后，进入包含 `pyproject.toml` 的 `JEVDB` 文件夹。后面的命令均在这个目录执行：

```sh
cd /你的项目位置/JEVDB
```

请确认至少包含以下内容：

```text
JEVDB/
├── pyproject.toml                 # Python 包与 DuckDB 版本依赖
├── python/                       # Python 包与扩展构建配置
├── src/                          # JEVDB 扩展源码
├── examples/stock_trading/
│   ├── trade.py                  # 交互与批量运行入口
│   ├── test_trade.py             # 本地测试
│   ├── prices.csv               # 历史行情与股票名称
│   ├── news.csv                 # 新闻及出处
│   ├── trading_rules.csv        # 交易参数
│   └── README.md                # 交易机制详细说明
└── README_中文运行指南.md
```

分享项目的成员应确认以上新增文件已经提交或放入压缩包。不要把个人 API Key、整个 Conda 环境或本机编译产物当作必需文件分发；不同操作系统和 CPU 架构需要匹配各自的扩展。

## 2. 使用 Conda 创建环境

先安装 Anaconda 或 Miniconda，然后打开终端。Windows 可以使用 Anaconda Prompt；若后续缺少兼容扩展，建议使用 WSL2 的 Linux 环境。

```sh
conda create -n jevdb python=3.12 -y
conda activate jevdb
python -m pip install --upgrade pip
python -m pip install "jevdb==0.2.0" "duckdb==1.4.3"
python -m pip check
```

本地已验证的组合为 **Python 3.12、JEVDB 0.2.0、DuckDB 1.4.3**。当前仓库明确依赖 DuckDB 1.4.3，请不要单独升级 DuckDB。股票示例其余依赖均为 Python 标准库，不需要另外安装 `openai`、`pandas` 或 `backtrader`。

上面的安装方式取决于包源是否提供适合当前系统和架构的包。若安装失败，或者下一步加载扩展失败，请按第 4 节从团队源码构建。

## 3. 先验证 JEVDB 能否正常连接

在已激活的环境中执行：

```sh
python -c "import sys, importlib.metadata as m; print(sys.executable); print('jevdb:', m.version('jevdb')); print('duckdb:', m.version('duckdb'))"
python -c "import jevdb; con = jevdb.connect(); print(con.execute('SELECT 1 + 1').fetchall()); con.close()"
```

第二条命令预期输出：

```text
[(2,)]
```

这表示 Python 包、DuckDB 和 JEVDB 扩展已经能够一起加载并执行 SQL。**这个检查不调用模型、不需要 API Key**；它还不能证明模型服务配置正确。

## 4. 安装失败或扩展符号错误：从源码构建

如果第 3 节已成功，可以直接跳到第 5 节。以下步骤用于没有匹配的预编译包，或出现 `symbol not found`、`PhysicalJoin...BuildPipelines` 等扩展加载问题的情况。

### 4.1 安装编译工具

保持 `jevdb` 环境激活，安装构建依赖：

```sh
conda install -c conda-forge "cmake>=3.24,<4" ninja openssl pkg-config -y
```

此外，需要支持 C++20 的编译器：

- **macOS**：执行 `xcode-select --install` 安装 Command Line Tools；如果已经安装，按系统提示跳过。
- **Ubuntu / WSL2 Ubuntu**：执行 `sudo apt-get update`，再执行 `sudo apt-get install -y build-essential`。
- **原生 Windows**：需要合适的 MSVC C++ 构建工具，本指南没有验证该构建路径。推荐在 WSL2 Ubuntu 内重新安装 Conda 并执行 Linux 流程，不要混用 Windows 和 WSL 的 Python 环境。

### 4.2 从当前团队仓库安装

在 macOS / Linux / WSL2 的终端、仓库根目录执行：

```sh
conda activate jevdb
export CMAKE_ARGS="-DOPENSSL_ROOT_DIR=$CONDA_PREFIX"
python -m pip install --force-reinstall --no-cache-dir .
```

注意最后的 `.`，它表示从当前目录构建安装。构建会下载 DuckDB 1.4.3 源码并编译，首次安装可能耗时较长，需要能够访问下载站点与 Python 包源。

团队版本的 `python/CMakeLists.txt` 应包含：

```cmake
-DBUILD_EXTENSIONS_ONLY=OFF
-DEXTENSION_STATIC_BUILD=ON
```

这个配置使扩展静态链接所需的 DuckDB 代码，用于解决官方 Python DuckDB 包未导出相关 C++ 符号时的加载问题。若拿到的文件仍是旧配置，请先获取团队的修正版。不要直接复制其他系统上的 `.duckdb_extension` 文件。

安装结束后重新执行第 3 节的两条检查命令。更完整的底层扩展构建说明见 [docs/building.md](docs/building.md)。本指南的股票程序通过 Python 启动，不需要另外启动 DuckDB 命令行或数据库服务器。

## 5. 配置模型服务与 API Key

股票程序支持两种后端：

| 交互菜单选择 | 后端参数 | 默认服务地址 | 默认模型 | 读取的环境变量 |
| --- | --- | --- | --- | --- |
| `1` OpenAI 兼容 | `llm` | `https://open.bigmodel.cn/api/coding/paas/v4` | `glm-5.3` | `OPENAI_API_KEY` |
| `2` Jev | `jev` | `https://app.vllm-sr.ai/v1` | `vllm-sr/d3-ultra` | `JEV_API_KEY` |

这些是程序内置的默认值，不代表每位成员的账号都有对应模型权限。请以自己的服务商提供的地址、模型标识和权限为准；交互启动时可以修改地址与模型。

**使用 OpenAI 兼容后端时不需要 `JEV_API_KEY`。** `OPENAI_API_KEY` 是程序读取的变量名称，也可以装入其他 OpenAI 兼容服务商的 Key，但 Key 必须与所选服务地址对应。OpenAI 官方发放的 Key 不能用于上表的其他服务商地址。

最简单的做法是先不设置环境变量，运行程序后在提示处输入 Key。输入时终端不会显示字符，这是正常的；粘贴后按回车即可。程序不会自动读取 `.env` 文件，也不会自动读取 `reviews.py` 中的 Key。

也可以在当前终端会话提前设置。macOS / Linux / WSL2 示例：

```sh
export OPENAI_API_KEY='替换为所选服务商的真实Key'
```

Windows PowerShell 示例：

```powershell
$env:OPENAI_API_KEY = '替换为所选服务商的真实Key'
```

若使用 Jev 后端，将变量名换成 `JEV_API_KEY`。不要把真实 Key 写入 README、CSV 或提交到 Git。

新新闻的分类需要网络和有效 Key；已有的匹配分类缓存可复用。程序会把新闻标题、正文和股票代码发送给所选模型服务，可能产生调用费用。OpenAI 兼容服务还需支持 Chat Completions 的严格 JSON Schema 输出；地址可访问并不等于这个接口能力一定兼容，应以首次实际分类是否成功为准。

## 6. 推荐：通过交互模式完成第一次实验

在仓库根目录执行：

```sh
conda activate jevdb
python examples/stock_trading/trade.py
```

按以下流程操作：

1. **选择股票**：程序显示 `000858（五粮液）`、`601318（中国平安）`。直接回车选择全部，也可输入单个代码或用英文逗号分隔多个代码。
2. **选择日期**：程序提示所选股票共同拥有行情的时间范围。自带数据的范围为 `2025-04-24` 至 `2025-05-15`，第一次实验建议开始与结束日期均直接回车。
3. **输入初始资金**：直接回车使用 `100000` 元。
4. **选择后端、地址与模型**：使用自己有权限的服务；采用默认值前先核对第 5 节。
5. **准备阶段**：输入 `1` 查看新闻；输入 `2` 调整资金、佣金和滑点；输入 `3` 才开始调用 JEVDB 分类。若尚未设置 Key，此时会提示输入。
6. **查看模型判断**：逐条显示 `positive`、`negative` 或 `neutral`，并标注是新调用还是缓存。输入 `1` 查看新闻事实、出处和分类；输入 `3` 开始模拟交易。
7. **查看交易结果**：程序展示期末资产、收益率、最大回撤与持仓。结果菜单中输入 `1` 查看订单，输入 `2` 查看每日资产，输入 `4` 查看保存目录。
8. **继续实验**：输入 `3` 修改资金或费用并重跑当前区间；输入 `5` 重新选择股票、日期和模型，开始新一轮；输入 `0` 退出。

每轮都从空仓和指定初始资金开始，属于独立实验，不会继承上一轮持仓。仅修改资金或费用重跑时复用已经得到的新闻判断。判断菜单中的“重新调用模型”会绕过缓存，可能再次计费。

自带新闻只有两条，披露日期都是 `2025-04-26`。若选的区间不包含新闻，程序会提示重新选择日期。首次运行建议保留完整默认区间。

## 7. 批量运行与自定义配置

需要固定参数复现实验时，可以直接运行脚本，不必使用交互式 Python：

```sh
python examples/stock_trading/trade.py --symbols 000858,601318 --start 2025-04-24 --end 2025-05-15 --capital 100000 --output .tmp/experiment_01
```

批量模式的 `--symbols`、`--start`、`--end` 必须同时提供。默认使用 `llm` 后端与第 5 节的默认地址和模型。

指定另一个 OpenAI 兼容服务时，先设置该服务的 `OPENAI_API_KEY`，再修改下面两个占位值：

```sh
python examples/stock_trading/trade.py --symbols 000858,601318 --start 2025-04-24 --end 2025-05-15 --backend llm --endpoint "https://你的服务地址/v1" --model "你的模型标识" --output .tmp/experiment_02
```

使用 Jev 服务时，先设置 `JEV_API_KEY`：

```sh
python examples/stock_trading/trade.py --symbols 000858,601318 --start 2025-04-24 --end 2025-05-15 --backend jev --output .tmp/experiment_jev
```

其他参数：

| 参数 | 用途 |
| --- | --- |
| `--interactive` | 显式启动交互模式 |
| `--prices 路径` | 替换行情 CSV |
| `--news 路径` | 替换新闻 CSV |
| `--rules 路径` | 替换交易参数 CSV |
| `--output 路径` | 指定结果与缓存目录，默认 `.tmp/stock_trading` |
| `--help` | 查看全部命令参数 |

输入文件默认按脚本所在位置查找；相对 `--output` 路径则相对于启动命令时的工作目录。**批量模式重复使用同一输出目录会覆盖同名结果文件**，比较实验时请分别指定不同目录。

## 8. 数据、交易规则与结果含义

自带 `prices.csv` 包含两只股票在 13 个交易日的 26 条行情，来自东方财富的未复权日线；`news.csv` 是两份一季度公告中财务事实的简短改写，保留了原始来源链接；`trading_rules.csv` 是可调整的模拟假设。数据出处和更完整机制见 [股票示例说明](examples/stock_trading/README.md)。

三个 CSV 都在开头用 `#` 注释解释每列。程序会自动跳过注释；用其他 CSV 工具导入时需另外跳过这些行。文件使用 UTF-8，股票代码必须作为文本保留，例如 `000858` 不能变成 `858`。

| 文件 | 主要内容与单位 |
| --- | --- |
| `prices.csv` | 日期、股票代码、开高低收价格（元/股）、成交量（手，每手 100 股）、股票名称 |
| `news.csv` | 新闻编号、股票代码、披露时间、标题、正文、来源链接 |
| `trading_rules.csv` | 买卖数量单位、最小报价单位、佣金率与最低佣金、过户费、印花税、滑点、涨跌幅与成交量参与率 |

费率使用小数，如 `0.0005` 表示 `0.05%`。自带新闻的 `T00:00:00` 是披露日期占位，并非核实过的真实发布时间。程序在披露日期之后首个有行情的交易日开盘执行信号，本例为 `2025-04-28`。

交易信号与主要约束如下：

- 同一天同一股票的新闻全部为正面时买入，全部为负面时卖出；有中性或冲突时不交易。模型判断的是对公司业务的影响，并不直接预测股价。
- 根据开盘价和滑点计算成交候选价，检查最小报价单位、当日高低价和涨跌停边界。
- 检查资金、持仓、买卖数量单位、T+1、单只股票 20% 仓位限制，以及当日成交量 1% 的默认参与率限制。
- 双边收取佣金和过户费，卖出另收印花税；不允许卖空，不处理融资、分红与除权等情况。

交互模式每次模拟保存到独立目录：

```text
.tmp/stock_trading/
├── decision_cache.json
└── run_日期_时间_随机标识/
    ├── decisions.csv
    ├── orders.csv
    ├── equity.csv
    └── experiment.json
```

| 输出文件 | 如何使用 |
| --- | --- |
| `decisions.csv` | 查看新闻对应的模型标签 |
| `orders.csv` | 查看成交或跳过状态、股数、价格、费用与原因；出现订单记录不一定表示成交 |
| `equity.csv` | 查看每天现金与总资产的变化 |
| `experiment.json` | 记录本次模型、配置、交易参数与分类结果，用于对照实验；不保存 API Key |
| `decision_cache.json` | 复用相同后端、地址、模型、条件和新闻内容对应的判断，减少重复调用 |

不同模型可能输出不同标签，模拟收益也可能不同。没有成交可能是中性判断、持仓不足或交易约束导致，应先查看订单原因。两条新闻的小样本主要验证程序流程，不能据此证明策略有效；日线也无法保证模拟数量在真实开盘时可成交。

## 9. 不调用模型的本地验证

确认命令参数：

```sh
python examples/stock_trading/trade.py --help
```

运行交易程序测试：

```sh
python -m unittest discover -s examples/stock_trading -p 'test_*.py' -v
```

当前版本包含 11 项测试，通过时末尾显示 `OK`。测试覆盖交易约束、费用、缓存和交互多轮等逻辑，使用固定测试结果，不调用外部模型，也不需要 Key。它们与第 3 节的连接检查分别验证交易逻辑和扩展加载；真实模型接口仍需在第 6 节实际分类时验证。

## 10. 常见问题

### 提示找不到 `jevdb`，或找不到 `trade.py`

先执行 `conda activate jevdb`，用第 3 节命令确认当前 Python 路径。使用 `python -m pip` 保证安装到当前解释器。脚本找不到时确认已经进入仓库根目录，且团队共享文件包含 `examples/stock_trading/`。

### 报错 `symbol not found` / `PhysicalJoin...BuildPipelines`

这是扩展与 DuckDB 的链接或版本兼容问题。先确认 DuckDB 为 `1.4.3`，再按第 4 节使用团队修正后的源码重新构建。不能通过补填 API Key 解决这个错误。

### 报错扩展文件缺失、无法加载或架构不兼容

不要复用其他系统或 CPU 架构的编译产物。获取完整源码后按第 4 节安装，并重新执行 `SELECT 1 + 1` 检查。如果构建报错找不到 OpenSSL，确认已安装 Conda 的 `openssl`，且 `CMAKE_ARGS` 指向当前 `CONDA_PREFIX`。

### 模型请求返回 401 / 403 / 404 / 400

- `401 / 403`：检查 Key 是否有效、是否属于当前 endpoint 的服务商、账号是否有权限或额度。
- `404`：检查服务地址与模型名称是否正确，以及是否需要 `/v1` 等路径。按服务商文档填写，避免重复拼接接口路径。
- `400`：查看错误正文，核对模型是否支持所需的 Chat Completions 与严格 JSON Schema，必要时更换兼容模型或服务。

### 网络、证书或构建下载失败

检查当前机器到包源、DuckDB 源码下载站点和模型服务的网络。如果公司或学校网络需要代理或证书，请按所在网络的配置要求处理。不要通过关闭证书验证来掩盖问题。连接检查成功而分类失败时，优先检查模型服务网络与配置。

### 粘贴 Key 时没有显示，或仍然提示输入 Key

隐藏输入不会回显，粘贴后回车即可。环境变量只对设置它的终端及其子进程生效；换了终端需要重新设置。程序不会自动加载 `.env`，也不会从其他示例脚本读取凭据。

### 日期无效、没有新闻或行情缺失

先使用自带完整区间 `2025-04-24` 至 `2025-05-15`。自定义数据需保证股票代码在三张表中一致，新闻位于实验区间，每个回测交易日都有所有选中股票的完整行情。开始日期必须早于结束日期。

### 想复现实验，但结果与其他成员不同

对照 `experiment.json` 中的模型、地址、标签、初始资金及费率，并确认使用相同 CSV。模型结果可能变化；要比较交易参数的影响，可在同一轮交互中选择“修改资金或费用重跑”，复用本轮分类结果，并保留各次输出目录。

## 11. 首次运行核对清单

1. 已获取包含股票示例和修正构建配置的完整团队版本。
2. 已激活 `jevdb` 环境，DuckDB 版本为 `1.4.3`。
3. 连接检查输出 `[(2,)]`，本地交易测试显示 `OK`。
4. 已准备与 endpoint 匹配的 Key 和可用模型。
5. 使用默认股票与完整日期区间完成新闻分类、模拟及结果查看。
6. 已找到输出目录，并保存本次 `experiment.json` 与 CSV 明细用于课程报告。
