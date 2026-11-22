"""News-driven A-share paper trading with JEVDB decisions."""

import argparse
import csv
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from getpass import getpass
import hashlib
import json
import math
import os
from pathlib import Path
from uuid import uuid4


CONDITION = (
    "Classify the likely impact of this news on the named company's business. "
    "Choose positive for clearly favorable news, negative for clearly adverse news, "
    "or neutral when the impact is unclear or mixed. Do not predict the stock price."
)
LABELS = ("positive", "negative", "neutral")
BACKEND_DEFAULTS = {
    "llm": ("https://open.bigmodel.cn/api/coding/paas/v4", "glm-5.3"),
    "jev": ("https://app.vllm-sr.ai/v1", "vllm-sr/d3-ultra"),
}
MAX_WEIGHT = 0.20


@dataclass(frozen=True)
class Price:
    day: date
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume_lots: int
    name: str = ""


@dataclass(frozen=True)
class TradingRule:
    symbol: str
    buy_lot: int
    sell_lot: int
    tick_size: float
    commission_rate: float
    min_commission: float
    transfer_fee_rate: float
    stamp_duty_rate: float
    slippage_rate: float
    price_limit_rate: float
    max_participation_rate: float


@dataclass(frozen=True)
class News:
    news_id: str
    symbol: str
    published_at: datetime
    title: str
    body: str
    source_url: str = ""


def read_prices(path):
    prices = {}
    with path.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(line for line in stream if not line.startswith("#")):
            price = Price(date.fromisoformat(row["date"]), row["symbol"].strip().upper(),
                          float(row["open"]), float(row["high"]), float(row["low"]),
                          float(row["close"]), int(row["volume_lots"]), row.get("name", "").strip())
            if (not all(math.isfinite(value) and value > 0 for value in
                        (price.open, price.high, price.low, price.close))
                    or price.low > min(price.open, price.close)
                    or price.high < max(price.open, price.close) or price.volume_lots < 0):
                raise ValueError(f"Invalid OHLC prices: {price.day} {price.symbol}")
            key = (price.day, price.symbol)
            if key in prices:
                raise ValueError(f"Duplicate price: {key}")
            prices[key] = price
    return prices


def read_rules(path):
    rules = {}
    with path.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(line for line in stream if not line.startswith("#")):
            rule = TradingRule(row["symbol"].strip().upper(), int(row["buy_lot"]),
                               int(row["sell_lot"]), float(row["tick_size"]),
                               float(row["commission_rate"]), float(row["min_commission"]),
                               float(row["transfer_fee_rate"]), float(row["stamp_duty_rate"]),
                               float(row["slippage_rate"]), float(row["price_limit_rate"]),
                               float(row["max_participation_rate"]))
            values = (rule.tick_size, rule.commission_rate, rule.min_commission,
                      rule.transfer_fee_rate, rule.stamp_duty_rate, rule.slippage_rate,
                      rule.price_limit_rate, rule.max_participation_rate)
            if (not rule.symbol or rule.symbol in rules or rule.buy_lot <= 0 or rule.sell_lot <= 0
                    or not all(math.isfinite(value) and value >= 0 for value in values)
                    or rule.tick_size <= 0 or rule.price_limit_rate >= 1
                    or rule.max_participation_rate > 1):
                raise ValueError(f"Invalid trading rule: {rule.symbol}")
            rules[rule.symbol] = rule
    return rules


def rounded_price(value, tick_size):
    tick = Decimal(str(tick_size))
    return float((Decimal(str(value)) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick)


def cents(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def transaction_cost(quantity, price, rule, action):
    value = quantity * price
    commission = cents(max(value * rule.commission_rate, rule.min_commission))
    transfer_fee = cents(value * rule.transfer_fee_rate)
    stamp_duty = cents(value * rule.stamp_duty_rate) if action == "SELL" else 0.0
    return commission, transfer_fee, stamp_duty


def read_news(path):
    news = []
    seen = set()
    with path.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(line for line in stream if not line.startswith("#")):
            item = News(row["news_id"].strip(), row["symbol"].strip().upper(),
                        datetime.fromisoformat(row["published_at"]), row["title"].strip(),
                        row["body"].strip(), row.get("source_url", "").strip())
            if not item.news_id or item.news_id in seen:
                raise ValueError(f"Missing or duplicate news_id: {item.news_id}")
            seen.add(item.news_id)
            news.append(item)
    return news


def cache_key(item, model, endpoint=None, backend="llm"):
    endpoint = endpoint or BACKEND_DEFAULTS[backend][0]
    content = json.dumps([backend, endpoint, model, CONDITION, item.symbol, item.title, item.body], ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def classify_news(items, model, cache_path, endpoint=None, backend="llm", force=False):
    endpoint = endpoint or BACKEND_DEFAULTS[backend][0]
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    decisions = {}
    missing = [item for item in items if force or cache_key(item, model, endpoint, backend) not in cache]
    if missing:
        import jevdb

        key_name = "OPENAI_API_KEY" if backend == "llm" else "JEV_API_KEY"
        if not os.environ.get(key_name):
            os.environ[key_name] = getpass(f"{key_name}: ").strip()
        if not os.environ[key_name]:
            raise ValueError(f"{key_name} is required for new decisions")
        con = jevdb.connect()
        try:
            secret = "llm (TYPE openai" if backend == "llm" else "jev (TYPE jev"
            con.execute(f"CREATE SECRET {secret}, PROVIDER env)")
            con.execute("SET jevdb_backend=?", [backend])
            con.execute("SET jevdb_endpoint=?", [endpoint])
            con.execute("SET jevdb_model=?", [model])
            con.execute("CREATE TABLE news (news_id VARCHAR, symbol VARCHAR, title VARCHAR, body VARCHAR)")
            con.executemany("INSERT INTO news VALUES (?, ?, ?, ?)",
                            [(item.news_id, item.symbol, item.title, item.body) for item in missing])
            condition = CONDITION.replace("'", "''")
            results = con.execute(f"""
                SELECT news_id,
                       jev_choose('{condition}',
                                  {{'symbol': symbol, 'title': title, 'body': body}},
                                  ['positive', 'negative', 'neutral']) AS impact
                FROM news
            """).fetchall()
            decisions.update(results)
            if len(decisions) != len(missing) or any(label not in LABELS for label in decisions.values()):
                raise ValueError("JEVDB returned incomplete or invalid classifications")
            for item in missing:
                cache[cache_key(item, model, endpoint, backend)] = decisions[item.news_id]
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        finally:
            con.close()
    decisions.update({item.news_id: cache[cache_key(item, model, endpoint, backend)]
                      for item in items if item.news_id not in decisions})
    if any(label not in LABELS for label in decisions.values()):
        raise ValueError("Decision cache contains an invalid classification")
    return decisions, len(missing)


def schedule_signals(news, decisions, prices, start, end):
    sessions = {}
    for day, symbol in prices:
        if start <= day <= end:
            sessions.setdefault(symbol, set()).add(day)
    sessions = {symbol: sorted(days) for symbol, days in sessions.items()}
    scheduled = {}
    for item in news:
        if item.published_at.date() < start or item.published_at.date() > end:
            continue
        execution_day = next((day for day in sessions.get(item.symbol, [])
                              if day > item.published_at.date()), None)
        if execution_day is not None:
            scheduled.setdefault((execution_day, item.symbol), []).append(item)
    signals = []
    for (day, symbol), items in sorted(scheduled.items()):
        labels = {decisions[item.news_id] for item in items}
        action = "BUY" if labels == {"positive"} else "SELL" if labels == {"negative"} else "HOLD"
        signals.append({"day": day, "symbol": symbol, "action": action,
                        "news_ids": ",".join(item.news_id for item in items),
                        "reason": "conflicting/neutral news" if action == "HOLD" else "news classification"})
    return signals


def simulate(prices, signals, start, end, capital, rules):
    days = sorted({day for day, _ in prices if start <= day <= end})
    if not days:
        raise ValueError("No prices in the requested interval")
    symbols = sorted({symbol for _, symbol in prices})
    if set(symbols) - rules.keys():
        raise ValueError(f"Missing trading rules: {', '.join(sorted(set(symbols) - rules.keys()))}")
    for day in days:
        missing = [symbol for symbol in symbols if (day, symbol) not in prices]
        if missing:
            raise ValueError(f"Missing prices on {day}: {', '.join(missing)}")
    cash = capital
    holdings = {symbol: 0 for symbol in symbols}
    last_buy = {}
    previous_close = {}
    for symbol in symbols:
        earlier = [day for day, name in prices if name == symbol and day < days[0]]
        if earlier:
            previous_close[symbol] = prices[(max(earlier), symbol)].close
    signals_by_day = {}
    for signal in signals:
        signals_by_day.setdefault(signal["day"], []).append(signal)
    orders = []
    equity_curve = []
    for day in days:
        day_signals = sorted(signals_by_day.get(day, []),
                             key=lambda signal: (signal["action"] != "SELL", signal["symbol"]))
        for signal in day_signals:
            symbol = signal["symbol"]
            action = signal["action"]
            bar = prices[(day, symbol)]
            rule = rules[symbol]
            price = rounded_price(bar.open * (1 + rule.slippage_rate if action == "BUY"
                                               else 1 - rule.slippage_rate), rule.tick_size)
            quantity = 0
            reason = signal["reason"]
            commission = transfer_fee = stamp_duty = 0.0
            prior = previous_close.get(symbol)
            if action == "HOLD":
                pass
            elif prior is not None and (action == "BUY" and (bar.open >= rounded_price(
                    prior * (1 + rule.price_limit_rate), rule.tick_size)
                    or price > rounded_price(prior * (1 + rule.price_limit_rate), rule.tick_size))
                    or action == "SELL" and (bar.open <= rounded_price(
                        prior * (1 - rule.price_limit_rate), rule.tick_size)
                        or price < rounded_price(prior * (1 - rule.price_limit_rate), rule.tick_size))):
                reason = "price limit"
            elif price > bar.high or price < bar.low:
                reason = "slippage price outside daily range"
            elif action == "SELL":
                available = holdings[symbol] if last_buy.get(symbol) != day else 0
                volume_cap = int(bar.volume_lots * 100 * rule.max_participation_rate)
                quantity = min(available, volume_cap) // rule.sell_lot * rule.sell_lot
                if quantity:
                    commission, transfer_fee, stamp_duty = transaction_cost(quantity, price, rule, action)
                    cash = cents(cash + quantity * price - commission - transfer_fee - stamp_duty)
                    holdings[symbol] -= quantity
                else:
                    reason = ("no sellable position (T+1 or lot size)" if not available
                              else "daily volume participation limit")
            elif action == "BUY":
                equity_at_open = cash + sum(holdings[name] * prices[(day, name)].open for name in symbols)
                remaining_weight = max(0, MAX_WEIGHT * equity_at_open - holdings[symbol] * price)
                budget = min(cash, remaining_weight,
                             int(bar.volume_lots * 100 * rule.max_participation_rate)
                             // rule.buy_lot * rule.buy_lot * price)
                quantity = int(budget / price // rule.buy_lot) * rule.buy_lot
                while quantity:
                    commission, transfer_fee, stamp_duty = transaction_cost(quantity, price, rule, action)
                    if cents(quantity * price + commission + transfer_fee) <= cash + 1e-8 and quantity * price <= remaining_weight + 1e-8:
                        break
                    quantity -= rule.buy_lot
                if quantity:
                    cash = cents(cash - quantity * price - commission - transfer_fee)
                    holdings[symbol] += quantity
                    last_buy[symbol] = day
                else:
                    reason = ("daily volume participation limit" if budget < rule.buy_lot * price
                              and cash >= rule.buy_lot * price and remaining_weight >= rule.buy_lot * price
                              else "insufficient cash or position limit")
                    commission = transfer_fee = stamp_duty = 0.0
            orders.append({**signal, "price": price if action != "HOLD" else "",
                           "quantity": quantity, "status": "FILLED" if quantity else "SKIPPED",
                           "reason": reason, "commission": commission,
                           "transfer_fee": transfer_fee, "stamp_duty": stamp_duty,
                           "cash_after": cents(cash)})
        equity = cash + sum(holdings[symbol] * prices[(day, symbol)].close for symbol in symbols)
        equity_curve.append({"day": day, "cash": cents(cash), "equity": cents(equity)})
        previous_close.update({symbol: prices[(day, symbol)].close for symbol in symbols})
    peak = capital
    max_drawdown = 0.0
    for row in equity_curve:
        peak = max(peak, row["equity"])
        max_drawdown = max(max_drawdown, 1 - row["equity"] / peak)
    return orders, equity_curve, holdings, (equity_curve[-1]["equity"] / capital - 1), max_drawdown


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def load_experiment(symbols, start, end, prices_path, news_path, rules_path):
    prices = {key: price for key, price in read_prices(prices_path).items() if key[1] in symbols}
    rules = {symbol: rule for symbol, rule in read_rules(rules_path).items() if symbol in symbols}
    news = [item for item in read_news(news_path) if item.symbol in symbols
            and start <= item.published_at.date() <= end]
    if not prices or not news or symbols - {symbol for _, symbol in prices} or symbols - rules.keys():
        raise ValueError("所选区间须同时包含行情、新闻和每只股票的交易规则")
    days = sorted({day for day, _ in prices if start <= day <= end})
    if not days:
        raise ValueError("所选区间没有行情")
    for day in days:
        if any((day, symbol) not in prices for symbol in symbols):
            raise ValueError(f"{day} 缺少所选股票的行情")
    return prices, news, rules, days


def run_experiment(prices, news, rules, decisions, start, end, capital, output, backend, endpoint, model):
    decision_rows = [{"news_id": item.news_id, "symbol": item.symbol,
                      "published_at": item.published_at, "title": item.title,
                      "impact": decisions[item.news_id]} for item in news]
    signals = schedule_signals(news, decisions, prices, start, end)
    orders, curve, holdings, total_return, drawdown = simulate(
        prices, signals, start, end, capital, rules)
    write_csv(output / "decisions.csv", decision_rows, list(decision_rows[0]))
    write_csv(output / "orders.csv", orders, ["day", "symbol", "action", "news_ids", "reason",
                                               "price", "quantity", "status", "commission",
                                               "transfer_fee", "stamp_duty", "cash_after"])
    write_csv(output / "equity.csv", curve, ["day", "cash", "equity"])
    record = {"symbols": sorted(rules), "start": start.isoformat(), "end": end.isoformat(),
              "capital": capital, "backend": backend, "endpoint": endpoint, "model": model,
              "rules": {symbol: asdict(rule) for symbol, rule in rules.items()},
              "decisions": decisions}
    (output / "experiment.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return orders, curve, holdings, total_return, drawdown


def prompt_choice(message, choices, default=None):
    while True:
        answer = input(message).strip()
        if not answer and default is not None:
            return default
        if answer in choices:
            return answer
        print(f"请输入：{' / '.join(choices)}")


def prompt_value(message, default, parse, valid):
    while True:
        answer = input(f"{message} [{default}]：").strip()
        try:
            value = parse(answer) if answer else default
            if valid(value):
                return value
        except ValueError:
            pass
        print("输入无效，请重试。")


def show_data(news, prices, rules, days):
    print(f"已加载 {len(news)} 条公告、{len(days)} 个交易日、{len(prices)} 条行情、{len(rules)} 组交易规则。")
    for item in news:
        print(f"  {item.published_at.date()} {item.symbol} {item.news_id} {item.title}")
    for symbol, rule in sorted(rules.items()):
        print(f"  {symbol}：买入单位 {rule.buy_lot} 股，佣金 {rule.commission_rate:.3%}"
              f"（最低 {rule.min_commission:.2f} 元），印花税 {rule.stamp_duty_rate:.3%}，"
              f"滑点 {rule.slippage_rate:.3%}，成交量参与率 {rule.max_participation_rate:.1%}")


def show_news_details(news, decisions=None):
    for item in news:
        label = f" | JEVDB: {decisions[item.news_id]}" if decisions else ""
        print(f"\n{item.news_id} {item.symbol} {item.title}{label}")
        print(f"  摘要：{item.body}")
        print(f"  来源：{item.source_url or '未提供'}")
        print(f"  披露日期：{item.published_at.date()}（样本 00:00:00 不是实际发布时间）")


def interactive_session(args, all_news, all_prices, all_rules):
    round_number = 1
    while True:
        print(f"\n第 {round_number} 轮实验（独立回测，从初始资金和空仓开始）")
        if not interactive_round(args, all_news, all_prices, all_rules):
            return
        round_number += 1


def interactive_round(args, all_news, all_prices, all_rules):
    available = sorted({item.symbol for item in all_news} &
                       {symbol for _, symbol in all_prices} & set(all_rules))
    if not available:
        raise ValueError("输入文件中没有同时具备新闻、行情和交易规则的股票")
    print("JEVDB 股票新闻模拟交易实验台（不发送真实订单）")
    names = {bar.symbol: bar.name for bar in all_prices.values() if bar.name}
    print("可选股票：" + "、".join(f"{symbol}（{names.get(symbol, '名称未提供')}）" for symbol in available))
    while True:
        raw = input("选择股票代码，逗号分隔 [全部]：").strip()
        symbols = {part.strip().upper() for part in raw.split(",") if part.strip()} if raw else set(available)
        if symbols and symbols <= set(available):
            break
        print("股票代码无效，请从列表中选择。")
    common_days = set.intersection(*({day for day, name in all_prices if name == symbol}
                                     for symbol in symbols))
    available_days = sorted(common_days)
    if len(available_days) < 2:
        print("所选股票没有足够的共同交易日，请重新选择。")
        return True
    print(f"可用行情范围：{available_days[0]} 至 {available_days[-1]}；日期格式 YYYY-MM-DD。")
    while True:
        start = prompt_value(f"开始日期（{available_days[0]} 至 {available_days[-2]}）",
                             available_days[0], date.fromisoformat,
                             lambda value: available_days[0] <= value < available_days[-1])
        end = prompt_value(f"结束日期（须晚于 {start}，最晚 {available_days[-1]}）",
                           available_days[-1], date.fromisoformat,
                           lambda value: start < value <= available_days[-1])
        try:
            prices, news, rules, days = load_experiment(
                symbols, start, end, args.prices, args.news, args.rules)
            break
        except ValueError as error:
            print(f"该区间无法回测：{error}。请重新选择日期。")
            print("所选股票的新闻日期：" + "、".join(sorted({str(item.published_at.date())
                  for item in all_news if item.symbol in symbols})))
    capital = prompt_value("初始资金（元）", args.capital, float,
                           lambda value: math.isfinite(value) and value > 0)
    backend = prompt_choice("模型后端：1 OpenAI 兼容 / 2 Jev [1]：", ("1", "2"), "1")
    backend = "llm" if backend == "1" else "jev"
    default_endpoint, default_model = BACKEND_DEFAULTS[backend]
    endpoint = prompt_value("服务地址", args.endpoint or default_endpoint, str,
                            lambda value: value.startswith(("https://", "http://")))
    model = prompt_value("模型名称", args.model or default_model, str, lambda value: bool(value.strip()))
    print(f"需求：{', '.join(sorted(symbols))}，{start} 至 {end}，初始资金 {capital:,.2f}")
    show_data(news, prices, rules, days)
    while True:
        choice = prompt_choice("准备阶段：1 查看新闻 / 2 调整参数 / 3 调用 JEVDB / 0 退出：",
                               ("1", "2", "3", "0"))
        if choice == "0":
            return
        if choice == "1":
            show_news_details(news)
        elif choice == "2":
            capital = prompt_value("初始资金（元）", capital, float,
                                   lambda value: math.isfinite(value) and value > 0)
            for symbol in sorted(rules):
                rule = rules[symbol]
                commission = prompt_value(f"{symbol} 佣金率", rule.commission_rate, float,
                                          lambda value: math.isfinite(value) and 0 <= value < 1)
                slippage = prompt_value(f"{symbol} 滑点率", rule.slippage_rate, float,
                                        lambda value: math.isfinite(value) and 0 <= value < 1)
                rules[symbol] = replace(rule, commission_rate=commission, slippage_rate=slippage)
            show_data(news, prices, rules, days)
        else:
            break
    cache_path = args.output / "decision_cache.json"
    decisions = {}
    while True:
        print(f"工具调用：JEVDB jev_choose，后端 {backend}，模型 {model}")
        refresh = bool(decisions)
        for index, item in enumerate(news, 1):
            result, calls = classify_news([item], model, cache_path, endpoint, backend,
                                          force=refresh)
            decisions.update(result)
            print(f"  [{index}/{len(news)}] {item.symbol} {item.news_id} → {result[item.news_id]}"
                  f"（{'新调用' if calls else '缓存'}）")
        choice = prompt_choice("判断已完成：1 查看依据 / 2 重新调用模型 / 3 开始模拟 / 0 退出：",
                               ("1", "2", "3", "0"))
        while choice == "1":
            show_news_details(news, decisions)
            choice = prompt_choice("1 再看依据 / 2 重新调用模型 / 3 开始模拟 / 0 退出：",
                                   ("1", "2", "3", "0"))
        if choice == "0":
            return
        if choice == "3":
            break
    while True:
        run_output = args.output / (datetime.now().strftime("run_%Y%m%d_%H%M%S_") + uuid4().hex[:8])
        orders, curve, holdings, total_return, drawdown = run_experiment(
            prices, news, rules, decisions, start, end, capital, run_output, backend, endpoint, model)
        print(f"模拟完成：期末资产 {curve[-1]['equity']:,.2f}；收益率 {total_return:.2%}；"
              f"最大回撤 {drawdown:.2%}；持仓 {holdings}")
        choice = prompt_choice("结果：1 订单 / 2 每日资产 / 3 修改资金或费用重跑 / 4 文件位置 / 5 开始新一轮（重选股票和日期） / 0 退出：",
                               ("1", "2", "3", "4", "5", "0"))
        while choice in ("1", "2", "4"):
            if choice == "1":
                for order in orders:
                    print(f"  {order['day']} {order['symbol']} {order['action']} {order['status']} "
                          f"{order['quantity']} 股 @ {order['price']}；原因：{order['reason']}；"
                          f"佣金 {order['commission']}，过户费 {order['transfer_fee']}，印花税 {order['stamp_duty']}")
            elif choice == "2":
                for point in curve:
                    print(f"  {point['day']} 现金 {point['cash']:,.2f}，总资产 {point['equity']:,.2f}")
            else:
                print(f"结果文件：{run_output.resolve()}")
            choice = prompt_choice("结果：1 订单 / 2 每日资产 / 3 修改资金或费用重跑 / 4 文件位置 / 5 开始新一轮（重选股票和日期） / 0 退出：",
                                   ("1", "2", "3", "4", "5", "0"))
        if choice == "5":
            print(f"本轮结果已保存：{run_output.resolve()}")
            return True
        if choice == "0":
            print(f"结果文件：{run_output.resolve()}")
            return
        capital = prompt_value("初始资金（元）", capital, float,
                               lambda value: math.isfinite(value) and value > 0)
        for symbol in sorted(rules):
            rule = rules[symbol]
            commission = prompt_value(f"{symbol} 佣金率", rule.commission_rate, float,
                                      lambda value: math.isfinite(value) and 0 <= value < 1)
            slippage = prompt_value(f"{symbol} 滑点率", rule.slippage_rate, float,
                                    lambda value: math.isfinite(value) and 0 <= value < 1)
            rules[symbol] = replace(rule, commission_rate=commission, slippage_rate=slippage)


def main():
    parser = argparse.ArgumentParser(description="JEVDB news-driven paper trading (no real orders)")
    parser.add_argument("--symbols", help="Comma-separated stock symbols")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--interactive", action="store_true", help="Start guided experiment")
    parser.add_argument("--capital", type=float, default=100000)
    parser.add_argument("--backend", choices=tuple(BACKEND_DEFAULTS), default="llm")
    parser.add_argument("--model", help="Override the model for the selected backend")
    parser.add_argument("--endpoint", help="Override the endpoint for the selected backend")
    parser.add_argument("--prices", type=Path, default=Path(__file__).with_name("prices.csv"))
    parser.add_argument("--news", type=Path, default=Path(__file__).with_name("news.csv"))
    parser.add_argument("--rules", type=Path, default=Path(__file__).with_name("trading_rules.csv"))
    parser.add_argument("--output", type=Path, default=Path(".tmp/stock_trading"))
    args = parser.parse_args()
    if args.interactive or (args.symbols is None and args.start is None and args.end is None):
        if not math.isfinite(args.capital) or args.capital <= 0:
            parser.error("Capital must be positive and finite")
        try:
            interactive_session(args, read_news(args.news), read_prices(args.prices), read_rules(args.rules))
        except (KeyboardInterrupt, EOFError):
            print("\n已退出，未继续执行。")
        return
    if not all((args.symbols, args.start, args.end)):
        parser.error("Batch mode requires --symbols, --start and --end together")
    endpoint, model = BACKEND_DEFAULTS[args.backend]
    endpoint = args.endpoint or endpoint
    model = args.model or model
    if (args.start >= args.end or not math.isfinite(args.capital) or args.capital <= 0
            or not model.strip() or not endpoint.startswith(("https://", "http://"))):
        parser.error("Start must precede end; capital, model and endpoint must be valid")
    selected = {symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()}
    if not selected:
        parser.error("Select at least one symbol")
    prices, news, rules, _ = load_experiment(
        selected, args.start, args.end, args.prices, args.news, args.rules)
    print(f"需求：{', '.join(sorted(selected))}，{args.start} 至 {args.end}，初始资金 {args.capital:,.2f}")
    decisions, calls = classify_news(news, model, args.output / "decision_cache.json", endpoint, args.backend)
    print(f"语义工具：JEVDB jev_choose；本次新判断 {calls} 条，缓存复用 {len(news) - calls} 条")
    orders, curve, holdings, total_return, drawdown = run_experiment(
        prices, news, rules, decisions, args.start, args.end, args.capital,
        args.output, args.backend, endpoint, model)
    print("结果展示：")
    for item in news:
        print(f"  新闻 {item.news_id} {item.symbol}: {decisions[item.news_id]} | {item.title}")
    for row in orders:
        print(f"  {row['day']} {row['symbol']} {row['action']} {row['status']} "
              f"{row['quantity']} 股 @ {row['price']}，原因：{row['reason']}")
    print(f"期末资产 {curve[-1]['equity']:,.2f}；收益率 {total_return:.2%}；最大回撤 {drawdown:.2%}；持仓 {holdings}")
    print(f"明细文件：{args.output.resolve()}")


if __name__ == "__main__":
    main()
