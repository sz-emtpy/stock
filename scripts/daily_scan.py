#!/usr/bin/env python3
"""每日盘后自动扫描入口（供 cron / systemd timer 调用）。

启动一次全市场扫描并阻塞等待完成，完成后打印结果摘要。

用法：
    python3 scripts/daily_scan.py                    # 扫描全部 A 股（默认，约 5576 只）
    python3 scripts/daily_scan.py --scope hs300      # 只扫沪深300，用于快速验证
    python3 scripts/daily_scan.py --force            # 忽略本地缓存强制重取
    CHANPICKER_DB=/data/chanpicker.db python3 scripts/daily_scan.py

cron 示例（每交易日 18:00）：
    0 18 * * 1-5 cd /opt/chanpicker && .venv/bin/python scripts/daily_scan.py >> /var/log/chanpicker/scan.log 2>&1
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.provider import UNIVERSE_SCOPES  # noqa: E402
from backend.service import ScanService  # noqa: E402
from backend.store import Store  # noqa: E402

# 全市场首次扫描实测约 30-50 分钟，等待窗口给足 3 小时
MAX_WAIT_SECONDS = 3 * 3600


def main() -> int:
    parser = argparse.ArgumentParser(description="每日盘后扫描")
    parser.add_argument("--scope", default="all", choices=list(UNIVERSE_SCOPES), help="扫描范围")
    parser.add_argument("--force", action="store_true", help="忽略本地缓存强制重新取数")
    args = parser.parse_args()

    service = ScanService(Store())
    started = service.start_market_scan(scope=args.scope, force=args.force)
    run_id = started["run_id"]
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 扫描启动 {run_id}，"
        f"范围 {started.get('scope_label', args.scope)}，共 {started['total']} 只"
    )

    waited = 0
    while waited < MAX_WAIT_SECONDS:
        status = service.scan_status(run_id)
        state = status.get("status")
        if state in ("finished", "interrupted"):
            hits = status.get("hits") or []
            first = sum(1 for h in hits if h["kind"] == "FIRST_BUY")
            second = sum(1 for h in hits if h["kind"] == "SECOND_BUY")
            label = "扫描完成" if state == "finished" else "扫描中断"
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {label}：{status.get('message', '')}")
            print(f"  命中 {len(hits)} 条（一买 {first} / 二买 {second}）")
            return 0 if state == "finished" else 1
        time.sleep(20)
        waited += 20

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 扫描超时（{MAX_WAIT_SECONDS}s），任务仍在后台运行", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
