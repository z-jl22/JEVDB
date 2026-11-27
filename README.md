# JEVDB 股票研究与模拟交易平台

## 启动工作台

安装依赖并构建前端后，在仓库根目录通过项目虚拟环境直接启动。

```sh
stock_research/.venv/bin/python -m stock_research.cli serve --host 127.0.0.1 --port 8000
```

打开 [本地工作台](http://127.0.0.1:8000/) 访问投研 Agent、行情与基本面、量化回测和模拟交易。可通过 [健康检查](http://127.0.0.1:8000/api/health) 确认后端已启动。按 `Ctrl+C` 停止服务。

## 首次安装

准备 Python 3.12（确保终端能运行 `python3.12`）、Node.js 22.19 或更新版本，以及支持 C++20 的编译器和 OpenSSL 开发库。macOS 可使用 Xcode 命令行工具和 Homebrew 的 `openssl@3`。在 macOS 上用 Homebrew 安装 OpenSSL 后，先执行 `export CMAKE_ARGS="-DOPENSSL_ROOT_DIR=$(brew --prefix openssl@3)"`。

在仓库根目录执行以下命令。

```sh
python3.12 -m venv stock_research/.venv
stock_research/.venv/bin/python -m pip install .
stock_research/.venv/bin/python -m pip install -e ./stock_research
npm --prefix stock_research install --no-package-lock
npm --prefix stock_research/web ci
npm --prefix stock_research/web run build
```

第一条 `pip install` 会下载 DuckDB 源码并编译 JEVDB，也会安装匹配的 DuckDB 1.4.3 Python 包。首次编译可能需要几分钟。安装完成后，运行上面的启动命令。前端源码有修改时，重新运行 `npm --prefix stock_research/web run build`，然后刷新页面。

首次启动时，工作台会从 `stock_research/data/baseline/` 导入示例行情、新闻和交易参数。

## 使用模型

网页可以直接打开。使用实时投研 Agent 或新闻模型情绪分析时，在投研工作台填写 OpenAI 兼容服务的 **Base URL**、**Model** 和 **API Key**。API Key 在刷新页面后需重新填写。行情、策略与模拟账本保存在项目的 `.tmp/stock_research/` 目录。
