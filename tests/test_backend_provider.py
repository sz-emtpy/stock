from __future__ import annotations

import unittest

from backend.provider import MarketDataError, parse_klines, parse_markdown_table, _to_float
from chanpicker.core import Bar


KLINE_MARKDOWN = """
| date | open | last | high | low | volume | amount | exchange | change_pct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-15 | 1286 | 1309.27 | 1317.76 | 1282 | 28484 | 3717518500 | 0.23 | 1.8094867807 |
| 2026-10-14 | 1270.1 | 1286 | 1286.75 | 1266 | 23773 | 3045610000 | 0.19 | 0.8714477327 |
| 2026-10-13 | 1287 | 1274.89 | 1288.48 | 1268.11 | 26573 | 3393320000 | 0.21 | -1.0178571429 |
"""


class ProviderParseTest(unittest.TestCase):
    def test_parse_markdown_table_skips_noise_lines(self) -> None:
        text = "📈 标题行\n| code | name |\n| --- | --- |\n| sh600519 | 贵州茅台 |\n说明文字"
        rows = parse_markdown_table(text)
        self.assertEqual(rows, [{"code": "sh600519", "name": "贵州茅台"}])

    def test_parse_klines_uses_last_as_close(self) -> None:
        bars = parse_klines(KLINE_MARKDOWN, "sh600519")
        self.assertEqual(len(bars), 3)
        self.assertEqual(bars[-1].date, "2026-10-15")
        self.assertEqual(bars[-1].close, 1309.27)
        self.assertEqual(bars[-1].open, 1286)
        self.assertEqual(bars[-1].high, 1317.76)
        self.assertEqual(bars[-1].volume, 28484)
        self.assertTrue(all(bar.high >= bar.low for bar in bars))

    def test_parse_klines_rejects_garbage(self) -> None:
        with self.assertRaises(MarketDataError):
            parse_klines("没有表格的输出", "sh600519")

    def test_to_float_tolerates_markup(self) -> None:
        self.assertEqual(_to_float("1,286.00"), 1286.0)
        self.assertEqual(_to_float("--"), 0.0)
        self.assertEqual(_to_float("3.5%"), 3.5)


class BarModelTest(unittest.TestCase):
    def test_bar_validates_ohlc_range(self) -> None:
        with self.assertRaises(ValueError):
            Bar(symbol="x", date="2026-01-01", open=10, high=9, low=8, close=9)


if __name__ == "__main__":
    unittest.main()
