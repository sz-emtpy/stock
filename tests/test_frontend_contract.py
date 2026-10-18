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
        # 9 张带「查看结构」入口的表（候选/关注圈/5 张池表/自选）。
        # table-runs / table-cards / table-snapshots 是纯展示表格，没有个股跳转，
        # 故不参与 TABLE_LABELS 映射。
        tables = self.table_ids()
        display_only = {"table-runs", "table-cards", "table-snapshots"}
        navigable = tables - display_only
        self.assertEqual(len(tables), 13, f"应为 13 张表，实际 {sorted(tables)}")
        for mapping_name in ("TABLE_LABELS", "TABLE_TAB"):
            block = re.search(mapping_name + r"\s*=\s*\{(.*?)\};", self.html, re.S)
            self.assertIsNotNone(block, f"缺少 {mapping_name}")
            keys = set(re.findall(r"'(table-[^']+)'", block.group(1)))  # type: ignore[union-attr]
            self.assertEqual(keys, navigable, f"{mapping_name} 与表格清单不一致：{sorted(navigable ^ keys)}")

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
        # 市场页签现在还顺带加载板块热度与扫描历史
        self.assertRegex(self.html, r"if \(name === 'market'\) \{[^}]*loadSuccessRate\(\)")

    def test_watchlist_hint_documents_removal_rule(self) -> None:
        # 出圈规则要在界面上写全：一买失败 / 二买失败 / 二买守住 三种终态都会出圈，
        # 且必须说明「关注圈是状态机跟踪池，数量天然小于买点池」这一口径差异。
        self.assertIn("一买失败、二买失败、二买守住都会出圈", self.html)
        self.assertIn("二买后窗口内破二买低点", self.html)
        self.assertIn("关注圈是「状态机跟踪池」", self.html)


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

    def test_divergence_toggle_semantics_inverted(self) -> None:
        """背驰段开关语义（用户确认已反转）：

        复选框文案是「只看最近一个背驰段」——默认**不勾选 = 全画**，
        勾选后才只描最近一个（防止图面糊满）。这与早期「默认只描最近一个」相反。

        锁定：① 复选框 label 必须是「只看最近一个背驰段」；
        ② `buildDivergenceSeries` 里「不勾选」分支必须落到 `list`（全画），
           「勾选」分支才 `list.slice(-1)`（只取最近一个）。
        谁把默认又改回「只描最近一个」会让早期信号重新糊满图面，必须抓住。
        """

        self.assertIn("只看最近一个背驰段", self.html, "背驰段开关文案必须是「只看最近一个背驰段」")
        block = re.search(r"function buildDivergenceSeries\(.*?\n\}", self.html, re.S)
        self.assertIsNotNone(block, "缺少 buildDivergenceSeries 定义")
        body = block.group(0)  # type: ignore[union-attr]
        self.assertIn("list.slice(-1)", body, "勾选后应只取最近一个背驰段（slice(-1)）")
        # 默认分支：onlyLatest 为假时取全部 list（全画）
        self.assertRegex(
            body,
            r"onlyLatest[^;]*\?[^:]*list\.slice\(-1\)\s*:\s*list",
            "默认（不勾选）必须画全部背驰段，而非只画最近一个",
        )  # type: ignore[union-attr]

    def test_signal_detail_skips_object_meta(self) -> None:
        # previous_segment 是坐标对象，fmt 只会得到「—」，直接刷掉
        self.assertIn("typeof v !== 'object'", self.html)


class PoolRateAndChartContractTest(unittest.TestCase):
    """买卖点池胜算列 + 个股对比条 + K 线 tooltip 涨跌幅 的前端契约。

    本轮新增：池表每行要直接显示当前节点的胜算率；分析页要展示个股所属板块
    与同期个股/板块/大盘力度对比；K 线 tooltip 要显示当日涨跌幅。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_pool_tables_have_numeric_rate_column(self) -> None:
        # 5 张买卖点池表都要有「胜算」列，且 data-type=num
        # （排序引擎靠 data-type 决定数值排序，写成 text 会让胜算列乱序）。
        # 买点池（一买/二买/三买）的胜算列排序键是 system_score_value——体系分在前；
        # 卖点池（一卖/二卖）没有体系分口径，排序键仍是 rate_value。
        sys_cols = self.html.count('data-key="system_score_value" data-type="num">胜算')
        rate_cols = self.html.count('data-key="rate_value" data-type="num">胜算')
        self.assertGreaterEqual(sys_cols, 3, "买点池缺少体系分型「胜算」列（应有 3 张）")
        self.assertGreaterEqual(rate_cols, 2, "卖点池缺少数值型「胜算」列（应有 2 张）")
        self.assertGreaterEqual(sys_cols + rate_cols, 5, "买卖点池应有 5 张带「胜算」列的表")

    def test_pool_rate_cell_shows_system_score(self) -> None:
        """胜算列要同时体现「体系分」与「胜率」，且体系分来自后端即将给出的字段。"""

        self.assertRegex(self.html, r"function\s+systemScoreCell\s*\(")
        self.assertRegex(self.html, r"function\s+poolRateCell\s*\(")
        self.assertIn("h.system_score", self.html)
        self.assertIn("h.system_hits", self.html)
        self.assertIn("h.system_available", self.html)
        # 买点池调用处必须开 opts.systemScore，否则渲染的是纯胜率、体系分不显示
        self.assertIn("'一买', { systemScore: true })", self.html)
        self.assertIn("'二买', { systemScore: true })", self.html)
        self.assertIn("'三买', { systemScore: true })", self.html)

    def test_comparison_bar_container_exists(self) -> None:
        # 个股所属板块 + 同期对比信息条的挂载点
        self.assertIn('id="comparison-bar"', self.html, "缺少对比条容器 comparison-bar")

    def test_comparison_renderer_wired(self) -> None:
        # 分析入口要把 sectors / comparison 挂上并调用渲染
        self.assertRegex(self.html, r"function\s+renderComparison\s*\(")
        self.assertIn("renderComparison(data)", self.html)
        self.assertIn("$('comparison-bar')", self.html)

    def test_chart_tooltip_shows_daily_change_pct(self) -> None:
        # K 线 tooltip 必须显示「当日涨跌幅 %」（相对前一根收盘），红涨绿跌
        self.assertIn("当日幅度", self.html, "tooltip 缺少「当日幅度」涨跌幅展示")
        # 涨跌幅计算：相对前一根收盘
        self.assertRegex(
            self.html,
            r"chg\s*=\s*item\.prevClose\s*\?\s*\(item\.close\s*-\s*item\.prevClose\)\s*/",
            "tooltip 涨跌幅必须相对前一根收盘计算",
        )
        # 中国市场配色：涨红跌绿
        self.assertRegex(self.html, r"chg\s*>=\s*0\s*\?\s*'#ef4444'\s*:\s*'#22c55e'")

    def test_datazoom_defaults_to_full_view(self) -> None:
        # 默认全览，不让前 55% 的历史买卖点被裁掉
        self.assertRegex(self.html, r"start:\s*0,\s*end:\s*100", "dataZoom 默认必须全览（start:0,end:100）")

    def test_all_five_signal_kinds_rendered_on_chart(self) -> None:
        # 一买/二买/三买用 pin（黄/绿/紫），一卖/二卖用向下箭头（浅红/深红）
        for needle in (
            "pinSeries('一买'",
            "pinSeries('二买'",
            "pinSeries('三买'",
            "arrowSeries('一卖'",
            "arrowSeries('二卖'",
        ):
            self.assertIn(needle, self.html, f"图表缺少买卖点标注：{needle}")


class IntervalNestingContractTest(unittest.TestCase):
    """30 分钟区间套的前端契约：按钮、容器、渲染函数、API 入口都要就位。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_nesting_button_exists(self) -> None:
        self.assertIn('id="btn-nesting"', self.html, "缺少「30分钟区间套」按钮")
        self.assertIn("$('btn-nesting').addEventListener('click', runIntervalNesting)", self.html)

    def test_nesting_container_exists(self) -> None:
        self.assertIn('id="nesting-box"', self.html, "缺少区间套结果容器 nesting-box")

    def test_nesting_renderer_wired(self) -> None:
        self.assertRegex(self.html, r"function\s+renderIntervalNesting\s*\(")
        self.assertIn("API.intervalNesting", self.html)
        self.assertIn("runIntervalNesting()", self.html)

    def test_nesting_short_labels_defined(self) -> None:
        # 渲染里用 KIND_LABELS_SHORT 缩写买点名，必须定义否则渲染抛错
        self.assertIn("KIND_LABELS_SHORT", self.html)


class LazyComparisonContractTest(unittest.TestCase):
    """板块/对比懒加载的前端契约。

    板块反查实测 5.7 秒，绝不能挡在首屏上：/api/analyze 只在已有板块缓存时
    才直接带上 comparison；否则返回 lazy=true，由前端异步补一次独立请求。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_comparison_api_entry_exists(self) -> None:
        self.assertIn("/api/analysis/comparison", self.html, "缺少板块/对比独立接口")

    def test_lazy_loader_declared_and_called(self) -> None:
        self.assertRegex(self.html, r"function\s+loadComparison\s*\(")
        self.assertIn("loadComparison(symbol, state.period, data)", self.html)

    def test_analyze_renders_before_comparison(self) -> None:
        """先渲染 K 线与结论，再补对比——顺序反了就说明又阻塞首屏了。"""

        block = re.search(r"async function runAnalyze\(.*?\n\}", self.html, re.S)
        self.assertIsNotNone(block)
        body = block.group(0)  # type: ignore[union-attr]
        self.assertLess(
            body.index("renderChart(data)"),
            body.index("loadComparison("),
            "K 线渲染必须在对比懒加载之前",
        )

    def test_lazy_guard_discards_stale_response(self) -> None:
        """快速左右切换时，慢的对比响应不能覆盖当前这只票。"""

        self.assertRegex(self.html, r"comparisonToken", "缺少对比请求的竞态守卫")
        self.assertRegex(self.html, r"token\s*!==\s*comparisonToken")

    def test_lazy_marker_respected(self) -> None:
        # 后端标记 lazy 时前端不再画一次（避免两个「加载中」打架）
        self.assertRegex(self.html, r"c\.lazy")

    def test_existing_sector_bar_still_used(self) -> None:
        # 原有 comparison-bar 容器与 renderComparison 不能被删
        self.assertIn('id="comparison-bar"', self.html)
        self.assertRegex(self.html, r"function\s+renderComparison\s*\(")


class UsabilityContractTest(unittest.TestCase):
    """易用性补强的前端契约：搜索/筛选/分页/联想/主题/错误块/时间线/三联图/自选/热度/历史。

    这些功能都靠 DOM id 与函数名接线，改名或漏写会静默失效（点了没反应），
    所以用纯文本断言把它们钉住。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_every_table_has_search_box(self) -> None:
        # 纯展示表（扫描历史/结构卡/快照）不需要搜索；其余 9 张业务表都要有
        display_only = {"table-runs", "table-cards", "table-snapshots"}
        tables = set(re.findall(r'<table id="(table-[^"]+)"', self.html)) - display_only
        searched = set(re.findall(r'data-search="(table-[^"]+)"', self.html))
        self.assertEqual(searched, tables, f"缺搜索框的表：{sorted(tables - searched)}")

    def test_watchlist_has_state_filter(self) -> None:
        self.assertIn('data-filter="table-watch"', self.html)
        # 五个状态都要能筛
        for state in ("WAIT_SECOND_BUY", "WEAK_TRACK", "OPPORTUNITY", "FAILED", "SECOND_BUY_FAILED"):
            self.assertIn(f'<option value="{state}"', self.html, f"筛选下拉缺少状态 {state}")

    def test_pagination_replaces_show_all_only(self) -> None:
        # 分页落地后，「显示全部」不再是唯一手段
        self.assertIn("const PAGE_SIZE = 50;", self.html)
        self.assertRegex(self.html, r"function\s+paintPager\s*\(")
        self.assertRegex(self.html, r"window\.gotoPage\s*=\s*function")

    def test_search_does_not_mutate_view_items(self) -> None:
        """搜索只过滤视图、不改 view.items —— 否则左右切换的顺序会被搜索词改掉。"""

        block = re.search(r"function viewFiltered\(.*?\n\}", self.html, re.S)
        self.assertIsNotNone(block, "缺少 viewFiltered")
        body = block.group(0)  # type: ignore[union-attr]
        self.assertIn("view.items", body)
        self.assertNotIn("view.items =", body, "搜索不该写回 view.items")

    def test_symbol_input_autocomplete(self) -> None:
        self.assertIn('id="symbol-suggest"', self.html)
        self.assertRegex(self.html, r"function\s+querySuggest\s*\(")
        self.assertRegex(self.html, r"function\s+pickSuggest\s*\(")
        self.assertIn("/api/universe?q=", self.html)

    def test_symbol_prefix_normalisation(self) -> None:
        """输 600519 要能自动补成 sh600519（后端只认带前缀的代码）。"""

        block = re.search(r"function normalizeSymbolInput\(.*?\n\}", self.html, re.S)
        self.assertIsNotNone(block, "缺少 normalizeSymbolInput")
        body = block.group(0)  # type: ignore[union-attr]
        self.assertIn("'sh'", body)
        self.assertIn("'sz'", body)
        self.assertRegex(body, r"startsWith\('6'\)")
        self.assertRegex(body, r"startsWith\('0'\)")

    def test_theme_toggle_and_persist(self) -> None:
        self.assertIn('id="btn-theme"', self.html)
        self.assertIn("body.light", self.html)
        self.assertIn("chanpicker-theme", self.html, "主题需要 localStorage 持久化")
        self.assertRegex(self.html, r"function\s+applyTheme\s*\(")

    def test_error_block_replaces_toast_only(self) -> None:
        self.assertIn('id="error-box"', self.html)
        self.assertRegex(self.html, r"function\s+showError\s*\(")
        self.assertRegex(self.html, r"function\s+classifyError\s*\(")
        # 至少覆盖限流与 K 线不足两类建议
        self.assertIn("数据源限流", self.html)
        self.assertIn("K 线数量不足", self.html)

    def test_chart_loading_skeleton(self) -> None:
        self.assertRegex(self.html, r"function\s+showChartLoading\s*\(")
        self.assertRegex(self.html, r"function\s+hideChartLoading\s*\(")

    def test_chart_loading_does_not_hide_chart(self) -> None:
        """loading 用覆盖层，绝不能把 chart 容器设 display:none——否则首次分析时
        echarts.init 在隐藏容器上拿到 0 尺寸、之后不 resize，图表空白。"""
        block = re.search(r"function showChartLoading\(.*?\n\}", self.html, re.S)
        self.assertIsNotNone(block)
        body = block.group(0)  # type: ignore[union-attr]
        self.assertNotIn("chart.style.display", body, "showChartLoading 不应改 chart 的 display")
        self.assertNotIn("prevDisplay", body)

    def test_timeline_panel(self) -> None:
        self.assertIn('id="timeline-card"', self.html)
        self.assertIn('id="timeline-list"', self.html)
        self.assertRegex(self.html, r"function\s+renderTimeline\s*\(")

    def test_three_timeframe_strip(self) -> None:
        self.assertIn('id="tf-grid"', self.html)
        self.assertRegex(self.html, r"function\s+renderTimeframeStrip\s*\(")
        # 三格必须覆盖日线 / 30 分钟 / 15 分钟
        for period in ("'day'", "'m30'", "'m15'"):
            self.assertIn(period, self.html)

    def test_favorites_tab_and_star(self) -> None:
        self.assertIn('<button data-tab="favorites">自选持仓</button>', self.html)
        self.assertIn('id="btn-fav"', self.html)
        self.assertIn('id="table-favorites"', self.html)
        self.assertRegex(self.html, r"function\s+loadFavorites\s*\(")
        self.assertRegex(self.html, r"function\s+toggleFavorite\s*\(")
        # 成本价编辑与浮盈展示
        self.assertRegex(self.html, r"window\.editCost\s*=")
        self.assertIn("pnl_pct", self.html)

    def test_sector_heat_panel(self) -> None:
        self.assertIn('id="heat-list"', self.html)
        self.assertRegex(self.html, r"function\s+loadSectorHeat\s*\(")
        self.assertIn("/api/market/sector-heat", self.html)

    def test_scan_history_and_incremental(self) -> None:
        self.assertIn('id="table-runs"', self.html)
        self.assertRegex(self.html, r"function\s+loadScanHistory\s*\(")
        self.assertIn('id="btn-scan-incremental"', self.html)
        self.assertRegex(self.html, r"function\s+startIncrementalScan\s*\(")

    def test_batch_nesting_panel(self) -> None:
        self.assertIn('id="btn-batch-nesting"', self.html)
        self.assertRegex(self.html, r"function\s+runBatchNesting\s*\(")
        self.assertIn("/api/market/interval-nesting/batch", self.html)

    def test_sensitivity_panel(self) -> None:
        self.assertIn('id="sensitivity-body"', self.html)
        self.assertRegex(self.html, r"function\s+loadSensitivity\s*\(")
        self.assertIn("/api/backtest/sensitivity", self.html)

    def test_url_share_state(self) -> None:
        self.assertRegex(self.html, r"function\s+readUrlState\s*\(")
        self.assertRegex(self.html, r"function\s+writeUrlState\s*\(")
        self.assertIn("history.replaceState", self.html)
        # 启动时应读取 URL 并自动分析
        self.assertRegex(self.html, r"if\s*\(urlState\.symbol\)")

    def test_prefs_persist(self) -> None:
        self.assertRegex(self.html, r"function\s+loadPrefs\s*\(")
        self.assertRegex(self.html, r"function\s+savePrefs\s*\(")
        self.assertIn("chanpicker-prefs", self.html)

    def test_mobile_responsive_rules(self) -> None:
        self.assertIn("@media (max-width: 860px)", self.html)
        # 移动端表格必须能横向滚动，否则列被压扁
        self.assertRegex(self.html, r"\.card > table \{ display: block; overflow-x: auto;")

    def test_rate_tooltip_is_visible_not_title(self) -> None:
        """胜算详情用可见 tooltip，不用 title（触屏看不到 title）。"""

        block = re.search(r"function rateCell\(.*?\n\}", self.html, re.S)
        self.assertIsNotNone(block)
        body = block.group(0)  # type: ignore[union-attr]
        self.assertIn("tip-bubble", body)
        self.assertNotIn("title=", body, "rateCell 不应退回 title 属性")


class TradingSystemContractTest(unittest.TestCase):
    """交易体系页 + 个股匹配分的前端契约。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_system_tab_and_sections(self) -> None:
        self.assertIn('<button data-tab="system">交易体系</button>', self.html)
        self.assertIn('id="tab-system"', self.html)
        for element_id in ("sys-kv", "sys-gain", "table-cards", "sys-validate", "table-snapshots"):
            self.assertIn(f'id="{element_id}"', self.html, f"缺少 {element_id}")

    def test_system_render_functions(self) -> None:
        for name in ("loadSystem", "renderSystemGain", "renderSystemCards",
                     "renderSystemValidate", "rebuildSystem", "renderSystemMatch"):
            self.assertRegex(self.html, r"function\s+" + name + r"\s*\(")

    def test_system_api_wired(self) -> None:
        for path in ("/api/system/gain", "/api/system/validate",
                     "/api/system/rebuild-outcomes", "/api/system/history"):
            self.assertIn(path, self.html, f"缺少 API {path}")

    def test_stock_match_rendered(self) -> None:
        self.assertIn('id="system-match"', self.html)
        self.assertIn("renderSystemMatch(data.system_match, data.bars)", self.html)

    def test_match_latest_and_history_split(self) -> None:
        """近 3 个交易日的买点突出显示，更早的一律归入历史折叠（用户口径）。"""

        self.assertIn("is_active", self.html)
        self.assertIn("SM_LATEST_WINDOW_DAYS", self.html)
        self.assertIn("tradingDaysFromLast", self.html)
        # 调用处必须把 bars 传进来供交易日距离计算
        self.assertIn("renderSystemMatch(data.system_match, data.bars)", self.html)
        self.assertIn("sm-latest", self.html)
        self.assertRegex(self.html, r"window\.toggleSystemHistory\s*=")
        self.assertIn('id="sm-history"', self.html)
        # 展开/收起按钮
        self.assertIn("展开历史", self.html)

    def test_system_tab_loads_on_switch(self) -> None:
        self.assertRegex(self.html, r"if \(name === 'system'\) loadSystem\(\);")

    def test_pick_tab_and_panel(self) -> None:
        self.assertIn('<button data-tab="pick">体系选股</button>', self.html)
        self.assertIn('id="tab-pick"', self.html)
        self.assertIn('id="table-pick"', self.html)
        self.assertRegex(self.html, r"function\s+loadPick\s*\(")
        self.assertIn("/api/system/pick", self.html)
        self.assertRegex(self.html, r"if \(name === 'pick'\) loadPick\(\);")


class TradingSystemPickContractTest(unittest.TestCase):
    """体系选股池前端契约：跳转映射 + 搜索筛选。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_pick_table_navigable(self) -> None:
        # 选股池表格要能点「查看结构」跳个股，必须进 TABLE_LABELS/TABLE_TAB
        self.assertIn("'table-pick': '体系选股'", self.html)
        self.assertIn("'table-pick': 'pick'", self.html)

    def test_pick_has_search_and_filter(self) -> None:
        self.assertIn('data-search="table-pick"', self.html)
        self.assertIn('data-filter="table-pick"', self.html)
        # 三种买点都能筛
        for kind in ("FIRST_BUY", "SECOND_BUY", "THIRD_BUY"):
            self.assertIn(f'<option value="{kind}"', self.html)


class ChartBigViewContractTest(unittest.TestCase):
    """点击主图放大看大图（用户反馈的「点日线图没大图」）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_modal_and_functions(self) -> None:
        self.assertIn('id="chart-modal"', self.html)
        self.assertIn('id="cm-chart"', self.html)
        for name in ("openBigChart", "closeBigChart", "buildChartOption"):
            self.assertRegex(self.html, r"function\s+" + name + r"\s*\(")

    def test_chart_click_bound_to_open(self) -> None:
        # 用容器原生 DOM 事件，点空白也能放大（chart.on('click')/getZr 只响应图形元素）
        self.assertRegex(self.html, r"\$\('chart'\)\.addEventListener\('click',\s*\(\) => openBigChart\(\)\)")

    def test_resize_listener_registered_once(self) -> None:
        # resize 只在 init 块里注册一次，不能每次 renderChart 都 addEventListener（会堆积）
        block = re.search(r"function renderChart\(.*?\n\}", self.html, re.S)
        self.assertIsNotNone(block)
        body = block.group(0)  # type: ignore[union-attr]
        self.assertEqual(body.count("addEventListener('resize'"), 1, "resize 监听器只应注册一次")

    def test_esc_closes_modal(self) -> None:
        self.assertIn("keydown", self.html)
        self.assertIn("closeBigChart()", self.html)


if __name__ == "__main__":
    unittest.main()
