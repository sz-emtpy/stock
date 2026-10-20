# ChanPicker 项目长期记忆

缠论 A 股选股系统。核心链路：背驰 + 一买 → 关注圈 → 二买提示「机会来了」。

## 不可动摇的口径（改代码前先看这里）

1. **买点 vs 卖点语义分离**
   - 买点（一买/二买/三买）用于**选股**，决定 `qualifies`。
   - 卖点（一卖/二卖）只做**持仓风险提示**，走独立字段 `risk_signals` / 独立接口 `/api/market/risk`，**绝不并入 `qualifies`**。
2. **日线是选股主周期**。分钟（m30/m15）只做区间套确认 / 日内择时：
   - 不做全市场扫描（数据源只留约 9 个交易日）；
   - **不改写关注圈状态机**（`WatchlistManager` 只在 `period == "day"` 时被调用）。
3. **防未来函数**：信号一律在分型后的**下一根 K 线**确认（`bar_index = 分型索引 + 1`）；但图表上的「关键点位」锚点用 `anchor_index / anchor_price`（分型本身）。两者是有意分离的，别"修"回去。
4. **图表配色**：涨红跌绿（中国市场惯例）。买点图钉 黄(一买)/绿(二买)/紫(三买)；卖点用**向下箭头** 浅红(一卖)/深红(二卖)。
5. **全市场扫描的剔除口径**：剔除**北交所**（bj 前缀）与**风险警示股**（ST / *ST / SST / S*ST）。数据源 5576 只 → 实际 5028 只。退市整理期个股按「其它都要纳入」保留。改动前先想清楚会不会偷偷缩小范围。
6. **表格排序要走同一套引擎**：表头用 `data-key`/`data-type`/`data-fallback` 声明，空值恒排最后；关注圈默认「二买日，缺失退回一买日」降序，其余按信号日降序。**由 `setTableItems → paintTable` 驱动的表共 9 张**（候选×2、关注圈、自选、池表×5），`table-runs` 是纯展示表不参与排序。改前端表格后跑 `tests/test_table_sort.py` + `tests/test_frontend_contract.py`。
   - **搜索/筛选只过滤视图，绝不能写回 `view.items`**——否则左右切换的顺序会被搜索词改掉（`viewFiltered` 里有注释，`UsabilityContractTest` 有断言）。
   - 分页 `PAGE_SIZE=50`，「显示全部」保留为 `expanded` 旁路。
7. **个股左右切换依赖「入口带来源表」**：`analyzeFrom(symbol, tableId)` 缺了 tableId 就无从按列表顺序切换；切换顺序取自 `sortRows()` 后的顺序而非原始 items。改列表入口或表格渲染后跑 `tests/test_frontend_contract.py` 的 `StockNavigationTest`。
8. **关注圈状态机与出圈口径**（2026-10-16 起）：
   - `OPPORTUNITY` **不是终态**，必须留在 `TRACKING_STATES` 里继续参与判定，否则「二买失败」永远观测不到、二买成功率只能是假的 100%。
   - 二买失败 = 二买确认后 `max_wait_bars`（30 根）内收盘价跌破**二买低点**（`anchor_price`，不是确认 K 线收盘价——后者是任意值，用它会误杀约一半二买）。
   - 一买失败优先级高于二买失败（同时跌破两个低点记 `FAILED`）。
   - 出圈是**软剔除**：`/api/watchlist` 按 `TRACKING_STATES` 过滤，记录留在库里供成功率统计。
   - 一买成功率分母 = 已终结的一买记录；二买只看已确认过二买的记录。
9. **信号 meta 必带 `previous_segment` / `current_segment`**（两段笔的起止索引/价格/方向），前端才能画背驰段。一买/一卖比的是「相隔一段反向笔的两个同向段」（两段**不**相接）；二买/三买/二卖比的是「离开段 vs 紧接其后的回调段」（两段严格相接）。
10. **启动时必须清残留扫描行**：`ScanService.__init__` 调 `store.mark_interrupted_runs()`，把 `scan_runs.status='running'` 判为 `interrupted`。否则进程被 kill 后前端永远轮询一个不会结束的任务。
11. **`bars(period, trade_date)` 覆盖索引是性能命门**（2026-10-16 加）：`recent_trading_dates` / `period_counts` 只按 period 过滤，索引前导列必须是 period。原索引 `(symbol, period, trade_date)` 前导列是 symbol → 全扫 218 万行（8990 ms / 7809 ms），加索引后 0.24 ms / 61 ms，首屏从 12–20 秒回到 0.63 秒。回归见 `tests/test_query_performance.py`。
12. **补名一律走 `store.symbol_names()` 批量**，禁止在循环里调 `symbol_name()`：候选近万条 + 关注圈四千条，逐行就是 N+1（换 MySQL 会放大成秒级）。批量按 500 分批（SQLite 变量上限 999）。
13. **`bar_index` / `anchor_index` 都不是稳定量，禁止用作唯一键或做算术**（2026-10-16）：
    - 它们是「K 线窗口内的相对位置」。用户传 `days=600` vs `days=400`，同一个信号下标不同 → 原 `UNIQUE(symbol,kind,anchor_index,period)` 拦不住，库内曾有 15416 条重复（占 36%）。信号身份已改为 `Store.SIGNAL_IDENTITY_COLUMNS = (symbol, kind, period, trade_date, price, anchor_price)`。
    - **配对一买↔二买的间隔也必须用交易日序**（信号日在 K 线序列里的位置），不能相减 bar_index——两次不同扫描写下的信号相减会算出错误间隔。
14. **成功率有两套口径，勿混**：`watchlist_stats()` 看全市场（关注圈状态流转）；`symbol_success_rate(symbol)` 看个股自身（signals 表按时间线重新配对）。个股口径样本 < 3 次**不给百分比**，只给明细。
15. **背驰判定按缠论原文三条件**（`src/chanpicker/effort.py`，2026-10-16 起）：① DIF/DEA 不创新高 ② 同色柱逐片面积变小 ③ 均线有向偏离收窄；任一成立即背驰，②③同时为**标准背驰**（p368）。
    - **MACD 柱 = DIF − DEA，不乘 2**。旧 `macd_histogram()` 里的 `*2` 已移除——那只是软件显示需要，乘了会让所有面积阈值对不上。
    - 柱必须**按同色分片**累加（p125「肯定是片和片比」），零柱归入当前片、不开新片；末片按 p295 折算 ×2。
    - 均线偏离必须**带符号**：上涨段取正向 max、下跌段取负向 min。写成 `max(正向, -负向)` 会让 `ma_weaker` 恒为 False（曾真实发生过）。
    - `min_macd_area` 回退时三个条件要**统一**换基准：面积→价格差、DIF→±价格差、均线→0（该条件下无信息，判 True 等于凭空造背驰）。
    - 阈值语义一律「正值更严格」：`area_threshold = prev * (ratio - margin)`。写成相加会让 margin 越大越容易判弱，与参数名相反。
16. **中枢与买卖点必须在同一层级**（2026-10-16，p152/p1351）：原文的中枢是「至少三个连续**次级别走势类型**重叠」，且「笔不能构成中枢」。所以 `ChanConfig.use_canonical_zhongshu=True` 时中枢**和全部买卖点**都改在段层面（`structure.TrendLeg`）判定。早期版本中枢用段、买卖点用笔，两套索引对不上——中枢终点 index 11、向上突破笔起点 index 10，三买永远匹配不上，信号**静默消失**（不报错，只是没有）。
17. **一买/一卖的两道收紧缺一不可**（p116 + p368）：段粒度比笔粗，不收紧时一买涨到 **14.2 个/股**；加「只取标准背驰（②③同时弱）」+「只取趋势背驰（背驰段前须有两个依次同向中枢）」后回到 **2.3 个/股**（旧笔口径 2.4）。
18. **`_migrate_legacy` 的去重键不能含 `period`**：那一段迁移时才给老表补 `period` 列，去重的 `GROUP BY ... period` 会报 `no such column: period`。回归见 `tests/test_symbol_success_rate.py::LegacyMigrationDuplicateTest`。
19. **买卖点的「道」在 `src/chanpicker/masystem.py`**（2026-10-16）：一买=男上位+**最后一吻后**+背驰下跌+破中枢ZD；二买=女上位+**第一吻后**+下跌+不破一买低点；一卖/二卖严格镜像（p84「卖就反过来就可以了」）。`ChanConfig.use_ma_system` / `enforce_ma_rules` / `ma_min_male_kisses`。
    - **`ma_min_male_kisses` 数的是「本轮」吻数不是全程累计**（用 `kiss_run_count_before`）。用累计数会让第一轮男上位的第一个吻之后被误判成「已经过多吻」，把该拒的信号放行——这是真实踩过的坑。
    - **判定点不能落在「尚未结束的缠绕内部」**（`kiss_covering`）。只查 `kisses_before` 会退回更早的吻，把仍在缠绕中的位置误放行——一买/二买/一卖/二卖四处都要查。
    - 原文 p98 要求 `min_kisses=2`，默认放宽为 1（星注留了「一吻若能与前面走势重组出同级别箱体也可成趋势一买」的口子）。
    - 三买**不纳入**均线规则（p208 只讲中枢定理，没给均线定性）。
20. **30 分钟做不了 p100 买点定律验证——数据量是硬约束**（2026-10-16 实测）：m30 只有 **56~80 根 / 7~10 个交易日**（数据源只保留约 9 天）。段口径下一买需要「中枢(3段) + 趋势背驰(两个同向中枢) + 背驰段」≈24 根≈3 天，理论够但概率极低——茅台 7 只股票实测 **0 个 30分钟一买**（全默认口径、放开趋势背驰、放开标准背驰、连中枢要求一起放开，都还是 0）。这是 p116「没有趋势，没有背驰」在极短周期上的真实表现，不是缺陷。
    - 关注圈 575 只含二买的标的中，只有 **62 只的二买日落在 m30 数据窗口内**（其余在 9 天之前，数据源已无留存）。
21. **`min_legs_bars` 自适应（`_resolve_legs_bars`）当前数据量下基本是空转**：`day 400→3`、`m30 80→3`，要 800 根以上才变成 7、1200 根变 8。所以它不是 30 分钟无信号的解药，别指望调它能解决短周期问题。
22. **westock 分钟数据的真实深度已探明（2026-10-16 实测，别再重复探测）**：
    - 硬上限：`m5` 10 天、`m15` 20 天、`m30` 30 天——超出直接报「查询日期跨度不能超过 N 天」
    - **但实际只有约 10 个交易日**：请求 `m30 --start 2026-09-17 --end 2026-10-16`（30 天窗口）只回 **81 根**，覆盖 **2026-09-28 ~ 2026-10-16**。30 天是「允许查询的范围」，不是「能拿到的数据深度」。
    - 连续请求会触发限流「请求过于频繁」，探测时需间隔 **25 秒以上**。
    - 分钟 K 线**强制不复权**（`--fq` 对 m1/m5/m15/m30/m60/m120 无效），这点与日线（qfq）不同，做跨周期对比时要注意。
- **别把它当「补历史数据」的工具**：只能让未来的二买具备 p100 验证条件（T+3 起），补不回 9-28 之前的历史。
23. **数据库体积增长可控，查询不受影响（2026-10-16 实测）**：
    - 当前 **520MB / 218 万 bars 行**（day 218万 + m30 1760 + m15 240），每根约 249 字节
    - 增长速率：日线 5028 根/交易日 + m30 增量 15824 根/交易日 ≈ **2.1 万根/天 ≈ 5MB/天**
    - 外推：1 年 1.7GB / 4 年 5.3GB / 8 年 10GB —— SQLite 这个量级毫无压力
    - **性能关键：所有业务查询都走索引**（`idx_signals_period`、`idx_bars_period`、
      `idx_bars_period_date`、`idx_signals_period_kind_date`），只触碰索引覆盖的行，
      **耗时与总库体积无关**。实测个股取 400 根 8.5ms、候选表 59ms、关注圈 41ms
    - 真正的全表扫描（`COUNT(*) FROM bars`）只要 79ms；信号统计不走全表
    - `scan_hits` 每年约 +31 万行（每天 1 次扫描），可忽略
    - **结论：暂不需要分库或归档**。真到 10GB+ 再考虑按 trade_date 分区或归档冷数据
24. **验证 localhost 服务一律加 `curl --noproxy '*'`**：本机 `HTTP_PROXY=127.0.0.1:60789`
    会劫持所有 localhost 请求，返回的 502 是代理的、不是后端的。不知道这点会误判成「服务挂了」，
    白白排查半天。
25. **线上慢 ≠ 数据库慢：先量带宽，再谈换库（2026-10-20 实测 stock.temeu.top）**：
    - 架构是 **nginx → uvicorn(127.0.0.1:8787) → SQLite**（`DEPLOY.md:340`）。
    - 线上出口实测仅 **110~130 KB/s（≈1Mbps）**：`/api/market/candidates` 体积 7.00MB，
      **TTFB 4.81s 就出首字节，总耗时 63.17s** —— 58 秒纯下载。`echarts.min.js` 1.03MB 单文件 7.83s。
    - **nginx 未开 gzip**（有 `Vary: Accept-Encoding` 却无 `Content-Encoding`）。gzip 实测
      candidates 6.43MB→0.36MB（**18.1×**）、echarts→0.33MB（3.1×）。**nginx 默认 `gzip_types`
      只含 `text/html`，必须显式加 `application/json`**，否则 API JSON 不压缩。
    - 首页 8 个请求**共用一把 SQLite RLock 串行**：并发时 `risk` 0.22s→3.98s、
      `scan/history` 0.005s→5.21s。首页总耗时≈串行累加，不是取最大值。
    - `candidates` 6s 里 **3.8s 是 `symbol_success_rates(1952 只)`**；`load_close_series` 760ms（78 万行）、
      `signals_for_symbols` 322ms（每条 `json.loads(meta_json)`，胜率用不到 meta）。
      **其中 395 只纯三买标的白算**（THIRD_BUY 的 `stat_key=None` 直接显示「—」）。
    - **真索引缺口**：`signals_for_symbols` 走 `idx_signals_period` + `USE TEMP B-TREE FOR ORDER BY`，
      `symbol IN` 没命中。缺 `signals (symbol, period, trade_date)`。
    - 响应字段：`system_hits` 占 **78.2%**（5.02MB）、`meta` 11.7%（0.75MB，候选表不用）。
    - **结论：不换 MySQL/PG**。7MB JSON × 110KB/s 与引擎无关，换库只会再加网络往返。
      该换的时机：多实例并发写 / >50GB / 跨机部署 / 需要行级锁事务。
    - 方案见 `改造方案-首页查询性能优化.md`（P0 gzip → 4-6s；P0+P1 → 2-3s）。
26. **前端折叠/吸顶的三条不可动摇约束（2026-10-20）**：
    - **折叠只准用 CSS 隐藏，禁止给表格包一层容器**：`table` 必须是 `.card` 的直接子元素，
      否则移动端 `.card > table { display:block; overflow-x:auto }` 静默失效。
      收起规则 `.card.collapsible.collapsed:not(.head-in-toolbar) > *:not(h2)`。
    - **必须 `!important`**：`.empty` 空状态提示的 display 是 JS 写的 inline style，
      普通选择器盖不住，会留在收起后的标题下面。
    - **标题在 `.toolbar` 里的区块要标 `head-in-toolbar`**（fav/watch/pick/sys）：
      A 型规则会连 toolbar（含标题和刷新按钮）一起隐藏，且 B 型规则不会恢复它。
      事件委托必须用 `closest('.card.collapsible')`，不能取 `h2.parentElement`。
    - **表头吸顶**：`top: var(--sticky-top)` = headerH+navH（实测 115px），`z-index:4` 必须 < nav(9)。
      `border-collapse: collapse` 下 sticky th 的 border 会丢 → 底线用 `box-shadow: inset 0 -1px 0`。
      **<860px 必须关掉**（table 变 block 滚动容器后 sticky 会错位）。
    - **图表区块展开必须 `refreshChartsIn()`**，否则 ECharts 在 display:none 期间被量成 0×0。
    - 全部 24 区块清单与坑见 `.workbuddy/memory/2026-10-20.md`；回归
      `tests/test_frontend_contract.py::CollapsibleAndStickyContractTest`（新增区块要同步改 `COLLAPSIBLE_KEYS`）。

## 数据源事实（本机实测，别凭直觉改）

- 周期：`day` ≈400 根；`m30` ≈72 根；`m15` ≈96 根（分钟仅保留约 1 个月）。
- **分钟周期 `--limit` 无效**，必须用 `--start/--end`。
- 连续取数触发限流（「请求过于频繁」），已内置 3 次退避重试（20s/40s）。
- 每次改动取数逻辑都要考虑限流，不要把单只查询做成高频轮询。

## 工程约束

- **`Store` 的 sqlite 连接非线程安全**：FastAPI 同步端点跑在线程池里，所有访问 `self._conn` 的方法都必须带 `@_sync`（RLock 串行化）。新增任何 Store 方法都要加。已用 `tests/test_backend_store.py` 的并发用例锁死这个约束。
- 数据库路径优先 `CHANPICKER_DB`，默认 `data/chanpicker.db`。**改表结构前先 `cp` 备份**；迁移必须幂等（参考 `Store._migrate_legacy` 的 `PRAGMA index_list` 判定法）。
- Python 3.9+（用户服务器是 3.9.25）：不要用 `X | None` 这类 PEP 604 语法——FastAPI 在注册路由时会 `get_type_hints` 求值，3.9 会直接崩。统一用 `Optional[X]` / `Union[X, Y]`。
- **页签显隐只有 `switchTab()` 一个入口**：CSS 是 `section.tab{display:none}`，导航按钮与 `<section>` 的 `active` 必须成对且指向同一页签，缺一个就整页空白（数据已到位却看不见）。改前端页签相关代码后跑 `tests/test_frontend_contract.py`。
- **`/api/*` 一律 `Cache-Control: no-store`**（`app.py` 的 http middleware）。行情/信号是强时效数据，浏览器对无 Cache-Control 的 GET 会启发式缓存，实测出现过「同一秒两次 sector-heat 拿到不同结果，第二次是缓存」。
- **股票代码必须规范化**：后端只认带 `sh/sz/bj` 前缀的 6 位代码；前端 `normalizeSymbolInput()` 负责把 `600519` 补成 `sh600519`。不加会走数据源 3 次退避重试（20s+40s），用户干等 60 秒才看到错误。
- 写单测的两个陷阱：判 class 要取 `class="..."` 的**值**再 split（不能直接 split 整段属性）；`assertRegex` 第三个位置参数是 **msg 不是 flags**，行首锚定要传 `re.compile(p, re.M)`。
- 本地验证：`PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests`；空手起服务用 `PYTHONPATH=src:. .venv/bin/python -m uvicorn backend.app:app --port 8787`。
- 无头浏览器验证：Chrome 需加 `--no-sandbox`（否则本机沙箱下起不来）；Node 22 自带全局 WebSocket，可直接连 CDP，不必装 puppeteer。
- **前端改动不要只看契约测试**：契约测试只断言 DOM id/函数名存在，接线错了它照样过（点按钮没反应）。必须起服务 + CDP 实测交互。可用套路：`Page.navigate` → `Runtime.evaluate` 点按钮/改 input → 轮询 `state.analysis` 等就绪 → 读 DOM 断言。

## 工作方式

- **方案类需求先出清单/方案，等明确「开始编码」指令再动代码。**
- 改完必须编译 + 跑测试再交付；后端编译需绕过沙箱（target/ 写入受限）。
27. **通达信数据源实测：K 线拿不到（2026-10-20，别再重复探测）**：
    - pytdx 1.72 + mootdx 0.11.4，连 5 台公开服务器（3 台连通且 setup 成功）。
    - **能取**：分时（65 条）、证券数量（沪市 27633）、除权除息（45 条）。
    - **取不到**：日线/30分/15分/5分 K 线全部 0 根；实时报价 0 条。三台服务器一致，
      换 market / category / 指数 K 线均 0 → 不是单台问题，也不是参数问题。
    - **调用坑**：`mootdx.bars(symbol, frequency, start, offset)` —— 是 `start` + `offset(数量)`，
      没有 `count`；误传 `offset=0` 会永远返回 0 条。`pytdx` 是 `get_security_bars(category, market, code, start, count)`。
    - **本地 vipdoc 文件方案**需要先装通达信客户端下载数据；用户没装。
    - **需求拆分**：「盘中看最新 K 线」用现有 westock 就能做（m30 盘中可取，只是历史仅 10 天）；
      真正卡住的只有「历史深度」。别为一个已有能力去冒整个数据源改造风险。
28. **数据源候选实测对比（2026-10-20，选源前先看这条）**：
    | 源 | 日线 | m30 深度 | 盘中实时 | 分钟复权 |
    |---|---|---|---|---|
    | westock（现用） | 400 根 | **10 天** | ✅ | ❌ 强制不复权 |
    | 通达信 pytdx/mootdx | ❌ 拿不到 | ❌ | ❌ | — |
    | 东财 HTTP | 6024 根/25年 | 31 天 | ✅ 当天正在形成的 K 线 | ❌ fqt 无效 |
    | **baostock** | 2863 根/11年 | **13152 根/6.8 年** | ❌ T+1 | ✅ 1后/2前/3不复权全支持 |
    - **东财**：`push2his.eastmoney.com/api/qt/stock/kline/get`，必须带 UA+Referer；
      `klt`=1/5/15/30/60/101(日)，`fqt`=0/1/2。前复权已验证真实（差 28.02 元 = 每股分红）。
      分钟 31 天是服务器硬限制，改 `beg` 或翻页都取不到更早。连续 8 只后断连，需节流。
    - **baostock**：`bs.login()` 免密；日线 fields **不能带 `time`**（会报「日线指标参数传入错误」）；
      m30/m15 从 2020 起、分钟复权真实生效 → 是解决「分钟历史太浅」的最优免费源。
    - **推荐组合**：baostock 做历史（T+1，收盘后跑）+ 东财做盘中实时 + westock 兜底。
    - 换源后**历史分钟信号会整体重算**（窗口变长 → 中枢/背驰判定不同），需重扫并接受结论变化。
29. **多源互补架构（2026-10-20 落地）**：`backend/sources/` + `provider.fetch_bars` 内部换源，
    对外签名不变（service 与测试的大量 `mock.patch.object(service_module,"fetch_bars")` 不受影响）。
    - **日线绝不跨源合并**：东财与 baostock 前复权基准不同（400 根里 71 根偏差 >0.5%、最大 0.99%），
      混用会在除权处留断层、让笔/中枢判定静默翻转。日线只取单一源（优先东财，失败整段换 baostock）。
    - **分钟可以合并**：两边都不复权，实测重叠 248 根、冲突 0、偏差 0.0077%。
    - **分钟强制不复权**（`effective_fq`）：与已落库的历史 signals 可比，别改成 qfq。
    - **日线盘中不补当天**：未收盘日线算出的中枢收盘后可能全不成立（`provider._prune_stale_signals`
      就是清这个的），从源头剔除。
    - **`needs_tail` 用数据判断不用时钟**：baostock 末条已覆盖今天就不打东财。
    - **东财必须熔断**（失败冷却 60s）：超时 5s×2 次，否则失败时一次请求拖 45s+。
    - **测试进程默认关闭多源**（`"unittest" in sys.modules`）：防漏 mock 时真联网。
      显式设 `CHANPICKER_SOURCE` 时以显式值为准；`=westock` 是回滚开关。
    - **baostock 必须按 `days` 反推起始日**：固定从 2020 拉全量再裁，取 500 根 m30 要 26s，反推后 2s。
    - 东财 `lmt` 会被忽略（要 10 根回 2492 根），必须自己截断；报价必须请求 `f13` 否则深市票标成 sh。
    - 补历史：`scripts/sync_history.py`（默认约 1 年，`--days 0` 全量约 13 小时）。
    - 诊断：`/api/market/sources`（源状态 + 熔断 + 最近取数报告）、`/api/market/live`（盘中快照）。
