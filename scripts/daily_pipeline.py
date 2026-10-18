#!/usr/bin/env python3
"""每日盘后全流程流水线（供 cron / systemd timer 调用）。

把四个「每日必跑」步骤编排成一个入口，每步独立容错——单步失败不阻断
后续步骤，最后统一汇报退出码（0=全部成功，1=有步骤失败）。

步骤（按依赖顺序）：
  1. 全市场日线扫描      —— 关注圈/池/体系选股的信号源头
  2. 30 分钟增量拉取     —— 只拉关注圈标的，为区间套/p100 验证积累次级别数据
                            （实测 41 分钟，限流 25 秒间隔；失败不影响主链路）
  3. 重建结局样本        —— 体系 v2/v3 的一切都建立在 outcomes 上，
                            新交易日不 rebuild，体系分就是旧口径
  4. 重评关注圈          —— 二买失败/守住只能在后续 K 线上观测，必须主动重评

用法：
    python3 scripts/daily_pipeline.py                   # 全流程
    python3 scripts/daily_pipeline.py --skip-intraday   # 跳过分钟拉取（白天重跑时用）
    python3 scripts/daily_pipeline.py --scope hs300     # 快速验证
    python3 scripts/daily_pipeline.py --steps scan,rebuild   # 只跑指定步骤

cron 示例（每交易日 18:00；分钟拉取建议 17:30 独立跑，这里默认跳过）：
    0 18 * * 1-5 cd /opt/chanpicker && .venv/bin/python scripts/daily_pipeline.py \
        --skip-intraday >> /var/log/chanpicker/pipeline.log 2>&1
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from backend.provider import UNIVERSE_SCOPES  # noqa: E402
from backend.service import ScanService  # noqa: E402
from backend.store import Store  # noqa: E402

MAX_SCAN_WAIT_SECONDS = 3 * 3600
SCAN_POLL_SECONDS = 20


def log(step: str, message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{step}] {message}", flush=True)


def step_scan(service: ScanService, scope: str, force: bool) -> bool:
    """全市场扫描并阻塞等待完成。"""

    try:
        started = service.start_market_scan(scope=scope, force=force)
    except Exception as exc:  # noqa: BLE001 - 单步失败不阻断流水线
        log("scan", f"启动失败：{exc}")
        return False
    run_id = started["run_id"]
    log("scan", f"启动 {run_id}，范围 {started.get('scope_label', scope)}，共 {started['total']} 只")

    waited = 0
    while waited < MAX_SCAN_WAIT_SECONDS:
        status = service.scan_status(run_id)
        state = status.get("status")
        if state in ("finished", "interrupted"):
            hits = status.get("hits") or []
            log("scan", f"{state}：{status.get('message', '')}；命中 {len(hits)} 条")
            return state == "finished"
        time.sleep(SCAN_POLL_SECONDS)
        waited += SCAN_POLL_SECONDS
    log("scan", f"超时（{MAX_SCAN_WAIT_SECONDS}s），任务仍在后台运行")
    return False


def step_intraday(limit: int) -> bool:
    """30 分钟增量拉取（复用 sync_intraday 的实现，进程内调用）。"""

    try:
        import sync_intraday
    except Exception as exc:  # noqa: BLE001
        log("intraday", f"加载 sync_intraday 失败：{exc}")
        return False
    try:
        # sync_intraday.main() 自己解析 argv，改为直接驱动其内部流程：
        # 它的 main 里 load_watchlist_symbols + 分批拉取。这里只传 limit。
        sys.argv = ["sync_intraday.py", "--limit", str(limit)]
        rc = sync_intraday.main()
        log("intraday", f"完成，rc={rc}")
        return rc == 0
    except SystemExit as exc:
        rc = exc.code
        log("intraday", f"结束，rc={rc}")
        return rc in (0, None)
    except Exception as exc:  # noqa: BLE001 - 分钟数据是锦上添花，失败不阻断
        log("intraday", f"失败（不影响主链路）：{exc}")
        return False


def step_rebuild(service: ScanService) -> bool:
    """重建结局样本（含 v2 规则化出场 + alpha + 特征重提取）。"""

    try:
        out = service.rebuild_outcomes("day")
        log("rebuild", out["message"])
        return True
    except Exception as exc:  # noqa: BLE001
        log("rebuild", f"失败：{exc}")
        return False


def step_refresh_watchlist(service: ScanService) -> bool:
    """重评关注圈（二买失败/守住只能在后续 K 线上观测）。"""

    try:
        out = service.refresh_watchlist()
        log("watchlist", out.get("message", "已重评"))
        return True
    except Exception as exc:  # noqa: BLE001
        log("watchlist", f"失败：{exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="每日盘后全流程流水线")
    parser.add_argument("--scope", default="all", choices=list(UNIVERSE_SCOPES), help="扫描范围")
    parser.add_argument("--force", action="store_true", help="忽略本地缓存强制重新取数")
    parser.add_argument("--skip-intraday", action="store_true", help="跳过 30 分钟增量拉取")
    parser.add_argument(
        "--steps",
        default="scan,intraday,rebuild,watchlist",
        help="要执行的步骤（逗号分隔）：scan/intraday/rebuild/watchlist",
    )
    parser.add_argument("--intraday-limit", type=int, default=None, help="分钟拉取上限（调试用）")
    args = parser.parse_args()

    steps = {s.strip() for s in args.steps.split(",") if s.strip()}
    unknown = steps - {"scan", "intraday", "rebuild", "watchlist"}
    if unknown:
        parser.error(f"未知步骤：{unknown}")

    results: dict[str, bool] = {}
    service = ScanService(Store())

    if "scan" in steps:
        results["scan"] = step_scan(service, args.scope, args.force)
    if "intraday" in steps and not args.skip_intraday:
        limit = args.intraday_limit if args.intraday_limit is not None else 10 ** 9
        results["intraday"] = step_intraday(limit)
    elif "intraday" in steps:
        results["intraday"] = True  # --skip-intraday 视为跳过而非失败
    if "rebuild" in steps:
        results["rebuild"] = step_rebuild(service)
    if "watchlist" in steps:
        results["watchlist"] = step_refresh_watchlist(service)

    log("summary", " ".join(f"{k}={'OK' if v else 'FAIL'}" for k, v in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
