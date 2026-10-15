from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Union


class SignalKind(str, Enum):
    FIRST_BUY = "FIRST_BUY"
    SECOND_BUY = "SECOND_BUY"
    THIRD_BUY = "THIRD_BUY"
    FIRST_BUY_FAILED = "FIRST_BUY_FAILED"
    WEAK_TRACK = "WEAK_TRACK"


BUY_KINDS = (SignalKind.FIRST_BUY, SignalKind.SECOND_BUY, SignalKind.THIRD_BUY)


class WatchState(str, Enum):
    WAIT_SECOND_BUY = "WAIT_SECOND_BUY"
    WEAK_TRACK = "WEAK_TRACK"
    OPPORTUNITY = "OPPORTUNITY"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Bar:
    symbol: str
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError(f"{self.symbol} {self.date}: high must be >= low")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(f"{self.symbol} {self.date}: open/close must be inside high/low")


@dataclass(frozen=True)
class Fractal:
    index: int
    date: str
    kind: str
    price: float


@dataclass(frozen=True)
class Stroke:
    start: Fractal
    end: Fractal

    @property
    def direction(self) -> str:
        return "up" if self.start.kind == "bottom" and self.end.kind == "top" else "down"

    @property
    def start_index(self) -> int:
        return self.start.index

    @property
    def end_index(self) -> int:
        return self.end.index

    @property
    def high(self) -> float:
        return max(self.start.price, self.end.price)

    @property
    def low(self) -> float:
        return min(self.start.price, self.end.price)

    @property
    def price_delta(self) -> float:
        return abs(self.end.price - self.start.price)


@dataclass(frozen=True)
class Zhongshu:
    start_index: int
    end_index: int
    zd: float
    zg: float

    @property
    def valid(self) -> bool:
        return self.zd <= self.zg


@dataclass(frozen=True)
class Signal:
    symbol: str
    kind: SignalKind
    date: str
    bar_index: int
    price: float
    anchor_index: int
    anchor_price: float
    message: str
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScanResult:
    symbol: str
    bars: int
    fractals: list[Fractal]
    strokes: list[Stroke]
    zhongshu: list[Zhongshu]
    signals: list[Signal]

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "bars": self.bars,
            "fractals": [asdict(item) for item in self.fractals],
            "strokes": [
                {
                    "start": asdict(item.start),
                    "end": asdict(item.end),
                    "direction": item.direction,
                    "low": item.low,
                    "high": item.high,
                }
                for item in self.strokes
            ],
            "zhongshu": [asdict(item) for item in self.zhongshu],
            "signals": [signal_to_dict(item) for item in self.signals],
        }


@dataclass(frozen=True)
class ChanConfig:
    min_stroke_gap: int = 4
    min_divergence_score: float = 0.05
    min_second_buy_score: float = 0.0
    min_third_buy_score: float = 0.0
    min_macd_area: float = 1e-9
    require_zhongshu_for_first_buy: bool = True
    max_wait_bars: int = 30
    cooldown_bars: int = 60
    min_pullback_ratio: float = 0.20
    max_pullback_ratio: float = 0.90


@dataclass
class WatchItem:
    symbol: str
    state: WatchState
    first_buy_date: str
    first_buy_index: int
    first_buy_price: float
    first_buy_low: float
    last_update_date: str
    second_buy_date: Optional[str] = None
    second_buy_price: Optional[float] = None
    exit_date: Optional[str] = None
    exit_reason: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["state"] = self.state.value
        return data

    @staticmethod
    def from_dict(data: dict) -> "WatchItem":
        copied = dict(data)
        copied["state"] = WatchState(copied["state"])
        return WatchItem(**copied)


def load_bars_csv(path: Union[str, Path], symbol: Optional[str] = None) -> list[Bar]:
    """Load OHLCV bars from CSV.

    Required columns: date, open, high, low, close. Optional columns: symbol,
    volume, amount. Bars are sorted by date ascending after loading.
    """

    csv_path = Path(path)
    with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{csv_path}: empty CSV")
        lower_map = {name.lower(): name for name in reader.fieldnames}
        required = {"date", "open", "high", "low", "close"}
        missing = sorted(required - set(lower_map))
        if missing:
            raise ValueError(f"{csv_path}: missing required columns: {', '.join(missing)}")

        bars: list[Bar] = []
        for row_number, row in enumerate(reader, start=2):
            row_symbol = symbol or row.get(lower_map.get("symbol", ""), "") or csv_path.stem
            try:
                bars.append(
                    Bar(
                        symbol=row_symbol,
                        date=row[lower_map["date"]],
                        open=float(row[lower_map["open"]]),
                        high=float(row[lower_map["high"]]),
                        low=float(row[lower_map["low"]]),
                        close=float(row[lower_map["close"]]),
                        volume=float(row.get(lower_map.get("volume", ""), 0) or 0),
                        amount=float(row.get(lower_map.get("amount", ""), 0) or 0),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{csv_path}:{row_number}: invalid OHLCV row: {exc}") from exc

    if not bars:
        raise ValueError(f"{csv_path}: no bars")
    return sorted(bars, key=lambda item: item.date)


def signal_to_dict(signal: Signal) -> dict:
    data = asdict(signal)
    data["kind"] = signal.kind.value
    return data


def signal_from_dict(data: dict) -> Signal:
    copied = dict(data)
    copied["kind"] = SignalKind(copied["kind"])
    return Signal(**copied)


def dumps_json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


class ChanEngine:
    """Pure-Python daily Chan theory MVP.

    This engine intentionally keeps the first implementation dependency-free. It
    computes fractals, strokes, simple three-stroke centers, MACD effort, first
    buy and second buy signals. The signal dates use the confirmation bar after
    a bottom fractal, so historical scans do not use the fractal bar itself as
    an already-known signal.
    """

    def __init__(self, config: Optional[ChanConfig] = None) -> None:
        self.config = config or ChanConfig()

    def scan(self, bars: Sequence[Bar]) -> ScanResult:
        ordered = self._validate_bars(bars)
        symbol = ordered[0].symbol
        hist = macd_histogram([bar.close for bar in ordered])
        fractals = normalize_fractals(detect_fractals(ordered))
        strokes = build_strokes(fractals, self.config.min_stroke_gap)
        centers = detect_zhongshu(strokes)
        first_buy = self._detect_first_buy(ordered, strokes, centers, hist)
        second_buy = self._detect_second_buy(ordered, strokes, hist, first_buy)
        third_buy = self._detect_third_buy(ordered, strokes, centers, hist)
        signals = sorted(first_buy + second_buy + third_buy, key=lambda item: (item.bar_index, item.kind.value))
        return ScanResult(symbol=symbol, bars=len(ordered), fractals=fractals, strokes=strokes, zhongshu=centers, signals=signals)

    @staticmethod
    def _validate_bars(bars: Sequence[Bar]) -> list[Bar]:
        if len(bars) < 8:
            raise ValueError("at least 8 bars are required")
        ordered = sorted(bars, key=lambda item: item.date)
        symbols = {bar.symbol for bar in ordered}
        if len(symbols) != 1:
            raise ValueError(f"scan expects one symbol, got {sorted(symbols)}")
        return ordered

    def _detect_first_buy(
        self,
        bars: Sequence[Bar],
        strokes: Sequence[Stroke],
        centers: Sequence[Zhongshu],
        hist: Sequence[float],
    ) -> list[Signal]:
        signals: list[Signal] = []
        down_strokes = [stroke for stroke in strokes if stroke.direction == "down" and stroke.end.kind == "bottom"]
        for previous, current in zip(down_strokes, down_strokes[1:]):
            if current.end.price >= previous.end.price:
                continue
            previous_effort = self._stroke_effort(previous, hist)
            current_effort = self._stroke_effort(current, hist)
            if previous_effort <= 0:
                continue
            score = (previous_effort - current_effort) / previous_effort
            if score < self.config.min_divergence_score:
                continue

            center = latest_center_before(centers, current.end.index)
            if self.config.require_zhongshu_for_first_buy:
                if center is None or current.end.price > center.zd:
                    continue

            confirmation_index = current.end.index + 1
            if confirmation_index >= len(bars):
                continue
            confirmation = bars[confirmation_index]
            signals.append(
                Signal(
                    symbol=confirmation.symbol,
                    kind=SignalKind.FIRST_BUY,
                    date=confirmation.date,
                    bar_index=confirmation_index,
                    price=confirmation.close,
                    anchor_index=current.end.index,
                    anchor_price=current.end.price,
                    message="背驰后一买成立，纳入关注圈并等待二买确认",
                    meta={
                        "divergence_score": round(score, 6),
                        "previous_effort": round(previous_effort, 6),
                        "current_effort": round(current_effort, 6),
                        "zhongshu_zd": None if center is None else center.zd,
                        "zhongshu_zg": None if center is None else center.zg,
                    },
                )
            )
        return dedupe_signals(signals)

    def _detect_second_buy(
        self,
        bars: Sequence[Bar],
        strokes: Sequence[Stroke],
        hist: Sequence[float],
        first_buy_signals: Sequence[Signal],
    ) -> list[Signal]:
        signals: list[Signal] = []
        for first_buy in first_buy_signals:
            up_stroke = next(
                (
                    stroke
                    for stroke in strokes
                    if stroke.direction == "up" and stroke.start.index >= first_buy.anchor_index
                ),
                None,
            )
            if up_stroke is None or up_stroke.price_delta <= 0:
                continue
            pullback = next(
                (
                    stroke
                    for stroke in strokes
                    if stroke.direction == "down"
                    and stroke.start.index >= up_stroke.end.index
                    and stroke.end.kind == "bottom"
                ),
                None,
            )
            if pullback is None:
                continue
            if pullback.end.price <= first_buy.anchor_price:
                continue

            retracement = (up_stroke.end.price - pullback.end.price) / up_stroke.price_delta
            if not (self.config.min_pullback_ratio <= retracement <= self.config.max_pullback_ratio):
                continue

            up_effort = self._stroke_effort(up_stroke, hist)
            pullback_effort = self._stroke_effort(pullback, hist)
            if up_effort <= 0:
                continue
            score = (up_effort - pullback_effort) / up_effort
            if score < self.config.min_second_buy_score:
                continue

            confirmation_index = pullback.end.index + 1
            if confirmation_index >= len(bars):
                continue
            confirmation = bars[confirmation_index]
            signals.append(
                Signal(
                    symbol=confirmation.symbol,
                    kind=SignalKind.SECOND_BUY,
                    date=confirmation.date,
                    bar_index=confirmation_index,
                    price=confirmation.close,
                    anchor_index=pullback.end.index,
                    anchor_price=pullback.end.price,
                    message="二买确认，关注圈机会触发",
                    meta={
                        "first_buy_date": first_buy.date,
                        "first_buy_low": first_buy.anchor_price,
                        "retracement": round(retracement, 6),
                        "second_buy_score": round(score, 6),
                        "up_effort": round(up_effort, 6),
                        "pullback_effort": round(pullback_effort, 6),
                    },
                )
            )
        return dedupe_signals(signals)

    def _detect_third_buy(
        self,
        bars: Sequence[Bar],
        strokes: Sequence[Stroke],
        centers: Sequence[Zhongshu],
        hist: Sequence[float],
    ) -> list[Signal]:
        """三买：中枢完成后向上离开，回调不跌回中枢上沿 ZG。

        工程化判定（日线级别）：
        1. 存在一个有效中枢 [ZD, ZG]；
        2. 中枢之后出现向上笔，且笔终点价格 > ZG（有效离开）；
        3. 其后出现向下回调笔，回调终点价格 > ZG（未跌回中枢区间）；
        4. 回调力度相对离开力度衰减（MACD 柱面积比，阈值 min_third_buy_score）；
        5. 以回调底分型之后的一根 K 线为确认日，避免未来函数。
        """

        signals: list[Signal] = []
        for center in centers:
            breakout = next(
                (
                    stroke
                    for stroke in strokes
                    if stroke.direction == "up"
                    and stroke.start_index >= center.end_index
                    and stroke.end.price > center.zg
                ),
                None,
            )
            if breakout is None:
                continue
            pullback = next(
                (
                    stroke
                    for stroke in strokes
                    if stroke.direction == "down"
                    and stroke.start_index >= breakout.end_index
                    and stroke.end.kind == "bottom"
                ),
                None,
            )
            if pullback is None:
                continue
            if pullback.end.price <= center.zg:
                continue
            if pullback.end.price >= breakout.end.price:
                continue

            breakout_effort = self._stroke_effort(breakout, hist)
            pullback_effort = self._stroke_effort(pullback, hist)
            if breakout_effort <= 0:
                continue
            score = (breakout_effort - pullback_effort) / breakout_effort
            if score < self.config.min_third_buy_score:
                continue

            confirmation_index = pullback.end.index + 1
            if confirmation_index >= len(bars):
                continue
            confirmation = bars[confirmation_index]
            signals.append(
                Signal(
                    symbol=confirmation.symbol,
                    kind=SignalKind.THIRD_BUY,
                    date=confirmation.date,
                    bar_index=confirmation_index,
                    price=confirmation.close,
                    anchor_index=pullback.end.index,
                    anchor_price=pullback.end.price,
                    message="中枢后回调不破中枢上沿，三买成立",
                    meta={
                        "third_buy_score": round(score, 6),
                        "breakout_effort": round(breakout_effort, 6),
                        "pullback_effort": round(pullback_effort, 6),
                        "zhongshu_zd": center.zd,
                        "zhongshu_zg": center.zg,
                        "breakout_high": breakout.end.price,
                    },
                )
            )
        return dedupe_signals(signals)

    def _stroke_effort(self, stroke: Stroke, hist: Sequence[float]) -> float:
        left, right = sorted((stroke.start.index, stroke.end.index))
        macd_area = sum(abs(value) for value in hist[left : right + 1])
        if macd_area >= self.config.min_macd_area:
            return macd_area
        return stroke.price_delta


class WatchlistManager:
    def __init__(self, config: Optional[ChanConfig] = None) -> None:
        self.config = config or ChanConfig()

    def update(self, bars: Sequence[Bar], signals: Sequence[Signal], existing: Iterable[WatchItem] = ()) -> list[WatchItem]:
        ordered = ChanEngine._validate_bars(bars)
        symbol = ordered[0].symbol
        current_bar = ordered[-1]
        current_index = len(ordered) - 1
        items = [item for item in existing if item.symbol == symbol]
        active = next((item for item in items if item.state in {WatchState.WAIT_SECOND_BUY, WatchState.WEAK_TRACK}), None)

        first_buy_signals = [signal for signal in signals if signal.kind == SignalKind.FIRST_BUY]
        if active is None and first_buy_signals and not self._in_cooldown(items, first_buy_signals[-1].bar_index):
            first_buy = first_buy_signals[-1]
            active = WatchItem(
                symbol=symbol,
                state=WatchState.WAIT_SECOND_BUY,
                first_buy_date=first_buy.date,
                first_buy_index=first_buy.bar_index,
                first_buy_price=first_buy.price,
                first_buy_low=first_buy.anchor_price,
                last_update_date=current_bar.date,
                meta=dict(first_buy.meta),
            )
            items.append(active)

        if active is None:
            return items

        active.last_update_date = current_bar.date
        second_buy = next(
            (
                signal
                for signal in signals
                if signal.kind == SignalKind.SECOND_BUY and signal.bar_index >= active.first_buy_index
            ),
            None,
        )
        if current_bar.close < active.first_buy_low:
            active.state = WatchState.FAILED
            active.exit_date = current_bar.date
            active.exit_reason = "跌破一买低点，关注失效"
        elif second_buy is not None:
            active.state = WatchState.OPPORTUNITY
            active.second_buy_date = second_buy.date
            active.second_buy_price = second_buy.price
            active.exit_date = second_buy.date
            active.exit_reason = "二买确认，机会触发"
            active.meta.update(second_buy.meta)
        elif current_index - active.first_buy_index > self.config.max_wait_bars:
            active.state = WatchState.WEAK_TRACK
            active.exit_reason = f"超过 {self.config.max_wait_bars} 根K线未出现二买，降级弱跟踪"
        return items

    def _in_cooldown(self, items: Sequence[WatchItem], candidate_index: int) -> bool:
        terminal = [item for item in items if item.state in {WatchState.FAILED, WatchState.OPPORTUNITY}]
        if not terminal:
            return False
        latest = max(terminal, key=lambda item: item.first_buy_index)
        return candidate_index - latest.first_buy_index < self.config.cooldown_bars


def detect_fractals(bars: Sequence[Bar]) -> list[Fractal]:
    fractals: list[Fractal] = []
    for index in range(1, len(bars) - 1):
        previous, current, following = bars[index - 1], bars[index], bars[index + 1]
        if current.high > previous.high and current.high > following.high and current.low > previous.low and current.low > following.low:
            fractals.append(Fractal(index=index, date=current.date, kind="top", price=current.high))
        elif current.low < previous.low and current.low < following.low and current.high < previous.high and current.high < following.high:
            fractals.append(Fractal(index=index, date=current.date, kind="bottom", price=current.low))
    return fractals


def normalize_fractals(fractals: Sequence[Fractal]) -> list[Fractal]:
    normalized: list[Fractal] = []
    for fractal in sorted(fractals, key=lambda item: item.index):
        if not normalized:
            normalized.append(fractal)
            continue
        last = normalized[-1]
        if last.kind != fractal.kind:
            normalized.append(fractal)
            continue
        if fractal.kind == "top" and fractal.price >= last.price:
            normalized[-1] = fractal
        elif fractal.kind == "bottom" and fractal.price <= last.price:
            normalized[-1] = fractal
    return normalized


def build_strokes(fractals: Sequence[Fractal], min_gap: int) -> list[Stroke]:
    if min_gap < 1:
        raise ValueError("min_gap must be >= 1")
    strokes: list[Stroke] = []
    last: Optional[Fractal] = None
    for fractal in fractals:
        if last is None:
            last = fractal
            continue
        if fractal.kind == last.kind:
            last = fractal
            continue
        if fractal.index - last.index < min_gap:
            continue
        stroke = Stroke(start=last, end=fractal)
        if stroke.price_delta > 0:
            strokes.append(stroke)
            last = fractal
    return strokes


def detect_zhongshu(strokes: Sequence[Stroke]) -> list[Zhongshu]:
    centers: list[Zhongshu] = []
    for offset in range(0, max(0, len(strokes) - 2)):
        window = strokes[offset : offset + 3]
        zd = max(stroke.low for stroke in window)
        zg = min(stroke.high for stroke in window)
        center = Zhongshu(start_index=window[0].start_index, end_index=window[-1].end_index, zd=zd, zg=zg)
        if center.valid:
            centers.append(center)
    return centers


def latest_center_before(centers: Sequence[Zhongshu], index: int) -> Optional[Zhongshu]:
    eligible = [center for center in centers if center.end_index < index]
    if not eligible:
        return None
    return max(eligible, key=lambda item: item.end_index)


def dedupe_signals(signals: Sequence[Signal]) -> list[Signal]:
    deduped: dict[tuple[str, SignalKind, int], Signal] = {}
    for signal in signals:
        deduped[(signal.symbol, signal.kind, signal.anchor_index)] = signal
    return list(deduped.values())


def ema(values: Sequence[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1 - alpha) * result[-1])
    return result


def macd_histogram(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> list[float]:
    if not closes:
        return []
    fast_line = ema(closes, fast)
    slow_line = ema(closes, slow)
    dif = [left - right for left, right in zip(fast_line, slow_line)]
    dea = ema(dif, signal)
    return [(left - right) * 2 for left, right in zip(dif, dea)]
