# ChanPicker · 缠论 A 股选股系统

以缠论为基座的 A 股日线级别选股系统：**背驰 + 一买 → 关注圈 → 持续跟踪 → 二买提示机会**。

## 架构

```
frontend/index.html        前端单页应用（市场扫描 / 个股查询 / 关注圈 / 三日买点池，ECharts K线结构图）
backend/
  app.py                   FastAPI 服务（REST API + 静态托管前端）
  service.py               业务编排（单只分析 / 全市场扫描任务 / 关注圈）
  provider.py              westock CLI 数据适配（批量拉取 + 限流退避重试 + markdown 解析）
  store.py                 SQLite 持久化（K线缓存 / 信号 / 关注圈 / 扫描任务）
src/chanpicker/
  core.py                  缠论核心引擎（分型 → 笔 → 中枢 → 背驰 → 一买/二买 → 关注圈状态机）
  cli.py                   命令行入口（scan / watch，可脱离服务独立使用）
tests/                     单元测试（引擎逻辑 + 数据解析）
data/chanpicker.db         SQLite 数据库（自动创建）
run.sh                     一键启动脚本
```

## 环境要求

Python **3.9+**（已在 3.9.25 与 3.13 上实测通过；类型标注统一使用 `Optional`/`Union`，不依赖 3.10+ 语法），以及已授权可用的 westock CLI。

## 快速开始

```bash
# 1. 安装并授权数据源（首次）：
#    在 westockdata 技能包目录内执行 bash scripts/setup.sh（macOS/Linux）
westock auth status        # READY=已授权；AUTH_REQUIRED:<url> 则用浏览器打开该链接
westock auth fetch         # 授权后轮询到 TOKEN_READY 即完成（轮询期间不要再跑 auth status）

# 2. 一键启动
./run.sh                   # 等价于: python3 -m venv .venv && pip install -e . && uvicorn backend.app:app

# 3. 打开页面
open http://127.0.0.1:8787
```

页面四个标签：

- **市场扫描**：点击「扫描沪深300成分股」→ 进度条 → 自动列出近30日触发「一买」与「二买」的股票
- **个股查询**：输入代码（如 `sh600519`）→ 是否符合结论 + K线结构图（分型/笔/中枢/一买二买三买标注）
- **关注圈**：所有入圈股票的状态机（待二买 / 弱跟踪 / 机会已触发 / 一买失败）
- **三日买点池**：最近 3 个交易日内触发的买点，按一买池 / 二买池 / 三买池分组展示

## REST API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 服务与数据源健康状态 |
| GET | `/api/analyze?symbol=sh600519` | 单只个股缠论分析（K线+结构+信号+是否符合） |
| POST | `/api/market/scan?scope=hs300` | 启动全市场扫描（后台任务） |
| GET | `/api/market/scan/status` | 扫描进度与命中列表 |
| GET | `/api/market/candidates` | 当前符合的股票（一买/二买/三买分组，近30日） |
| GET | `/api/market/pools?days=3` | 三日买点池（最近 N 个交易日的一买/二买/三买） |
| GET | `/api/watchlist` | 关注圈状态机 |
| GET | `/api/signals/history?kind=FIRST_BUY` | 历史信号查询 |

## 缠论信号口径（日线级别）

- **一买（入圈）**：连续下移低点的下跌段中，末段 MACD 柱面积相对前段衰减（背驰度 > 5%），低点位于最近中枢 ZD 下方，底分型确认次日为信号日
- **二买（机会提示）**：一买后出现向上段，首次回调不破一买低点，回调段力度相对上涨段衰减，底分型确认次日触发
- **三买（趋势延续）**：中枢完成后出现向上离开笔（终点 > 中枢上沿 ZG），其后回调终点仍 > ZG（未跌回中枢），回调力度相对离开笔衰减，底分型确认次日触发
- **失效**：收盘跌破一买低点 → 出圈「一买失败」；入圈 30 根 K 线未出二买 → 降级「弱跟踪」
- 信号按「确认日」计算固化，历史信号不复核改判（防未来函数）

## 数据源

腾讯自选股（westock CLI）：日线前复权（qfq），批量拉取（每批 ≤20 只，限流自动退避重试），SQLite 增量缓存。

## 买点池口径

「三日买点池」的统计窗口是**最近 N 个交易日**（取自本地 K 线缓存的日期集合），不是自然日；数据来自 signals 表（全部已扫描标的），同一标的同一买点类型只保留最近一条。同一次回调可能同时满足二买与三买定义，此时会同时出现在两个池中。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests     # 12 个用例（引擎 + 数据解析）
```

## 免责声明

本系统为技术分析研究工具，所有信号基于公开行情数据与缠论量化规则推导，仅供参考，不构成投资建议。市场有风险，投资需谨慎。过往表现不预示未来收益。
