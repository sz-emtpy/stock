"""前端结构契约测试（零依赖，纯文本断言）。

覆盖一个真实缺陷：`nav` 里的默认按钮带了 `active`，但对应的 `<section>` 没带，
而 CSS 是 `section.tab { display: none }`，于是页面打开后四个区块全部隐藏、
主区域一片空白（数据其实已经请求回来了），必须手动点一次页签才显示。

约束（任一被破坏都会让默认页变白）：
1. 导航按钮的 data-tab 集合 == 区块 id 去掉 `tab-` 前缀的集合；
2. 恰好一个导航按钮带 active，恰好一个区块带 active；
3. 两者指向同一个页签；
4. CSS 对未激活区块仍是隐藏的（否则第 2、3 条就失去意义）。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parent.parent / "frontend" / "index.html"


class FrontendTabContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    @staticmethod
    def _class_value(attrs: str) -> str:
        """从 `<button data-tab="x" class="a b">` 的剩余属性里取出 class 的值。

        注意不能直接 `attrs.split()` 再判 `'active' in ...`：那样拿到的是
        `class="active"` 这种整体令牌，永远匹配不上，断言会假通过/假失败。
        """
        matched = re.search(r'class\s*=\s*"([^"]*)"', attrs or "")
        return matched.group(1) if matched else ""

    @staticmethod
    def _has_active(class_value: str) -> bool:
        return "active" in (class_value or "").split()

    def nav_buttons(self) -> list[tuple[str, bool]]:
        return [
            (m.group("tab"), self._has_active(self._class_value(m.group("attrs"))))
            for m in re.finditer(
                r'<button\s+data-tab="(?P<tab>[^"]+)"(?P<attrs>[^>]*)>', self.html
            )
        ]

    def sections(self) -> list[tuple[str, bool]]:
        return [
            (m.group("name"), self._has_active(m.group("cls")))
            for m in re.finditer(
                r'<section\s+class="(?P<cls>[^"]*)"\s+id="tab-(?P<name>[^"]+)"', self.html
            )
        ]

    def test_index_exists(self) -> None:
        self.assertTrue(INDEX_HTML.exists(), f"缺少前端文件: {INDEX_HTML}")

    def test_css_hides_inactive_sections(self) -> None:
        """前提：未激活区块是隐藏的。若这条变了，下面的断言意义也随之变化。"""

        self.assertRegex(self.html, r"section\.tab\s*\{\s*display:\s*none;?\s*\}")
        self.assertRegex(self.html, r"section\.tab\.active\s*\{\s*display:\s*block;?\s*\}")

    def test_nav_and_sections_cover_same_tabs(self) -> None:
        nav = {tab for tab, _ in self.nav_buttons()}
        sections = {name for name, _ in self.sections()}
        self.assertEqual(nav, sections, f"导航 {sorted(nav)} 与区块 {sorted(sections)} 不对应")
        self.assertGreaterEqual(len(nav), 4)

    def test_exactly_one_active_nav_and_section(self) -> None:
        active_nav = [tab for tab, active in self.nav_buttons() if active]
        active_sections = [name for name, active in self.sections() if active]
        self.assertEqual(len(active_nav), 1, f"应恰好一个导航按钮带 active，实际 {active_nav}")
        self.assertEqual(len(active_sections), 1, f"应恰好一个区块带 active，实际 {active_sections}")
        self.assertEqual(
            active_nav[0],
            active_sections[0],
            "默认高亮的导航按钮与默认显示的区块不是同一个页签 —— 页面打开会是空白",
        )

    def test_default_tab_is_market(self) -> None:
        active_nav = [tab for tab, active in self.nav_buttons() if active]
        self.assertEqual(active_nav, ["market"], "默认页签应为市场扫描")

    def test_tab_switch_is_centralised(self) -> None:
        """切换必须走唯一入口 switchTab，否则按钮与区块的 active 容易再次脱节。"""

        self.assertRegex(self.html, r"function\s+switchTab\s*\(")
        # 初始化时要真的调用一次（行首独立调用，不是只定义不执行）
        # 注意 assertRegex 第三个位置参数是 msg，flags 必须走 re.compile
        self.assertRegex(self.html, re.compile(r"^\s*activateDefaultTab\(\);\s*$", re.M))
        # 不允许再有绕过 switchTab、直接给 section 加 active 的写法
        for bad in (
            r"section\.tab'\)\.forEach\([^)]*classList\.add\('active'\)",
            r"tab-\$\{[^}]*\}\)\.classList\.add\('active'\)",
        ):
            self.assertNotRegex(self.html, bad, "区块 active 只允许由 switchTab 统一设置")


class StockNavigationTest(unittest.TestCase):
    """个股左右切换：入口必须带来源表，否则无从按列表顺序切换。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def table_ids(self) -> set[str]:
        return set(re.findall(r'<table id="(table-[^"]+)"', self.html))

    def test_nav_controls_exist(self) -> None:
        for element_id in ("btn-prev-stock", "btn-next-stock", "nav-info"):
            self.assertIn(f'id="{element_id}"', self.html, f"缺少切换控件 {element_id}")

    def test_row_entries_carry_source_table(self) -> None:
        # 旧写法只传 symbol，切换无从下手；行点击与「查看结构」按钮都必须带上来源表
        self.assertNotRegex(self.html, r"analyzeFrom\('\$\{esc\(h\.symbol\)\}'\)", "命中类表格仍在用旧的单参数入口")
        self.assertNotRegex(self.html, r"analyzeFrom\('\$\{esc\(it\.symbol\)\}'\)", "关注圈仍在用旧的单参数入口")
        self.assertIn("analyzeFrom('${esc(h.symbol)}', '${esc(tableId)}')", self.html)
        self.assertIn("analyzeFrom('${esc(it.symbol)}', 'table-watch')", self.html)
        self.assertIn("function analyzeRowLink(symbol, tableId)", self.html)
        self.assertIn("analyzeRowLink(h.symbol, tableId)", self.html)
        self.assertIn("analyzeRowLink(it.symbol, 'table-watch')", self.html)

    def test_row_button_stops_propagation(self) -> None:
        # 不阻止冒泡的话，一次点击会同时触发按钮与行的 onclick，分析跑两遍
        self.assertIn("event.stopPropagation(); analyzeFrom(", self.html)

    def test_table_labels_and_tabs_cover_every_table(self) -> None:
        tables = self.table_ids()
        self.assertEqual(len(tables), 8, f"应为 8 张表，实际 {sorted(tables)}")
        for mapping_name in ("TABLE_LABELS", "TABLE_TAB"):
            block = re.search(mapping_name + r"\s*=\s*\{(.*?)\};", self.html, re.S)
            self.assertIsNotNone(block, f"缺少 {mapping_name}")
            keys = set(re.findall(r"'(table-[^']+)'", block.group(1)))  # type: ignore[union-attr]
            self.assertEqual(keys, tables, f"{mapping_name} 与表格清单不一致：{sorted(tables ^ keys)}")

    def test_navigation_functions_declared(self) -> None:
        for name in ("orderedSymbols", "setNavContext", "renderNav", "stepStock", "syncNav"):
            self.assertRegex(self.html, r"function\s+" + name + r"\s*\(")

    def test_step_uses_current_sort_order(self) -> None:
        # 切换顺序必须来自排序后的列表，而不是原始 items，否则与用户看到的顺序不一致
        block = re.search(r"function orderedSymbols\(.*?\n\}", self.html, re.S)
        self.assertIsNotNone(block)
        self.assertIn("sortRows(view.items, view.keys, view.type, view.dir)", block.group(0))  # type: ignore[union-attr]

    def test_keyboard_bindings(self) -> None:
        self.assertIn("e.key === 'ArrowLeft'", self.html)
        self.assertIn("e.key === 'ArrowRight'", self.html)
        self.assertIn("e.key === 'Escape' && state.nav", self.html)

    def test_manual_input_clears_nav(self) -> None:
        self.assertRegex(self.html, r"symbol-input'\)\.addEventListener\('input'[^;]*state\.nav = null")

    def test_nav_initialised(self) -> None:
        self.assertRegex(self.html, re.compile(r"^\s*renderNav\(\);\s*$", re.M))


class WatchlistPanelContractTest(unittest.TestCase):
    """关注圈：出圈过滤、重评按钮、成功率面板的前端契约。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_refresh_button_exists(self) -> None:
        self.assertIn('id="btn-watch-refresh"', self.html)
        self.assertIn("$('btn-watch-refresh').addEventListener('click', refreshWatchlist)", self.html)
        self.assertRegex(self.html, r"function\s+refreshWatchlist\s*\(")

    def test_refresh_posts_to_local_endpoint(self) -> None:
        # 重评必须走本地缓存，不能顺手触发联网取数（全市场下代价极高）
        self.assertIn("watchlistRefresh: '/api/watchlist/refresh'", self.html)
        self.assertIn("fetchJSON(API.watchlistRefresh, { method: 'POST' })", self.html)

    def test_second_buy_failed_state_is_labelled(self) -> None:
        # 新增了 SECOND_BUY_FAILED，stateTag 必须认识，否则表格里会显示原始枚举值
        self.assertIn("SECOND_BUY_FAILED: ['fail2', '二买失败']", self.html)
        self.assertIn(".tag.fail2", self.html)

    def test_stats_panel_wired(self) -> None:
        for element_id in ("count-success", "success-kv", "wait-bars-note", "watch-meta"):
            self.assertIn(f'id="{element_id}"', self.html, f"缺少成功率面板元素 {element_id}")
        self.assertIn("watchlistStats: '/api/watchlist/stats'", self.html)
        self.assertRegex(self.html, r"function\s+loadSuccessRate\s*\(")
        self.assertRegex(self.html, r"function\s+renderSuccessRate\s*\(")

    def test_stats_panel_loads_on_market_and_watch_tabs(self) -> None:
        # 市场页签要看成功率，关注圈页签要看进出圈计数，两边都得加载
        self.assertRegex(self.html, r"if \(name === 'watch'\) \{ loadWatchlist\(\); loadSuccessRate\(\); \}")
        self.assertRegex(self.html, r"if \(name === 'market'\) loadSuccessRate\(\);")

    def test_watchlist_hint_documents_removal_rule(self) -> None:
        self.assertIn("一买失败与二买失败即剔除出圈", self.html)
        self.assertIn("二买后窗口内破二买低点", self.html)


class DivergenceSegmentContractTest(unittest.TestCase):
    """K 线背驰段：前端要能从 meta 里取出两段坐标并画出来。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_legend_and_toggle_exist(self) -> None:
        self.assertIn('id="divergence-all"', self.html)
        self.assertIn("$('divergence-all').addEventListener('change'", self.html)
        for name in ("背驰前段", "背驰段"):
            self.assertIn(name, self.html, f"图例缺少 {name}")

    def test_series_functions_declared(self) -> None:
        for name in (
            "divergenceSegments",
            "effortValue",
            "segmentSeries",
            "buildDivergenceSeries",
        ):
            self.assertRegex(self.html, r"function\s+" + name + r"\(")

    def test_all_five_signal_kinds_have_colour_and_effort_keys(self) -> None:
        for kind in ("FIRST_BUY", "SECOND_BUY", "THIRD_BUY", "FIRST_SELL", "SECOND_SELL"):
            self.assertIn(f"{kind}: '#", self.html, f"{kind} 缺少背驰段配色")
        block = re.search(r"const EFFORT_KEYS\s*=\s*\{(.*?)\};", self.html, re.S)
        self.assertIsNotNone(block, "缺少 EFFORT_KEYS 力度键映射")
        # 键名不带引号（FIRST_BUY: [...]），两种写法都认
        keys = set(
            re.findall(r"['\"]?(FIRST_BUY|SECOND_BUY|THIRD_BUY|FIRST_SELL|SECOND_SELL)['\"]?\s*:", block.group(1))  # type: ignore[union-attr]
        )
        self.assertEqual(len(keys), 5, f"力度键映射应覆盖 5 类信号，实际 {sorted(keys)}")

    def test_old_signals_without_segments_are_skipped(self) -> None:
        # 老库没有 previous_segment / current_segment，必须判空而不是崩图
        block = re.search(r"function divergenceSegments\(.*?\n\}", self.html, re.S)
        self.assertIsNotNone(block)
        body = block.group(0)  # type: ignore[union-attr]
        self.assertIn("s.meta.previous_segment", body)
        self.assertIn("s.meta.current_segment", body)

    def test_series_are_appended_to_chart(self) -> None:
        self.assertIn("...buildDivergenceSeries(a.signals),", self.html)
        legend = re.search(r"data: \['K线'.*?\]", self.html, re.S)
        self.assertIsNotNone(legend)
        self.assertIn("'背驰前段'", legend.group(0))  # type: ignore[union-attr]
        self.assertIn("'背驰段'", legend.group(0))  # type: ignore[union-attr]

    def test_default_shows_only_latest_segment(self) -> None:
        # 默认只描最近一个：全画会把图面糊满
        block = re.search(r"function buildDivergenceSeries\(.*?\n\}", self.html, re.S)
        self.assertIsNotNone(block)
        self.assertIn("list.slice(-1)", block.group(0))  # type: ignore[union-attr]

    def test_signal_detail_skips_object_meta(self) -> None:
        # previous_segment 是坐标对象，fmt 只会得到「—」，直接刷掉
        self.assertIn("typeof v !== 'object'", self.html)


if __name__ == "__main__":
    unittest.main()
