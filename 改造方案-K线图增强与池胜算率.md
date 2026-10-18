# 改造方案：K线图增强 + 买卖点池胜算率

> 状态：**待确认，未编码**。本文只做现状核查与方案设计，等明确指令再落地。
> 所有「实测」结论均来自本机对 westock CLI 与本地库 `data/chanpicker.db` 的真实调用。

---

## 〇、现状核查结论（先看这个，避免重复劳动）

### 1. 「所有买卖点标识」其实已经画了，问题是**默认看不见**

`renderChart()`（`frontend/index.html:1183-1217`）已经为五种信号各建了独立 series：

| series | 形态 | 颜色 |
| --- | --- | --- |
| 一买 | pin | 黄 `#f0b429` |
| 二买 | pin | 绿 `#3fb950` |
| 三买 | pin | 紫 `#a371f7` |
| 一卖 | 向下箭头（锚 `anchor_index/anchor_price`） | 浅红 `#ff7d6b` |
| 二卖 | 向下箭头（偏移 -42px 避让） | 深红 `#ff4d4f` |

legend 也齐全（`:1224`）。**真正的遮挡点在 `dataZoom`**：

```js
dataZoom: [{ type: 'inside', xAxisIndex: [0,1], start: 55, end: 100 }, ...]
```

默认只显示**后 45%** 的 K 线，前 55% 上的买卖点被裁掉了。用户感知到的「没标全」大概率是这个。

→ 这一项不是「新增绘制」，是**改默认视窗 + 补重叠避让**。

### 2. 背驰段默认只画最近一个

`buildDivergenceSeries()`（`:1114`）：

```js
const picked = $('divergence-all') && $('divergence-all').checked ? list : list.slice(-1);
```

默认 `slice(-1)`，勾选「显示全部背驰段」才全出。要反过来。

### 3. 板块数据链**能闭合**（本次新验证，别再重复探测）

 westock 支持板块 K 线，路径三步：

```
① westock industry-chain stock sh600519
   → 所属主题：食品饮料 / 商贸零售 / 白酒
② westock search 白酒 --type sector
   → pt02B20001 白酒概念(聚源) | pt01801125 白酒Ⅱ(申万二级) | pt01851251 白酒Ⅲ(申万三级)
③ westock kline pt01801125 --period day --start 2025-01-01 --end 2026-10-16
   → 433 行，深度与个股同源，足够
```

大盘指数同样可取：`westock kline sh000001 --period day`（`/api/health` 已在用 `quote sh000001` 做鉴权探测）。

**注意**：`quote` 不返回板块字段，所以必须走 `industry-chain stock` 反查，不能指望一次调用拿全。

### 4. 池胜算率的性能瓶颈定位在 `signals_for_symbol`

三日池实测规模（2026-10-14 ~ 10-16）：

```
一买 165 · 二买 27 · 三买 54 · 一卖 84 · 二卖 12  → 唯一标的 241 只
```

对这 241 只跑现有 `symbol_success_rate` 的取数部分：

| 取数方式 | 实测耗时 |
| --- | --- |
| `signals_for_symbol()` × 241（逐只） | **6206 ms** ← 瓶颈 |
| `load_bars()` × 241（逐只） | 630 ms |
| `SELECT ... WHERE symbol IN (...)` 批量（500 分批） | **62 ms** |
| bars 批量 `IN`（只取 close） | 100 ms |

**结论：不需要落缓存表，只要把取数批量化，241 只全量算约 250–300 ms，够快。**
（瓶颈成因：`signals` 表无 `symbol` 前导的复合索引，逐只会退化为按 `period` 扫后过滤。）

---

## 一、需求 A：K线图增强

### A1. 买卖点全标识

- [ ] `dataZoom` 默认改为 `start: 0, end: 100`（全览），或按 bars 数量自适应：≤300 根全览，>300 根取后 60%
- [ ] 一买/二买/三买 pin 的 `symbolSize` 46 在同区间密集时会叠成一坨 → 同一 `bar_index` 附近做纵向错开（按同 index 内序号 × -18px 递增偏移）
- [ ] 一卖/二卖箭头偏移量（现 -20 / -42）同样改为按同 index 冲突数动态计算
- [ ] legend 默认全选不变；`notMerge: true` 保留（否则切周期会串味）

### A2. 背驰段默认全展示

- [ ] `picked = list`（默认全画），开关语义**反转**为「只看最近一个」，label 文案同步改
- [ ] 防糊：超过 N 个（建议 8）时，只画最近 8 个并在 legend 旁提示「共 X 段，显示最近 8 段」——**这一条要不要做由你定**，不做也能用
- [ ] 端点的「力度 x.xx」文字在密集时会互相压住 → 多段时只在**背驰段**端点留字，前段不留（前段一律灰色虚线，颜色已可辨识）

### A3. 个股所属板块

新增表 + 取数：

```sql
CREATE TABLE IF NOT EXISTS stock_sectors (
  symbol      TEXT NOT NULL,
  sector_code TEXT NOT NULL,   -- pt01801125
  sector_name TEXT NOT NULL,   -- 白酒Ⅱ
  level       TEXT NOT NULL,   -- sw2 / sw3 / concept / chain
  updated_at  REAL NOT NULL,
  PRIMARY KEY (symbol, sector_code)
);
```

取数策略（**只在首次分析某只票时联网一次，之后复用**）：

1. `industry-chain stock <symbol>` → 主题名列表
2. 每个主题名 `search <name> --type sector`，**优先级：申万二级 > 申万三级 > 聚源概念**，取 1 个主板块 + 最多 2 个备选
3. 写入 `stock_sectors`
4. 前端在标题区/K线图卡片头显示：`贵州茅台(sh600519) · 白酒Ⅱ · 食品饮料`

限流防护：单只票约 1（产业链）+ 3（search）次调用，按既有 `BATCH_PAUSE=25s` 的教训，这里要内置 **≥3s 间隔 + 失败静默降级**（拿不到就显示「板块未知」，绝不阻断主流程）。

### A4. 同期个股 vs 板块 vs 大盘 力度对比

**口径要点（容易做错的地方）**：

- 个股是 **qfq 前复权**，板块/指数**没有复权概念** → **绝不能直接比价格**，一律换算成**区间涨跌幅 %** 再比
- MACD 力度（`effort.macd_area`）是**绝对值**，随价格量纲变化 → 板块指数点位 36000、个股 1300，面积不可比 → 必须**归一化**：同一窗口内 `area / 该标的首日收盘价` 或直接用「平均每根 K 线的面积占比」
- 时间窗对齐：以**个股 K 线的起止日期**为准，三者截取同一区间，缺失日对齐跳过

展示形态（两种，选一）：

- **方案 a（推荐，改动小）**：K 线图下方新增一条「同期对比」信息条
  `个股 +5.2% ｜ 白酒Ⅱ +3.1% ｜ 上证 -0.4% ｜ 相对板块 +2.1pp ｜ 相对大盘 +5.6pp`
  再配一个横向条形，红绿按中国市场惯例（涨红跌绿）
- **方案 b（信息量大，改动大）**：图上叠加三条归一化净值曲线（个股/板块/大盘，各自起点=100），新增一个副 grid

后端新增：`GET /api/analyze?symbol=...` 的响应里挂 `comparison` 字段：

```json
{
  "sector": { "code": "pt01801125", "name": "白酒Ⅱ", "level": "sw2" },
  "comparison": {
    "window": { "start": "2026-01-02", "end": "2026-10-16", "bars": 190 },
    "stock":   { "change_pct": 5.21, "effort": 12.4, "effort_norm": 0.0095 },
    "sector":  { "change_pct": 3.10, "effort": 9.8,  "effort_norm": 0.0076 },
    "market":  { "change_pct": -0.40, "effort": 5.1, "effort_norm": 0.0041 }
  }
}
```

---

## 二、需求 B：三日买卖点池直接展示胜算率

### B1. 后端：把取数批量化（性能前提）

- [ ] `Store.signals_for_symbols(symbols, period)` —— `WHERE period=? AND symbol IN (...)`，500 分批，带 `@_sync`
- [ ] `Store.load_close_series(symbols, period)` —— 只取 `symbol, trade_date, close`（二买判定只需收盘价）
- [ ] `ScanService.symbol_success_rates(symbols)` —— 复用现成的 `_judge_first_buy` / `_judge_second_buy` / `_summarize`，**判定口径一字不改**
- [ ] `/api/market/pools` 与 `/api/market/risk` 的每一行直接挂 `rate` 字段，前端**不额外发请求**

复用而不是重写的原因：记忆条目 14 明确「成功率有两套口径，勿混」。这次只是把个股口径**批量化**，判定逻辑照搬 `_judge_*`。

### B2. 前端：池表新增「胜算」列

加在「状态」列前（`renderHitTable` 的模板里插一列，表头补 `data-key="rate"`）：

| 池 | 显示内容 |
| --- | --- |
| 一买池 | **一买胜算**：`67% (2/3)` 或 `样本不足` 或 `市场 41%` |
| 二买池 | **二买胜算**：同理，取 `second_buy` |
| 三买池 | `—`（无成功率口径，不要硬造） |
| 一卖/二卖池 | `—`（卖点从不参与成功率统计，记忆条目 1） |

**关键决策：个股样本不足时怎么办**（实测大部分票历史信号 < 3 次）：

- **选项 1（推荐）混合**：个股 `closed >= 3` 显示个股百分比并标「个股」徽章；不足则退回 `watchlist_stats()` 的全市场基准，灰色显示 `市场 41%`，hover 提示「该股历史样本 N 次，不足 3 次，暂用全市场基准」
- 选项 2 纯个股：不足就显示 `样本不足（1/2）`，不给数字
- 选项 3 纯市场：所有行同一个全市场数字

排序：新列 `data-type="num"`、`data-fallback` 给 `-1`，保证空值恒排最后（记忆条目 6 的排序引擎约束）。

### B3. 回归测试

- [ ] `tests/test_query_performance.py` 加一条：241 只批量胜率 ≤ 800 ms（锁死不许退回逐只查询）
- [ ] `tests/test_backend_service.py` 加：批量结果与逐只 `symbol_success_rate` **逐字段相等**（防批量化改坏口径）
- [ ] `tests/test_frontend_contract.py` 加：池表「胜算」列存在且 `data-type="num"`
- [ ] `tests/test_frontend_contract.py` 的 `StockNavigationTest` 必须仍过（加列不能破坏左右切换）

---

## 三、边界与风险

| 风险 | 处置 |
| --- | --- |
| 板块/指数取数触发数据源限流（实测连续请求会「请求过于频繁」） | 内置 ≥3s 间隔 + 3 次退避；失败降级为「板块未知」，**绝不阻断主分析** |
| 板块 K 线与个股交易日不对齐（板块停牌日缺失） | 按日期取交集，不对齐就跳过；窗口 bars < 30 时对比结论标注「样本过少」 |
| 归一化力度没有原文依据，容易被当成「缠论标准」 | UI 上明确标注为**辅助参考**，不混入 `qualifies` 判定（买点判定仍只看原文之道） |
| 池表加列后表格变宽 | 现有 `.red/.green` 等类复用，不加新 CSS 断点；移动端不在本次范围 |
| 改 `dataZoom` 后首屏渲染点数变多 | 400 根 K 线对 ECharts 无压力，实测过；>600 根再考虑降采样 |

---

## 四、待你确认（我不猜）

1. **胜算率口径**：选项 1 混合 / 选项 2 纯个股 / 选项 3 纯市场（我推荐 1）
2. **A4 力度对比形态**：方案 a 信息条（小改动）/ 方案 b 归一化净值曲线副图（大改动）（我推荐 a）
3. **A2 背驰段**：全画不限个数 / 全画但限最近 8 个
4. **A1 视窗默认**：全览（0-100）/ 保持近 45% 但把「所有买卖点」理解为别的诉求（请补充你想看到的）

确认后我按批次编码，每批改完跑 `PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests` 全量（现 270 个）再交付。
