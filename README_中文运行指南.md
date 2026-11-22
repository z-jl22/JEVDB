# JEVDB 股票模拟交易：运行指南

## 1. 获取项目并创建环境

```sh
git clone https://github.com/z-jl22/JEVDB.git
cd JEVDB
conda create -n jevdb python=3.12 -y
conda activate jevdb
```

## 2. 安装 JEVDB（按系统选择）

**注：由于本人没有在 windows 系统上实际运行，因此没法验证这套配置方法在 windows 系统上是否同样可行，如果不可行可以使用 WSL 或借助 AI 配置**

### Linux / Windows WSL2：优先安装发布包

```sh
python -m pip install --no-cache-dir "jevdb==0.1.0" "duckdb==1.4.3"
```

安装后直接执行第 3 节。若没有兼容包、安装失败或扩展加载报错，使用下面的源码安装步骤。上游初始 wheel 主要面向 Linux x86-64，其他架构可能需要编译。

### macOS，或需要从源码安装的 Linux / WSL2

**macOS** 检查编译工具：

```sh
xcode-select -p
```

若未安装，执行 `xcode-select --install`，完成系统安装后继续。

**Ubuntu / Debian / WSL2 Ubuntu** 安装编译工具（需要支持 C++20 的编译器，建议使用较新的系统版本）：

```sh
sudo apt-get update
sudo apt-get install -y build-essential git
```

其他 Linux 发行版通过自己的包管理器安装对应的 C/C++ 编译工具。

在已激活的 Conda 环境、项目根目录执行：

```sh
conda install openssl -y
python -m pip install "cmake>=3.24,<4" ninja "scikit-build-core>=0.10"
python -m pip install --force-reinstall --no-cache-dir --no-build-isolation --config-settings="cmake.args=-DOPENSSL_ROOT_DIR=$CONDA_PREFIX" .
```

最后的 `.` 必须保留。首次编译需要联网，可能耗时较长；DuckDB 1.4.3 会自动安装

## 3. 验证安装

```sh
python -m pip check
python -c "import jevdb; con = jevdb.connect(); print(con.execute('SELECT 1 + 1').fetchall()); con.close()"
```

分别应显示 `No broken requirements found.` 和 `[(2,)]`。这一步不需要 API Key。

可选：运行本地交易测试，无需调用模型。

```sh
python -m unittest discover -s examples/stock_trading -p 'test_*.py' -v
```

已验证 11 项测试通过，末尾显示 `OK`。

## 4. 启动实验

以后每次打开新终端，只需激活环境、进入项目目录并启动：

```sh
conda activate jevdb
cd /你的项目位置/JEVDB
python examples/stock_trading/trade.py
```

WSL2 用户同样在 Ubuntu 终端执行；将 `cd` 后的路径换成自己克隆项目的目录。

按提示操作：

1. 选择股票、日期和初始资金。首次使用可直接回车保留默认值，日期范围为 `2025-04-24` 至 `2025-05-15`。
2. 选择模型后端，填写自己可用的服务地址与模型名称。
3. 准备菜单输入 `3` 调用 JEVDB；提示时输入 API Key，输入不会回显，粘贴后回车即可。
4. 分类完成后输入 `3` 开始模拟；结果菜单可查看订单、每日资产、文件位置，修改参数重跑或开始新一轮。

| 后端 | 所需 Key | 程序默认地址 | 程序默认模型 |
| --- | --- | --- | --- |
| `1` OpenAI 兼容 | 对应服务商的 `OPENAI_API_KEY` | `https://open.bigmodel.cn/api/coding/paas/v4` | `glm-5.3` |
| `2` Jev | `JEV_API_KEY` | `https://app.vllm-sr.ai/v1` | `vllm-sr/d3-ultra` |

地址、模型与 Key 必须匹配自己的服务权限；OpenAI 兼容服务需支持 Chat Completions 严格 JSON Schema。使用后端 `1` 不需要 `JEV_API_KEY`。程序不会自动读取 `.env` 或其他脚本中的 Key。外部模型调用可能计费，需要在自己的账号下验证。

每轮结果保存到 `.tmp/stock_trading/run_日期_时间_标识/`，包含 `decisions.csv`、`orders.csv`、`equity.csv`、`experiment.json`。每轮从空仓和指定资金开始，不继承上一轮持仓。

本项目为模拟交易，不发送真实订单。