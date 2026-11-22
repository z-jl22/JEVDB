import json
import contextlib
from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trade import (Price, cache_key, classify_news, main, read_news, read_prices,
                   read_rules, schedule_signals, simulate)


ROOT = Path(__file__).parent
START = date(2025, 4, 24)
END = date(2025, 5, 15)
LABELS = {"WLY-20250426-Q1": "positive", "PA-20250426-Q1": "positive"}


class TradingTest(unittest.TestCase):
    def setUp(self):
        self.prices = read_prices(ROOT / "prices.csv")
        self.news = read_news(ROOT / "news.csv")
        self.rules = read_rules(ROOT / "trading_rules.csv")

    def test_next_session_and_order_checks(self):
        signals = schedule_signals(self.news, LABELS, self.prices, START, END)
        self.assertEqual([(row["day"], row["symbol"], row["action"]) for row in signals], [
            (date(2025, 4, 28), "000858", "BUY"),
            (date(2025, 4, 28), "601318", "BUY"),
        ])
        orders, curve, holdings, total_return, drawdown = simulate(
            self.prices, signals, START, END, 100000, self.rules)
        self.assertEqual([row["quantity"] for row in orders], [100, 300])
        self.assertEqual([row["status"] for row in orders], ["FILLED", "FILLED"])
        self.assertEqual(holdings, {"000858": 100, "601318": 300})
        self.assertEqual(len(curve), 13)
        self.assertAlmostEqual(total_return, curve[-1]["equity"] / 100000 - 1)
        self.assertGreaterEqual(drawdown, 0)
        self.assertEqual(orders[0]["commission"], 5.0)
        self.assertEqual(orders[0]["stamp_duty"], 0.0)

    def test_sell_costs_and_t_plus_one(self):
        buy = {"day": date(2025, 4, 28), "symbol": "000858", "action": "BUY",
               "news_ids": "test", "reason": "test"}
        sell = dict(buy, day=date(2025, 4, 29), action="SELL")
        orders, _, holdings, _, _ = simulate(
            self.prices, [buy, sell], START, END, 100000, self.rules)
        self.assertEqual([order["quantity"] for order in orders], [100, 100])
        self.assertEqual(orders[1]["commission"], 5.0)
        self.assertGreater(orders[1]["stamp_duty"], 0)
        self.assertEqual(holdings["000858"], 0)
        same_day = dict(sell, day=buy["day"])
        orders, _, _, _, _ = simulate(
            self.prices, [buy, same_day], START, END, 100000, self.rules)
        self.assertEqual(next(order for order in orders if order["action"] == "SELL")["status"], "SKIPPED")

    def test_price_range_and_limit_block_execution(self):
        buy = {"day": date(2025, 4, 28), "symbol": "000858", "action": "BUY",
               "news_ids": "test", "reason": "test"}
        prices = dict(self.prices)
        prices[(buy["day"], buy["symbol"])] = Price(
            buy["day"], buy["symbol"], 130.11, 130.11, 129.90, 130.11, 185045)
        orders, _, _, _, _ = simulate(prices, [buy], START, END, 100000, self.rules)
        self.assertEqual(orders[0]["reason"], "slippage price outside daily range")
        prices[(buy["day"], buy["symbol"])] = Price(
            buy["day"], buy["symbol"], 141.96, 141.96, 141.96, 141.96, 185045)
        orders, _, _, _, _ = simulate(prices, [buy], START, END, 100000, self.rules)
        self.assertEqual(orders[0]["reason"], "price limit")

    def test_invalid_ohlc_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "prices.csv"
            path.write_text("date,symbol,open,high,low,close,volume_lots\n"
                            "2025-04-28,000858,10,9,8,10,100\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_prices(path)

    def test_daily_volume_caps_buy(self):
        buy = {"day": date(2025, 4, 28), "symbol": "000858", "action": "BUY",
               "news_ids": "test", "reason": "test"}
        prices = dict(self.prices)
        bar = prices[(buy["day"], buy["symbol"])]
        prices[(buy["day"], buy["symbol"])] = Price(
            bar.day, bar.symbol, bar.open, bar.high, bar.low, bar.close, 50)
        orders, _, _, _, _ = simulate(prices, [buy], START, END, 100000, self.rules)
        self.assertEqual(orders[0]["reason"], "daily volume participation limit")

    def test_conflicting_news_does_not_trade(self):
        decisions = dict(LABELS, N6="negative")
        news = self.news + [self.news[0].__class__(
            "N6", "000858", self.news[0].published_at, "Conflicting report", "Unclear")]
        signals = schedule_signals(news, decisions, self.prices, START, END)
        self.assertEqual(next(row for row in signals if row["symbol"] == "000858")["action"], "HOLD")

    def test_cached_decisions_require_no_key_or_model_call(self):
        with TemporaryDirectory() as directory:
            cache_path = Path(directory) / "decisions.json"
            cache_path.write_text(json.dumps({cache_key(item, "test-model"): LABELS[item.news_id]
                                              for item in self.news}), encoding="utf-8")
            decisions, calls = classify_news(self.news, "test-model", cache_path)
            self.assertEqual(decisions, LABELS)
            self.assertEqual(calls, 0)

    def test_backends_have_separate_cache_keys(self):
        item = self.news[0]
        self.assertNotEqual(cache_key(item, "same-model", backend="llm"),
                            cache_key(item, "same-model", backend="jev"))

    def test_complete_cli_flow_with_cached_decisions(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "decision_cache.json").write_text(json.dumps({
                cache_key(item, "test-model"): LABELS[item.news_id] for item in self.news
            }), encoding="utf-8")
            arguments = ["trade.py", "--symbols", "000858,601318", "--start", "2025-04-24",
                         "--end", "2025-05-15", "--model", "test-model", "--output", str(output)]
            stdout = StringIO()
            with patch("sys.argv", arguments), contextlib.redirect_stdout(stdout):
                main()
            self.assertIn("语义工具：JEVDB jev_choose；本次新判断 0 条，缓存复用 2 条", stdout.getvalue())
            self.assertIn("2025-04-28 000858 BUY FILLED", stdout.getvalue())
            self.assertTrue((output / "decisions.csv").exists())
            self.assertTrue((output / "orders.csv").exists())
            self.assertTrue((output / "equity.csv").exists())
            self.assertTrue((output / "experiment.json").exists())

    def test_interactive_flow_reviews_decisions_and_reruns(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "decision_cache.json").write_text(json.dumps({
                cache_key(item, "test-model"): LABELS[item.news_id] for item in self.news
            }), encoding="utf-8")
            answers = ["", "", "", "", "", "", "", "3", "1", "3", "3",
                       "120000", "", "", "", "", "0"]
            arguments = ["trade.py", "--interactive", "--model", "test-model",
                         "--output", str(output)]
            stdout = StringIO()
            with (patch("sys.argv", arguments), patch("builtins.input", side_effect=answers),
                  contextlib.redirect_stdout(stdout)):
                main()
            self.assertIn("[1/2]", stdout.getvalue())
            self.assertIn("来源：", stdout.getvalue())
            records = [json.loads(path.read_text(encoding="utf-8"))
                       for path in output.glob("run_*/experiment.json")]
            self.assertEqual(sorted(record["capital"] for record in records), [100000, 120000])

    def test_interactive_multiple_rounds_and_date_recovery(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "decision_cache.json").write_text(json.dumps({
                cache_key(item, "test-model"): LABELS[item.news_id] for item in self.news
            }), encoding="utf-8")
            answers = iter(["", "2025-05-06", "", "", "", "", "", "", "",
                            "3", "3", "5",
                            "000858", "", "", "", "", "", "", "3", "3", "0"])
            prompts = []

            def respond(message):
                prompts.append(message)
                return next(answers)

            stdout = StringIO()
            with (patch("sys.argv", ["trade.py", "--model", "test-model", "--output", str(output)]),
                  patch("builtins.input", side_effect=respond), contextlib.redirect_stdout(stdout)):
                main()
            self.assertIn("000858（五粮液）", stdout.getvalue())
            self.assertIn("601318（中国平安）", stdout.getvalue())
            self.assertIn("第 2 轮实验", stdout.getvalue())
            self.assertIn("该区间无法回测", stdout.getvalue())
            self.assertTrue(any("开始日期（2025-04-24 至 2025-05-14）" in prompt for prompt in prompts))
            self.assertTrue(any("结束日期（须晚于" in prompt for prompt in prompts))
            records = [json.loads(path.read_text(encoding="utf-8"))
                       for path in output.glob("run_*/experiment.json")]
            self.assertEqual(sorted(len(record["symbols"]) for record in records), [1, 2])


if __name__ == "__main__":
    unittest.main()
