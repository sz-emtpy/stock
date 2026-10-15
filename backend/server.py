"""生产启动入口：从环境变量读取监听地址、端口与日志级别。

用法：
    CHANPICKER_HOST=127.0.0.1 CHANPICKER_PORT=8787 python -m backend.server

环境变量：
    CHANPICKER_HOST      监听地址，默认 127.0.0.1（生产建议只监听本机，由 Nginx 反代）
    CHANPICKER_PORT      监听端口，默认 8787
    CHANPICKER_LOG_LEVEL 日志级别，默认 info
    CHANPICKER_DB        SQLite 库路径，默认 <项目>/data/chanpicker.db
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.getenv("CHANPICKER_HOST", "127.0.0.1")
    port = int(os.getenv("CHANPICKER_PORT", "8787"))
    log_level = os.getenv("CHANPICKER_LOG_LEVEL", "info")
    # workers 固定为 1：扫描任务的运行状态机与 SQLite 写入都在单进程内保证一致
    uvicorn.run(
        "backend.app:app",
        host=host,
        port=port,
        workers=1,
        log_level=log_level,
        access_log=True,
    )


if __name__ == "__main__":
    main()
