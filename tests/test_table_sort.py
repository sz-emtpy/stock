"""表格排序测试：行为测试跑的是 index.html 里真实的排序函数。

做法是从页面里原样抽出 sortValue / compareValues / sortRows 三个纯函数交给 Node
执行 —— 不复制一份实现到测试里，否则测的就不是线上代码了。另外静态校验 8 张表
的 data-key / data-type 声明，以及「默认按二买日或信号日降序」这条口径。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / "frontend" / "index.html"

SORT_FUNCTIONS = ("sortValue", "compareValues", "sortRows")

ROWS = [
    # id: a —— 有完整二买日
    {"id": "a", "date": "2026-01-02", "score": 0.3, "symbol": "sh600001",
     "second_buy_date": "2026-02-01", "first_buy_date": "2025-12-01"},
    # id: b —— 无二买日，靠一买日兜底
    {"id": "b", "date": "2026-01-05", "score": 0.1, "symbol": "sz000002",
     "second_buy_date": "", "first_buy_date": "2025-11-01"},
    # id: c —— 信号日为空，二买日最新
    {"id": "c", "date": "", "score": 0.9, "symbol": "sh600003",
     "second_buy_date": "2026-03-01", "first_buy_date": ""},
    # id: d —— 数值列为空
    {"id": "d", "date": "2026-01-01", "score": None, "symbol": "sz000001",
     "second_buy_date": "2026-01-15", "first_buy_date": "2025-10-01"},
]

MJS_TEMPLATE = """
%s
const rows = JSON.parse(process.argv[2]);
const ids = (r) => r.map(x => x.id);
console.log(JSON.stringify({
  dateDesc: ids(sortRows(rows, ['date'], 'date', 'desc')),
  dateAsc: ids(sortRows(rows, ['date'], 'date', 'asc')),
  numDesc: ids(sortRows(rows, ['score'], 'num', 'desc')),
  fallbackDesc: ids(sortRows(rows, ['second_buy_date', 'first_buy_date'], 'date', 'desc')),
  textAsc: ids(sortRows(rows, ['symbol'], 'text', 'asc')),
  stable: ids(sortRows(rows, ['date'], 'date', 'desc')) !== ids(sortRows(rows, ['date'], 'date', 'asc')),
}));
"""


def extract_sort_functions(html: str) -> str:
    blocks = []
    for name in SORT_FUNCTIONS:
        matched = re.search(r"^function " + name + r"\(.*?^\}", html, re.S | re.M)
        if matched is None:
            raise AssertionError(f"index.html 里找不到函数 {name}")
        blocks.append(matched.group(0))
    return "\n\n".join(blocks)


class SortBehaviourTest(unittest.TestCase):
    """真跑页面里的排序函数。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("未找到 node，跳过排序行为测试")
        cls.node = node
        cls.result = cls._run(cls.html)

    @classmethod
    def _run(cls, html: str) -> dict:
        source = MJS_TEMPLATE % extract_sort_functions(html)
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as handle:
            handle.write(source)
            path = handle.name
        try:
            completed = subprocess.run(
                [cls.node, path, json.dumps(ROWS)],
                capture_output=True, text=True, timeout=60, check=False,
            )
        finally:
            Path(path).unlink(missing_ok=True)
        if completed.returncode != 0:
            raise AssertionError(f"排序脚本执行失败: {completed.stderr[:500]}")
        return json.loads(completed.stdout)

    def test_date_descending_puts_latest_first(self) -> None:
        self.assertEqual(self.result["dateDesc"], ["b", "a", "d", "c"])

    def test_date_ascending(self) -> None:
        self.assertEqual(self.result["dateAsc"], ["d", "a", "b", "c"])

    def test_empty_value_always_last(self) -> None:
        # 空值在升/降序下都必须落在末尾，否则降序时首屏会被空行占满
        self.assertEqual(self.result["dateDesc"][-1], "c")
        self.assertEqual(self.result["dateAsc"][-1], "c")
        self.assertEqual(self.result["numDesc"][-1], "d")

    def test_number_descending(self) -> None:
        self.assertEqual(self.result["numDesc"], ["c", "a", "b", "d"])

    def test_fallback_key_used_when_primary_empty(self) -> None:
        # 二买日缺失时退回一买日：c/a/d 有值按降序，b 靠一买日 2025-11-01 排最后
        self.assertEqual(self.result["fallbackDesc"], ["c", "a", "d", "b"])

    def test_text_ascending(self) -> None:
        self.assertEqual(self.result["textAsc"], ["a", "c", "d", "b"])

    def test_direction_changes_order(self) -> None:
        self.assertTrue(self.result["stable"])


class SortDeclarationTest(unittest.TestCase):
    """由排序引擎驱动的表的表头声明与默认排序口径。

    注意 `table-runs`（扫描历史）不在此列：它是纯展示表，直接由 innerHTML 渲染，
    不经过 setTableItems/paintTable，所以没有 data-key 声明也不需要排序。
    """

    # 走 setTableItems → paintTable 的表（排序/搜索/分页共用一套引擎）
    SORT_DRIVEN = (
        "table-first",
        "table-second",
        "table-watch",
        "table-favorites",
        "table-pool-first",
        "table-pool-second",
        "table-pool-third",
        "table-pool-first-sell",
        "table-pool-second-sell",
        "table-pick",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def tables(self) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for matched in re.finditer(
            r'<table id="(?P<id>[^"]+)">\s*<thead><tr>(?P<head>.*?)</tr></thead>', self.html, re.S
        ):
            keys = re.findall(r'<th([^>]*)>', matched.group("head"))
            attrs = [re.search(r'data-key="([^"]+)"', k) for k in keys]
            found[matched.group("id")] = [a.group(1) if a else "" for a in attrs]
        return found

    def test_sort_driven_tables_declared(self) -> None:
        tables = self.tables()
        display_only = {"table-runs", "table-cards", "table-snapshots"}
        self.assertEqual(
            set(tables), set(self.SORT_DRIVEN) | display_only,
            f"表格清单与预期不一致：{sorted(set(tables))}",
        )

    def test_every_table_has_sortable_columns(self) -> None:
        tables = self.tables()
        for table_id in self.SORT_DRIVEN:
            self.assertIn(table_id, tables, f"缺少表 {table_id}")
            keys = tables[table_id]
            declared = [key for key in keys if key]  # 空串表示不可排序（状态/类型/操作）
            self.assertTrue(declared, f"{table_id} 没有任何可排序列")
            self.assertEqual(len(declared), len(set(declared)), f"{table_id} 存在重复排序键: {declared}")

    def test_action_column_not_sortable(self) -> None:
        tables = self.tables()
        for table_id in self.SORT_DRIVEN:
            self.assertEqual(tables[table_id][-1], "", f"{table_id} 的「操作」列不应参与排序")

    def test_date_columns_typed_as_date(self) -> None:
        for column in ("信号日", "一买日", "二买日"):
            for matched in re.finditer(r'<th([^>]*)>' + column + r'</th>', self.html):
                self.assertIn('data-type="date"', matched.group(1))

    def test_price_columns_typed_as_num(self) -> None:
        for matched in re.finditer(r'<th([^>]*)>(信号价|一买价|一买低点)</th>', self.html):
            self.assertIn('data-type="num"', matched.group(1))

    def test_second_buy_date_falls_back_to_first_buy_date(self) -> None:
        self.assertRegex(
            self.html,
            r'data-key="second_buy_date"\s+data-fallback="first_buy_date"',
            "二买日必须有兜底字段，否则没有二买的条目在降序时会全挤在末尾且无序",
        )

    def test_default_sort_keys_declared(self) -> None:
        # 命中类表格默认按信号日，关注圈默认按二买日
        self.assertIn("</tr>`, 'date');", self.html)
        self.assertIn("</tr>`, 'second_buy_date');", self.html)


if __name__ == "__main__":
    unittest.main()
