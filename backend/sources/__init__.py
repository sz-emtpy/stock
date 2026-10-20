"""多数据源互补层。

对外的唯一入口是 `registry`：`fetch_bars`（历史+实时合并）与 `fetch_quote`（盘中快照）。
各源模块只负责「按自己的协议把数据取回来并转成 Bar」，不做降级决策——
降级顺序集中在 registry，改策略只改一处。
"""

from . import baostock_source, eastmoney, registry  # noqa: F401
from .base import SourceError, merge_bars  # noqa: F401

__all__ = ["baostock_source", "eastmoney", "registry", "SourceError", "merge_bars"]
