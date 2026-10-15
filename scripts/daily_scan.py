#!/usr/bin/env python3
"""每日盘后自动扫描入口（供 cron / systemd timer 调用）。

启动一次全市场扫描并阻塞等待完成，完成后打印结果摘要。

用法：
    python3 scripts/daily_scan.py            # 扫描沪深300
    CHANPICKER_DB=/data/chanpicker.db python3 scripts/daily_scan.py

cron 示例（每交易日 18:00）：
    0 18 * * 1-5 cd /opt/chanpicker && .venv/bin/python scripts/daily_scan.py >> /var/log/chanpicker/scan.log 2>&1
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.service import ScanService  # noqa: E402
from backend.store import Store  # noqa: E402

MAX_WAIT_SECONDS = 3600


def main() -> int:
    service = ScanService(Store())
    started = service.start_market_scan(scope="hs300")
    run_id = started["run_id"]
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 扫描启动 {run_id}，共 {started['total']} 只")

    waited = 0
    while waited < MAX_WAIT_SECONDS:
        status = service.scan_status(run_id)
        if status.get("status") == "finished":
            hits = status.get("hits") or []
            first = sum(1 for h in hits if h["kind"] == "FIRST_BUY")
            second = sum(1 for h in hits if h["kind"] == "SECOND_BUY")
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 扫描完成：{status.get('message', '')}")
            print(f"  命中 {len(hits)} 条（一买 {first} / 二买 {second}）")
            return 0
        time.sleep(20)
        waited += 20

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 扫描超时（{MAX_WAIT_SECONDS}s），任务仍在后台运行", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
