"""扫描范围测试：全市场证券池解析、scope 分派与非法值拦截。

覆盖两个真实风险：
1. 数据源对 `universe` 命令可能存在返回上限，若只看表格行数会把「只拿到前 N 只」
   当成全市场，扫描范围静默缩水 —— 因此必须拿标题行声明的总数做校验；
2. `scope` 从接口进来是任意字符串，必须收敛成白名单，非法值返回 400 而不是
   带着一个空清单默默跑完。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from backend.provider import (  # noqa: E402
    MarketDataError,
    apply_universe_filters,
    fetch_all_a_share_symbols,
    is_excluded_market,
    is_st_name,
    normalize_scope,
    parse_universe_rows,
    parse_universe_total,
)
from backend.service import ScanService  # noqa: E402
from backend.store import Store  # noqa: E402

UNIVERSE_SAMPLE = """
📈 A股股票（4 只）

| code | name |
| --- | --- |
| sz002259 | 升达林业 |
| sh600619 | 海立股份 |
| sh603156 | 养元饮品 |
| sh688168 | 安博通 |
"""

TRUNCATED_SAMPLE = """
📈 A股股票（4 只）

| code | name |
| --- | --- |
| sz002259 | 升达林业 |
"""

# 覆盖四种风险警示写法 + 北交所
FILTER_SAMPLE = """
📈 A股股票（8 只）

| code | name |
| --- | --- |
| sh600519 | 贵州茅台 |
| sz002259 | 升达林业 |
| sh600735 | ST新华锦 |
| sz000911 | *ST广糖 |
| sh600870 | SST华新 |
| sz000033 | S*ST前锋 |
| bj430047 | 诺思兰德 |
| bj920002 | *ST万达 |
"""


class ScopeNormalizeTest(unittest.TestCase):
    def test_default_is_all(self) -> None:
        self.assertEqual(normalize_scope(None), "all")
        self.assertEqual(normalize_scope(""), "all")

    def test_accepts_known_scopes(self) -> None:
        self.assertEqual(normalize_scope("all"), "all")
        self.assertEqual(normalize_scope("HS300"), "hs300")

    def test_rejects_unknown_scope(self) -> None:
        for bad in ("zz500", "all_a", "1"):
            with self.assertRaises(ValueError, msg=f"{bad} 应被拒绝"):
                normalize_scope(bad)


class UniverseParseTest(unittest.TestCase):
    def test_total_from_title(self) -> None:
        self.assertEqual(parse_universe_total(UNIVERSE_SAMPLE), 4)
        self.assertIsNone(parse_universe_total("| code | name |"))

    def test_rows_use_code_alias(self) -> None:
        rows = parse_universe_rows(UNIVERSE_SAMPLE)
        self.assertEqual(
            rows,
            [
                ("sz002259", "升达林业"),
                ("sh600619", "海立股份"),
                ("sh603156", "养元饮品"),
                ("sh688168", "安博通"),
            ],
        )

    def test_rows_skip_blank_symbol(self) -> None:
        rows = parse_universe_rows("| code | name |\n| --- | --- |\n|  | 无名 |\n| sh600000 | 浦发银行 |\n")
        self.assertEqual(rows, [("sh600000", "浦发银行")])

    def test_missing_symbol_column_raises(self) -> None:
        with self.assertRaises(MarketDataError):
            parse_universe_rows("| ticker | label |\n| --- | --- |\n| sh600000 | 浦发银行 |\n")


class ExclusionRuleTest(unittest.TestCase):
    """剔除口径：北交所 + 风险警示股（ST / *ST / SST / S*ST）。"""

    def test_st_name_variants(self) -> None:
        for name in ("ST新华锦", "*ST广糖", "SST华新", "S*ST前锋", "ST", "*ST"):
            self.assertTrue(is_st_name(name), f"{name} 应识别为风险警示股")
        for name in ("贵州茅台", "升达林业", "诺思兰德", "中国ST", ""):
            self.assertFalse(is_st_name(name), f"{name} 不应识别为风险警示股")

    def test_bj_prefix_excluded(self) -> None:
        self.assertTrue(is_excluded_market("bj430047"))
        self.assertTrue(is_excluded_market("BJ920002"))
        self.assertFalse(is_excluded_market("sh600519"))
        self.assertFalse(is_excluded_market("sz000911"))

    def test_filter_keeps_only_qualified(self) -> None:
        rows = parse_universe_rows(FILTER_SAMPLE)
        kept, stats = apply_universe_filters(rows)
        self.assertEqual([s for s, _ in kept], ["sh600519", "sz002259"], "只剩非北交所、非 ST 的两只")
        # bj920002 同时是北交所和 ST，只计一次（北交所规则优先）
        self.assertEqual(stats, {"total": 8, "excluded_bj": 2, "excluded_st": 4, "kept": 2})

    def test_filter_can_be_disabled(self) -> None:
        rows = parse_universe_rows(FILTER_SAMPLE)
        kept, stats = apply_universe_filters(rows, exclude_bj=False, exclude_st=False)
        self.assertEqual(len(kept), 8)
        self.assertEqual(stats["kept"], 8)


class FetchAllAShareTest(unittest.TestCase):
    def setUp(self) -> None:
        # 不真调数据源：替换掉 CLI 调用入口，只验证解析与完整性校验
        import backend.provider as provider

        self.provider = provider
        self.origin = provider.run_westock
        provider.run_westock = lambda args, timeout=30: UNIVERSE_SAMPLE  # type: ignore[assignment]
        self.addCleanup(setattr, provider, "run_westock", self.origin)

    def test_returns_full_list(self) -> None:
        universe, stats = fetch_all_a_share_symbols()
        self.assertEqual(len(universe), 4)
        self.assertEqual(universe[0], ("sz002259", "升达林业"))
        self.assertEqual(stats["kept"], 4)
        self.assertEqual(stats["excluded_bj"], 0)
        self.assertEqual(stats["excluded_st"], 0)

    def test_declared_total_mismatch_raises(self) -> None:
        # 声明 4 只却只解析到 1 只 —— 说明被上限截断，必须报错而不是静默缩小范围
        self.provider.run_westock = lambda args, timeout=30: TRUNCATED_SAMPLE  # type: ignore[assignment]
        with self.assertRaises(MarketDataError) as ctx:
            fetch_all_a_share_symbols()
        self.assertIn("不完整", str(ctx.exception))


class ScanScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "test.db")
        self.service = ScanService(self.store)

    def test_invalid_scope_is_rejected_before_any_fetch(self) -> None:
        with self.assertRaises(ValueError):
            self.service.start_market_scan(scope="zz500")

    def test_scope_defaults_to_all(self) -> None:
        import backend.service as service_mod

        origin_universe = service_mod.fetch_universe_symbols
        origin_run = ScanService._run_market_scan
        seen: list[str] = []

        def fake_universe(scope="all", exclude_bj=True, exclude_st=True):
            seen.append(scope)
            return [("sh600000", "浦发银行")], {"total": 1, "excluded_bj": 0, "excluded_st": 0, "kept": 1}

        # 掐掉后台扫描，避免单测真的去调数据源
        service_mod.fetch_universe_symbols = fake_universe  # type: ignore[assignment]
        ScanService._run_market_scan = lambda *args, **kwargs: None  # type: ignore[assignment]
        self.addCleanup(setattr, service_mod, "fetch_universe_symbols", origin_universe)
        self.addCleanup(setattr, ScanService, "_run_market_scan", origin_run)

        started = self.service.start_market_scan()
        self.assertEqual(seen, ["all"], "默认范围必须是全市场")
        self.assertEqual(started["scope"], "all")
        self.assertEqual(started["scope_label"], "全部A股")
        self.assertEqual(started["total"], 1)


if __name__ == "__main__":
    unittest.main()
