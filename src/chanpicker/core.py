from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Union

from .effort import (
    Effort,
    MacdState,
    divergence_to_meta,
    effort_to_meta,
    judge_divergence,
    macd_state,
    measure_effort,
)
from .indicators import ema  # 重导出：core.ema 是公开 API
from .masystem import (
    MaKissTracker,
    judge_first_buy,
    judge_first_sell,
    judge_second_buy,
    judge_second_sell,
)
from .structure import (
    build_legs,
    detect_zhongshu_by_legs,
    has_trend_divergence_leg,
    leg_meta,
)


class SignalKind(str, Enum):
    FIRST_BUY = "FIRST_BUY"
    SECOND_BUY = "SECOND_BUY"
    THIRD_BUY = "THIRD_BUY"
    FIRST_SELL = "FIRST_SELL"
    SECOND_SELL = "SECOND_SELL"
    FIRST_BUY_FAILED = "FIRST_BUY_FAILED"
    WEAK_TRACK = "WEAK_TRACK"


BUY_KINDS = (SignalKind.FIRST_BUY, SignalKind.SECOND_BUY, SignalKind.THIRD_BUY)
SELL_KINDS = (SignalKind.FIRST_SELL, SignalKind.SECOND_SELL)


class WatchState(str, Enum):
    WAIT_SECOND_BUY = "WAIT_SECOND_BUY"
    WEAK_TRACK = "WEAK_TRACK"
    OPPORTUNITY = "OPPORTUNITY"
    FAILED = "FAILED"
    # 二买失败：二买确认后的考验窗口内，收盘价跌回二买低点下方。
    # 与 FAILED（跌破一买低点）是两层失效，不是同一个状态。
    SECOND_BUY_FAILED = "SECOND_BUY_FAILED"
    # 二买成功归档：考验窗口（max_wait_bars）内收盘价始终守住二买低点。
    # 这轮跟踪到此正常结束——二买的目的（确认回调不破前低）已经验证完毕，
    # 之后无论价格怎么走都不再属于这笔二买的账。
    # 不设这个终态会留下「僵尸记录」：二买确认后永不终结，first_buy_date
    # 永远停在很久以前（实测有 591 天前的记录），导致同一只股票后来出现新的
    # 一买时无法开启新一轮跟踪，关注圈的「一买数」也就永远和买点池对不上。
    SECOND_BUY_OK = "SECOND_BUY_OK"


# 仍在跟踪中的状态。OPPORTUNITY 必须留在这里：二买确认之后价格仍可能跌回
# 二买低点，若不继续参与判定，「二买失败」永远观测不到（机会态曾是终态）。
TRACKING_STATES = frozenset(
    {
        WatchState.WAIT_SECOND_BUY,
        WatchState.WEAK_TRACK,
        WatchState.OPPORTUNITY,
    }
)

# 已出圈（软剔除）：关注圈接口不再返回，但记录留在库里供成功率统计。
CLOSED_STATES = frozenset(
    {
        WatchState.FAILED,
        WatchState.SECOND_BUY_FAILED,
        WatchState.SECOND_BUY_OK,
    }
)

# 触发「同标的冷静期」的状态：出现过的终态都算，避免刚失败又重新入圈。
# SECOND_BUY_OK 刻意**不在**这里：二买守住是成功，不该因为「刚成功」而被
# 拦住下一轮一买——它和新一买本来就是不同的下跌结构。
COOLDOWN_STATES = frozenset(
    {WatchState.FAILED, WatchState.OPPORTUNITY, WatchState.SECOND_BUY_FAILED}
)

WATCH_STATE_LABELS = {
    WatchState.WAIT_SECOND_BUY: "待二买",
    WatchState.WEAK_TRACK: "弱跟踪",
    WatchState.OPPORTUNITY: "机会已触发",
    WatchState.FAILED: "一买失败",
    WatchState.SECOND_BUY_FAILED: "二买失败",
    WatchState.SECOND_BUY_OK: "二买守住",
}


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
    # 周期标签：day / m30 / m15，仅用于多周期区分，不参与缠论计算。
    period: str = "day"

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
    # 信号所属周期：day / m30 / m15
    period: str = "day"

    @property
    def is_sell(self) -> bool:
        return self.kind in SELL_KINDS

    @property
    def is_buy(self) -> bool:
        return self.kind in BUY_KINDS


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
    min_top_divergence_score: float = 0.05
    min_second_sell_score: float = 0.0
    min_macd_area: float = 1e-9
    require_zhongshu_for_first_buy: bool = True
    require_zhongshu_for_first_sell: bool = True
    max_wait_bars: int = 30
    cooldown_bars: int = 60
    min_pullback_ratio: float = 0.20
    max_pullback_ratio: float = 0.90
    # 背驰判定的三个条件阈值（见 effort.judge_divergence）。
    # 留默认值 0 = 与原「任一条件成立即背驰」的最宽松口径一致，
    # 调大可以让判定更严格、信号更少。
    divergence_dif_margin: float = 0.0
    divergence_area_margin: float = 0.0
    divergence_ma_margin: float = 0.0
    # 均线偏离度周期（原文的「缠中说禅走势力度」代理项，只作交叉验证）
    ma_period: int = 20
    # 中枢口径：True=缠论原文「至少三个连续次级别走势类型重叠」(p152)，
    # False=旧实现的「3 笔重叠」。原文 p1351 明确「笔不能构成中枢」，
    # 所以默认走原文口径；保留旧口径是为了能对照、回滚。
    use_canonical_zhongshu: bool = True
    require_canonical_zhongshu_for_first_buy: bool = True
    # 一买/一卖只取「标准背驰」（面积与均线同时弱）。p368：「两个出现一个就要注意，
    # **两个都出现更标准**」——前者当提示，后者才是建仓依据。
    # 不开这个限制时，段口径下一买会涨到 14 个/股，对选股没有实用价值。
    require_standard_divergence: bool = True
    # p116「没有趋势，没有背驰」：盘整里的背驰不算一买/一卖。
    # 开启后一买/一卖只取「下跌/上涨**趋势**」的背驰。
    require_trend_context: bool = True
    # ------------------------------------------------------------------
    # 缠论原文之道（p84/p98）：均线买卖系统的体位与吻
    # ------------------------------------------------------------------
    # 是否启用原文的「男上位最后一吻后」判定。关闭则退回到只用段+中枢的口径。
    use_ma_system: bool = True
    # True = 规则不满足就不出信号（严格贴原文）；
    # False = 仍出信号，但把「差在哪一条」写进 meta 的 rule_violations（放宽观察）。
    enforce_ma_rules: bool = True
    # 均线周期：原文用 5 日 / 10 日。这里按 K 线总数等比缩放，
    # 让 30 分钟图上的短/长期比例与日线保持一致。
    ma_short_bars: int = 5
    ma_long_bars: int = 10
    # 缠绕阈值（|短期-长期|/长期）
    ma_tangle_threshold: float = 0.01
    # 男上位至少要有几吻才认一买。原文 p98：「必须是至少第二吻以后」= 2；
    # 但星注给了「一吻若能与前面走势重组出同级别箱体，也可形成趋势一买」的例子，
    # 默认取 1（一吻也认，放宽但结构相通），需要严格口径时改成 2。
    ma_min_male_kisses: int = 1
    # 构造次级别走势类型段的 K 线根数（p152 对最低级别中枢的定义：
    # 「至少三个该级别单位 K 线重叠部分」）。
    # 设为 0 表示**按 K 线总数自动缩放**：总根数 / 120 并钳在 [3, 8]。
    # 固定 3 只适合日线（400 根）；30 分钟只有 56~80 根时，3 根一段会切出
    # 一堆碎段（实测茅台 10 天切出 26 段、11 个下跌段全是「低点一个比一个高」的
    # 噪声），相邻下跌段几乎永远构不成「创新低」，一买信号全灭。
    min_legs_bars: int = 0
    # 中枢延伸：后续段仍与中枢区间有重叠时并入，区间不变
    zhongshu_extend: bool = True


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


# 不同信号类型把强度分数放在不同的 meta 键里，取第一个命中的。
SCORE_META_KEYS = (
    "divergence_score",
    "second_buy_score",
    "third_buy_score",
    "top_divergence_score",
    "second_sell_score",
)


def signal_score_of(meta: Dict[str, Any]) -> float:
    for key in SCORE_META_KEYS:
        value = meta.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


def segment_meta(stroke: "Stroke") -> Dict[str, Any]:
    """把一段笔的起止坐标打包进信号 meta，供前端画「背驰段」。

    背驰的判定本质是「相邻两段力度对比」，所以只有把两段的起止点都带上，
    前端才能把这两段原样描出来；只给一个分数是看不出结构在哪里的。
    """

    return {
        "start_index": stroke.start.index,
        "start_price": stroke.start.price,
        "end_index": stroke.end.index,
        "end_price": stroke.end.price,
        "direction": stroke.direction,
    }


def signal_score_from_meta(divergence_meta: Dict[str, Any]) -> float:
    """从背驰判定结果里取强度分数。

    旧口径是「面积衰减比例」，新口径是 `judge_divergence` 给的 score
    （三条件命中比例 70% + 面积衰减幅度 30%）。两者的量纲不同，
    但都落在 0~1 之间，前端排序与表格展示不受影响。
    """

    detail = divergence_meta.get("divergence_detail") or {}
    score = detail.get("score")
    if score is None:
        ratio = detail.get("area_ratio")
        score = max(0.0, 1.0 - float(ratio)) if ratio is not None else 0.0
    return float(score)


def signal_to_dict(signal: Signal) -> dict:
    data = asdict(signal)
    data["kind"] = signal.kind.value
    data["score"] = signal_score_of(signal.meta)
    data["is_sell"] = signal.is_sell
    return data


def signal_from_dict(data: dict) -> Signal:
    copied = dict(data)
    copied["kind"] = SignalKind(copied["kind"])
    # score / is_sell 是 signal_to_dict 派生的展示字段，不属于 Signal 构造参数
    copied.pop("score", None)
    copied.pop("is_sell", None)
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
        # 由 scan() 填充：MACD 三线与收盘价序列，供逐片力度与均线偏离度计算复用
        self._macd: MacdState = macd_state([])
        self._closes: list[float] = []
        # 由 scan() 填充：均线买卖系统的体位与吻（p84/p98）
        self._ma_tracker = None

    def scan(self, bars: Sequence[Bar]) -> ScanResult:
        ordered = self._validate_bars(bars)
        symbol = ordered[0].symbol
        # 逐片力度与均线偏离度都需要这两份数据，先缓存起来给各 _detect_* 复用
        self._closes = [bar.close for bar in ordered]
        self._macd = macd_state(self._closes)
        # 缠论原文之道：均线买卖系统的体位与吻（p84/p98）
        self._ma_tracker = None
        if self.config.use_ma_system and len(ordered) > max(
            self.config.ma_short_bars, self.config.ma_long_bars
        ):
            tracker = MaKissTracker.build(
                self._closes, self.config.ma_short_bars, self.config.ma_long_bars
            )
            tracker.tangle_threshold = self.config.ma_tangle_threshold
            tracker.run([bar.date for bar in ordered])
            self._ma_tracker = tracker
        hist = self._macd.hist
        fractals = normalize_fractals(detect_fractals(ordered))
        strokes = build_strokes(fractals, self.config.min_stroke_gap)
        if self.config.use_canonical_zhongshu:
            # 原文口径：中枢与买卖点统一在「次级别走势类型」层面判定（p152）。
            # 两者必须同层级——混用会让「起点 >= 中枢终点」永远匹配不上。
            legs = self._build_legs(ordered)
            centers = detect_zhongshu_by_legs(legs, extend=self.config.zhongshu_extend)
            first_buy = self._detect_first_buy_by_legs(ordered, legs, centers)
            second_buy = self._detect_second_buy_by_legs(ordered, legs, first_buy)
            third_buy = self._detect_third_buy_by_legs(ordered, legs, centers)
            first_sell = self._detect_first_sell_by_legs(ordered, legs, centers)
            second_sell = self._detect_second_sell_by_legs(ordered, legs, first_sell)
        else:
            # 旧口径（保留用于对照与回滚）：中枢与买卖点都在「笔」层面。
            centers = detect_zhongshu(strokes)
            first_buy = self._detect_first_buy(ordered, strokes, centers, hist)
            second_buy = self._detect_second_buy(ordered, strokes, hist, first_buy)
            third_buy = self._detect_third_buy(ordered, strokes, centers, hist)
            first_sell = self._detect_first_sell(ordered, strokes, centers, hist)
            second_sell = self._detect_second_sell(ordered, strokes, hist, first_sell)
        signals = sorted(
            first_buy + second_buy + third_buy + first_sell + second_sell,
            key=lambda item: (item.bar_index, item.kind.value),
        )
        # 周期戳由入参 K 线决定，避免各 _detect_* 方法重复传参。
        period = ordered[0].period if ordered else "day"
        if period != "day":
            signals = [replace(signal, period=period) for signal in signals]
        return ScanResult(symbol=symbol, bars=len(ordered), fractals=fractals, strokes=strokes, zhongshu=centers, signals=signals)

    def _resolve_legs_bars(self, total_bars: int) -> int:
        """决定每段用几根 K 线。

        p152 只说「至少三个该级别单位 K 线重叠」，没规定固定值——因为「单位 K 线」
        的长度是相对的：日线上 3 根是合理粒度，30 分钟上 3 根却太碎。

        实测问题（茅台 30 分钟，80 根）：每段 3 根会切出 26 段、11 个下跌段，
        而这 11 个段全是「低点一个比一个高」的噪声——相邻下跌段几乎永远
        构不成「创新低」，一买信号全灭。

        这里按 K 线总数等比缩放（`min_legs_bars=0` 时启用），钳在 [3, 8]：
        400 根 → 3（与日线原口径一致）；80 根 → 3；30 根 → 3。
        不再更细是因为段越细、中枢越碎，背驰判定越不可靠——30 分钟数据
        本来就只有 56~80 根，再细就切不出有意义的中枢。
        """

        if self.config.min_legs_bars > 0:
            return self.config.min_legs_bars
        scaled = int(round(total_bars / 120))
        return max(3, min(8, scaled))

    def _build_legs(self, ordered: Sequence[Bar]) -> list:
        """构造次级别走势类型段（p152 对最低级别的定义）。"""

        return build_legs(
            [bar.high for bar in ordered],
            [bar.low for bar in ordered],
            closes=[bar.close for bar in ordered],
            min_legs=self._resolve_legs_bars(len(ordered)),
        )

    def _leg_effort(self, leg, left: int, right: int):
        """一段走势的力度（复用 effort 模块的三条件口径）。

        `min_macd_area` 的回退必须在这里同样生效——否则该配置项对段口径
        完全无效（笔路径有、段路径没有），两条路径行为不一致，很难排查。
        回退时把三个口径统一换成价格幅度：面积→价格差、DIF→±价格差、
        均线→0（该条件下无信息，判 True 等于凭空造背驰）。
        """

        effort = measure_effort(
            self._macd, left, right, direction=leg.direction,
            closes=self._closes, ma_period=self.config.ma_period,
        )
        if effort.macd_area >= self.config.min_macd_area:
            return effort
        signed = leg.price_delta if leg.direction == "up" else -leg.price_delta
        return replace(
            effort,
            macd_area=leg.price_delta,
            macd_piece_area=leg.price_delta,
            peak_area=leg.price_delta,
            dif_extreme=signed,
            dea_extreme=signed,
            ma_extreme=0.0,
        )

    def _leg_divergence_meta(self, previous, current) -> tuple[bool, dict]:
        """两段之间的背驰判定（与笔层面同一套三条件，见 effort 模块）。"""

        previous_effort = self._leg_effort(previous, previous.start_index, previous.end_index)
        current_effort = self._leg_effort(current, current.start_index, current.end_index)
        verdict = judge_divergence(
            previous_effort, current_effort, current.direction,
            area_ratio_threshold=1.0,
            dif_margin=self.config.divergence_dif_margin,
            ma_margin=self.config.divergence_ma_margin,
            area_margin=self.config.divergence_area_margin,
        )
        meta = effort_to_meta(previous_effort, "previous_effort_detail")
        meta.update(effort_to_meta(current_effort, "current_effort_detail"))
        meta.update(divergence_to_meta(verdict))
        return verdict.is_divergent, meta

    def _judge_first_buy_rule(
        self, divergence_index: int, segment_low_index: int, below_center: bool
    ):
        """按 p98 判定一买是否满足原文的「道」。返回 None 表示均线系统未启用。"""

        tracker = self._ma_tracker
        if tracker is None:
            return None
        return judge_first_buy(
            tracker,
            divergence_index=divergence_index,
            segment_low_index=segment_low_index,
            below_center=below_center,
            min_kisses=self.config.ma_min_male_kisses,
        )

    def _judge_second_buy_rule(
        self, pullback_low_index: int, first_buy_index: int, first_buy_low: float
    ):
        """按 p98 判定二买；均线位置使用索引，破位判断使用一买低点价格。"""

        tracker = self._ma_tracker
        if tracker is None:
            return None
        close_at = self._closes[pullback_low_index]
        return judge_second_buy(
            tracker,
            first_buy_index=first_buy_index,
            pullback_low_index=pullback_low_index,
            holds_first_buy_low=close_at > first_buy_low,
        )

    @staticmethod
    def _rule_meta(rule) -> dict:
        """把原文规则的判定结果写进 meta，前端可展示「为什么合规 / 差在哪」。"""

        return rule.to_meta() if rule is not None else {}

    def _detect_first_buy_by_legs(self, ordered, legs, centers) -> list[Signal]:
        """一买：下跌**趋势**的背驰点。

        两道收紧（缺一不可，否则段口径下信号会涨到 14 个/股）：
        1. **标准背驰**（p368）：面积与均线同时弱才算建仓依据；
        2. **趋势背驰**（p116）：「没有趋势，没有背驰」，盘整里的背驰不算一买。
        """

        signals: list[Signal] = []
        downs = [leg for leg in legs if leg.direction == "down"]
        for previous, current in zip(downs, downs[1:]):
            if current.low >= previous.low:
                continue
            is_divergent, divergence_meta = self._leg_divergence_meta(previous, current)
            if not is_divergent:
                continue
            if self.config.require_standard_divergence and not divergence_meta["divergence_detail"]["standard"]:
                continue
            if self.config.require_trend_context and not has_trend_divergence_leg(
                legs, current.start_index, centers, "down"
            ):
                continue
            previous_effort = self._leg_effort(previous, previous.start_index, previous.end_index)
            current_effort = self._leg_effort(current, current.start_index, current.end_index)
            score = signal_score_from_meta(divergence_meta)

            center = latest_center_before(centers, current.start_index)
            below_center = center is not None and current.low <= center.zd
            if self.config.require_zhongshu_for_first_buy:
                if center is None or current.low > center.zd:
                    continue

            confirmation_index = current.end_index + 1
            if confirmation_index >= len(ordered):
                continue

            # 缠论原文之道（p98）：「男上位最后一吻后出现的背驰式下跌构成」
            low_index = self._leg_low_index(current, ordered)
            rule = self._judge_first_buy_rule(
                confirmation_index, low_index, below_center
            )
            if rule is not None and not rule.satisfied and self.config.enforce_ma_rules:
                continue

            confirmation = ordered[confirmation_index]
            signals.append(
                Signal(
                    symbol=confirmation.symbol,
                    kind=SignalKind.FIRST_BUY,
                    date=confirmation.date,
                    bar_index=confirmation_index,
                    price=confirmation.close,
                    anchor_index=low_index,
                    anchor_price=ordered[low_index].low,
                    message="下跌走势背驰后一买成立，纳入关注圈并等待二买确认",
                    meta={
                        **divergence_meta,
                        "divergence_score": round(score, 6),
                        "structure_level": "leg",
                        **self._rule_meta(rule),
                        "previous_segment": leg_meta(previous, previous_effort),
                        "current_segment": leg_meta(current, current_effort),
                        "zhongshu_zd": None if center is None else center.zd,
                        "zhongshu_zg": None if center is None else center.zg,
                    },
                )
            )
        return dedupe_signals(signals)

    def _detect_second_buy_by_legs(self, ordered, legs, first_buys) -> list[Signal]:
        """二买：一买后次级别回调不破一买低点（p152-153 的严格依据）。

        原文：「在第一类买点出现后**第一次次级别回调**制造的低点，
        是市场中第二有利的位置」。

        「第一次」是硬要求：必须取离开段（第一个 up 段）**之后紧邻的那个
        down 段**。用 `start_index >= 跳过条件` 会跨过多个同向段找到远处的
        回调段（实测段距可达 4），那已经不是「第一次回调」了。
        """

        signals: list[Signal] = []
        for first_buy in first_buys:
            up_leg = next(
                (leg for leg in legs if leg.direction == "up" and leg.start_index >= first_buy.anchor_index),
                None,
            )
            if up_leg is None:
                continue
            # 「第一次次级别回调」= 离开段在段序列中的**下一个**段，
            # 且必须是反向（down）段，否则说明离开段还没结束，不构成回调。
            up_index = next(
                (i for i, leg in enumerate(legs) if leg.start_index == up_leg.start_index),
                None,
            )
            if up_index is None or up_index + 1 >= len(legs):
                continue
            pullback = legs[up_index + 1]
            if pullback.direction != "down":
                continue
            # 次级别回调不破一买低点
            if pullback.low <= first_buy.anchor_price:
                continue

            confirmation_index = pullback.end_index + 1
            if confirmation_index >= len(ordered):
                continue

            # 缠论原文之道（p98）：二买是「女上位第一吻后出现的下跌」
            rule = self._judge_second_buy_rule(
                pullback_low_index=self._leg_low_index(pullback, ordered),
                first_buy_index=first_buy.anchor_index,
                first_buy_low=first_buy.anchor_price,
            )
            if rule is not None and not rule.satisfied and self.config.enforce_ma_rules:
                continue

            confirmation = ordered[confirmation_index]
            previous_effort = self._leg_effort(up_leg, up_leg.start_index, up_leg.end_index)
            current_effort = self._leg_effort(pullback, pullback.start_index, pullback.end_index)
            score = (
                (previous_effort.macd_area - current_effort.macd_area)
                / previous_effort.macd_area
                if previous_effort.macd_area
                else 0.0
            )
            signals.append(
                Signal(
                    symbol=confirmation.symbol,
                    kind=SignalKind.SECOND_BUY,
                    date=confirmation.date,
                    bar_index=confirmation_index,
                    price=confirmation.close,
                    anchor_index=pullback.end_index,
                    anchor_price=pullback.low,
                    message="次级别回调不破一买低点，二买确认",
                    meta={
                        "first_buy_date": first_buy.date,
                        "first_buy_low": first_buy.anchor_price,
                        "structure_level": "leg",
                        **self._rule_meta(rule),
                        "second_buy_score": round(score, 6),
                        "previous_effort": round(previous_effort.macd_area, 6),
                        "current_effort": round(current_effort.macd_area, 6),
                        "previous_segment": leg_meta(up_leg, previous_effort),
                        "current_segment": leg_meta(pullback, current_effort),
                    },
                )
            )
        return dedupe_signals(signals)

    @staticmethod
    def _leg_low_index(leg, ordered) -> int:
        """找出该段最低点所在的 K 线索引——原文的判定都锚在「低点」上。"""

        best_index = leg.start_index
        best_low = None
        for index in range(leg.start_index, min(leg.end_index + 1, len(ordered))):
            low = ordered[index].low
            if best_low is None or low < best_low:
                best_low = low
                best_index = index
        return best_index

    @staticmethod
    def _leg_high_index(leg, ordered) -> int:
        """找出该段最高点所在的 K 线索引——卖点的原文判定锚在「高点」上。"""

        best_index = leg.start_index
        best_high = None
        for index in range(leg.start_index, min(leg.end_index + 1, len(ordered))):
            high = ordered[index].high
            if best_high is None or high > best_high:
                best_high = high
                best_index = index
        return best_index

    def _detect_third_buy_by_legs(self, ordered, legs, centers) -> list[Signal]:
        """三买：中枢后向上离开，回调不跌回中枢上沿 ZG。

        p208 定理原文：「其低点不跌破 ZG，则构成第三类买点」；
        p590 补充：必须是**第一次**回抽。
        """

        signals: list[Signal] = []
        for center in centers:
            breakout = next(
                (
                    leg
                    for leg in legs
                    if leg.direction == "up"
                    and leg.start_index >= center.end_index
                    and leg.high > center.zg
                ),
                None,
            )
            if breakout is None:
                continue
            # p590：必须是**第一次**回抽——取突破段之后的下一个反向段，
            # 不能跳过多个同向段去远处找（那样就不是「第一次」了）。
            breakout_index = next(
                (i for i, leg in enumerate(legs) if leg.start_index == breakout.start_index),
                None,
            )
            if breakout_index is None or breakout_index + 1 >= len(legs):
                continue
            pullback = legs[breakout_index + 1]
            if pullback.direction != "down":
                continue
            # 定理条件：回调低点不跌破 ZG
            if pullback.low <= center.zg:
                continue
            confirmation_index = pullback.end_index + 1
            if confirmation_index >= len(ordered):
                continue
            confirmation = ordered[confirmation_index]
            previous_effort = self._leg_effort(breakout, breakout.start_index, breakout.end_index)
            current_effort = self._leg_effort(pullback, pullback.start_index, pullback.end_index)
            score = (
                (previous_effort.macd_area - current_effort.macd_area)
                / previous_effort.macd_area
                if previous_effort.macd_area
                else 0.0
            )
            if score < self.config.min_third_buy_score:
                continue
            signals.append(
                Signal(
                    symbol=confirmation.symbol,
                    kind=SignalKind.THIRD_BUY,
                    date=confirmation.date,
                    bar_index=confirmation_index,
                    price=confirmation.close,
                    anchor_index=pullback.end_index,
                    anchor_price=pullback.low,
                    message="中枢后回调不破中枢上沿 ZG，三买成立",
                    meta={
                        "structure_level": "leg",
                        "third_buy_score": round(score, 6),
                        "previous_effort": round(previous_effort.macd_area, 6),
                        "current_effort": round(current_effort.macd_area, 6),
                        "previous_segment": leg_meta(breakout, previous_effort),
                        "current_segment": leg_meta(pullback, current_effort),
                        "zhongshu_zd": center.zd,
                        "zhongshu_zg": center.zg,
                        "breakout_high": breakout.high,
                    },
                )
            )
        return dedupe_signals(signals)

    def _detect_first_sell_by_legs(self, ordered, legs, centers) -> list[Signal]:
        """一卖：上涨**趋势**的顶背驰（一买在段层面的镜像，同样两道收紧）。"""

        signals: list[Signal] = []
        ups = [leg for leg in legs if leg.direction == "up"]
        for previous, current in zip(ups, ups[1:]):
            if current.high <= previous.high:
                continue
            is_divergent, divergence_meta = self._leg_divergence_meta(previous, current)
            if not is_divergent:
                continue
            if self.config.require_standard_divergence and not divergence_meta["divergence_detail"]["standard"]:
                continue
            if self.config.require_trend_context and not has_trend_divergence_leg(
                legs, current.start_index, centers, "up"
            ):
                continue
            previous_effort = self._leg_effort(previous, previous.start_index, previous.end_index)
            current_effort = self._leg_effort(current, current.start_index, current.end_index)
            score = signal_score_from_meta(divergence_meta)

            center = latest_center_before(centers, current.start_index)
            above_center = center is not None and current.high >= center.zg
            if self.config.require_zhongshu_for_first_sell:
                if center is None or current.high < center.zg:
                    continue
            confirmation_index = current.end_index + 1
            if confirmation_index >= len(ordered):
                continue

            # 缠论原文之道（一买的镜像）：「女上位最后一吻后出现的背驰式上涨」
            rule = None
            if self._ma_tracker is not None:
                rule = judge_first_sell(
                    self._ma_tracker,
                    divergence_index=confirmation_index,
                    segment_high_index=self._leg_high_index(current, ordered),
                    above_center=above_center,
                )
                if not rule.satisfied and self.config.enforce_ma_rules:
                    continue

            confirmation = ordered[confirmation_index]
            signals.append(
                Signal(
                    symbol=confirmation.symbol,
                    kind=SignalKind.FIRST_SELL,
                    date=confirmation.date,
                    bar_index=confirmation_index,
                    price=confirmation.close,
                    anchor_index=current.end_index,
                    anchor_price=current.high,
                    message="上涨走势顶背驰后一卖成立，风险提示：留意减仓",
                    meta={
                        **divergence_meta,
                        "top_divergence_score": round(score, 6),
                        "structure_level": "leg",
                        **self._rule_meta(rule),
                        "previous_segment": leg_meta(previous, previous_effort),
                        "current_segment": leg_meta(current, current_effort),
                        "zhongshu_zd": None if center is None else center.zd,
                        "zhongshu_zg": None if center is None else center.zg,
                    },
                )
            )
        return dedupe_signals(signals)

    def _detect_second_sell_by_legs(self, ordered, legs, first_sells) -> list[Signal]:
        """二卖：一卖后首次反弹不创新高（一卖在段层面的镜像）。"""

        signals: list[Signal] = []
        for first_sell in first_sells:
            down_leg = next(
                (leg for leg in legs if leg.direction == "down" and leg.start_index >= first_sell.anchor_index),
                None,
            )
            if down_leg is None:
                continue
            # 与二买对称：取下跌段之后紧邻的那个反弹段
            down_index = next(
                (i for i, leg in enumerate(legs) if leg.start_index == down_leg.start_index),
                None,
            )
            if down_index is None or down_index + 1 >= len(legs):
                continue
            rebound = legs[down_index + 1]
            if rebound.direction != "up":
                continue
            # 反弹创出一卖新高 → 二卖不成立
            if rebound.high >= first_sell.anchor_price:
                continue

            confirmation_index = rebound.end_index + 1
            if confirmation_index >= len(ordered):
                continue

            # 缠论原文之道（二卖为二买镜像）：「男上位第一吻后出现的反弹」
            rule = None
            if self._ma_tracker is not None:
                rule = judge_second_sell(
                    self._ma_tracker,
                    pullback_high_index=self._leg_high_index(rebound, ordered),
                    below_first_sell_high=rebound.high < first_sell.anchor_price,
                )
                if not rule.satisfied and self.config.enforce_ma_rules:
                    continue

            confirmation = ordered[confirmation_index]
            previous_effort = self._leg_effort(down_leg, down_leg.start_index, down_leg.end_index)
            current_effort = self._leg_effort(rebound, rebound.start_index, rebound.end_index)
            score = (
                (previous_effort.macd_area - current_effort.macd_area)
                / previous_effort.macd_area
                if previous_effort.macd_area
                else 0.0
            )
            signals.append(
                Signal(
                    symbol=confirmation.symbol,
                    kind=SignalKind.SECOND_SELL,
                    date=confirmation.date,
                    bar_index=confirmation_index,
                    price=confirmation.close,
                    anchor_index=rebound.end_index,
                    anchor_price=rebound.high,
                    message="反弹不创新高，二卖成立",
                    meta={
                        "first_sell_date": first_sell.date,
                        "first_sell_high": first_sell.anchor_price,
                        "structure_level": "leg",
                        **self._rule_meta(rule),
                        "second_sell_score": round(score, 6),
                        "previous_effort": round(previous_effort.macd_area, 6),
                        "current_effort": round(current_effort.macd_area, 6),
                        "previous_segment": leg_meta(down_leg, previous_effort),
                        "current_segment": leg_meta(rebound, current_effort),
                    },
                )
            )
        return dedupe_signals(signals)

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
            # 背驰判定：p368 的三个条件（DIF/DEA 不创新低 + 同色柱面积变小 +
            # 均线不创新低），任一成立即可；②③同时成立记为标准背驰。
            is_divergent, divergence_meta = self._divergence_meta(previous, current)
            if not is_divergent:
                continue
            score = signal_score_from_meta(divergence_meta)

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
                        **divergence_meta,
                        "divergence_score": round(score, 6),
                        "previous_segment": segment_meta(previous),
                        "current_segment": segment_meta(current),
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
                        "previous_segment": segment_meta(up_stroke),
                        "current_segment": segment_meta(pullback),
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
                    "previous_segment": segment_meta(breakout),
                    "current_segment": segment_meta(pullback),
                    "zhongshu_zd": center.zd,
                    "zhongshu_zg": center.zg,
                    "breakout_high": breakout.end.price,
                },
                )
            )
        return dedupe_signals(signals)

    def _detect_first_sell(
        self,
        bars: Sequence[Bar],
        strokes: Sequence[Stroke],
        centers: Sequence[Zhongshu],
        hist: Sequence[float],
    ) -> list[Signal]:
        """一卖：与一买镜像。连续两个上升段，后段创新高但力度衰减（顶背驰）。

        判定（日线级别）：
        1. 后一个上升段终点价格 > 前一个上升段终点价格（创新高）；
        2. 末段 MACD 柱面积相对前段衰减，顶背驰度 >= min_top_divergence_score；
        3. 高点位于最近中枢 ZG 上方（require_zhongshu_for_first_sell）；
        4. 以顶分型之后的一根 K 线为确认日，避免未来函数。
        """

        signals: list[Signal] = []
        up_strokes = [stroke for stroke in strokes if stroke.direction == "up" and stroke.end.kind == "top"]
        for previous, current in zip(up_strokes, up_strokes[1:]):
            if current.end.price <= previous.end.price:
                continue
            # 与一买对称的三条件顶背驰判定（p368）
            is_divergent, divergence_meta = self._divergence_meta(previous, current)
            if not is_divergent:
                continue
            score = signal_score_from_meta(divergence_meta)

            center = latest_center_before(centers, current.end.index)
            if self.config.require_zhongshu_for_first_sell:
                if center is None or current.end.price < center.zg:
                    continue

            confirmation_index = current.end.index + 1
            if confirmation_index >= len(bars):
                continue
            confirmation = bars[confirmation_index]
            signals.append(
                Signal(
                    symbol=confirmation.symbol,
                    kind=SignalKind.FIRST_SELL,
                    date=confirmation.date,
                    bar_index=confirmation_index,
                    price=confirmation.close,
                    anchor_index=current.end.index,
                    anchor_price=current.end.price,
                    message="顶背驰后一卖成立，风险提示：留意减仓",
                    meta={
                        **divergence_meta,
                        "top_divergence_score": round(score, 6),
                        "previous_segment": segment_meta(previous),
                        "current_segment": segment_meta(current),
                        "zhongshu_zd": None if center is None else center.zd,
                        "zhongshu_zg": None if center is None else center.zg,
                    },
                )
            )
        return dedupe_signals(signals)

    def _detect_second_sell(
        self,
        bars: Sequence[Bar],
        strokes: Sequence[Stroke],
        hist: Sequence[float],
        first_sell_signals: Sequence[Signal],
    ) -> list[Signal]:
        """二卖：一卖后首次反弹不创新高，且反弹力度衰减。"""

        signals: list[Signal] = []
        for first_sell in first_sell_signals:
            down_stroke = next(
                (
                    stroke
                    for stroke in strokes
                    if stroke.direction == "down" and stroke.start.index >= first_sell.anchor_index
                ),
                None,
            )
            if down_stroke is None or down_stroke.price_delta <= 0:
                continue
            rebound = next(
                (
                    stroke
                    for stroke in strokes
                    if stroke.direction == "up"
                    and stroke.start.index >= down_stroke.end.index
                    and stroke.end.kind == "top"
                ),
                None,
            )
            if rebound is None:
                continue
            # 反弹创出一卖新高 → 二卖不成立
            if rebound.end.price >= first_sell.anchor_price:
                continue

            retracement = (rebound.end.price - down_stroke.end.price) / down_stroke.price_delta
            if not (self.config.min_pullback_ratio <= retracement <= self.config.max_pullback_ratio):
                continue

            down_effort = self._stroke_effort(down_stroke, hist)
            rebound_effort = self._stroke_effort(rebound, hist)
            if down_effort <= 0:
                continue
            score = (down_effort - rebound_effort) / down_effort
            if score < self.config.min_second_sell_score:
                continue

            confirmation_index = rebound.end.index + 1
            if confirmation_index >= len(bars):
                continue
            confirmation = bars[confirmation_index]
            signals.append(
                Signal(
                    symbol=confirmation.symbol,
                    kind=SignalKind.SECOND_SELL,
                    date=confirmation.date,
                    bar_index=confirmation_index,
                    price=confirmation.close,
                    anchor_index=rebound.end.index,
                    anchor_price=rebound.end.price,
                    message="反弹不创新高，二卖成立",
                meta={
                    "first_sell_date": first_sell.date,
                    "first_sell_high": first_sell.anchor_price,
                    "retracement": round(retracement, 6),
                    "second_sell_score": round(score, 6),
                    "down_effort": round(down_effort, 6),
                    "rebound_effort": round(rebound_effort, 6),
                    "previous_segment": segment_meta(down_stroke),
                    "current_segment": segment_meta(rebound),
                },
                )
            )
        return dedupe_signals(signals)

    def _detect_centers(self, ordered: Sequence[Bar], strokes: Sequence[Stroke]) -> list[Zhongshu]:
        """构建中枢，按配置在两种口径间切换。

        原文口径（p152）：「某级别走势类型中，被**至少三个连续次级别走势类型**
        所重叠的部分，称为缠中说禅走势中枢」；对最后不能分解的级别（我们的
        日线图），定义为「至少三个该级别单位 K 线重叠部分」。

        旧口径：3 笔重叠。原文 p1351 专门强调「笔不能构成中枢」——
        笔是最低一级结构，不含「三个次级别运动」的含义，两者判出的中枢
        位置与数量都不同。保留旧口径仅用于对照与回滚。
        """

        if not self.config.use_canonical_zhongshu:
            return detect_zhongshu(strokes)

        legs = build_legs(
            [bar.high for bar in ordered],
            [bar.low for bar in ordered],
            closes=[bar.close for bar in ordered],
            min_legs=self.config.min_legs_bars,
        )
        return detect_zhongshu_by_legs(legs, extend=self.config.zhongshu_extend)

    def _stroke_effort(self, stroke: Stroke, hist: Sequence[float]) -> float:
        """单段走势的力度（口径1：同色柱逐片面积）。

        从「整段 abs() 求和」改为分片累加，并按 p295 对末片做 ×2 折算。
        分片是必须的（p125「肯定是片和片比」）：正负混着加等于把上涨和下跌的
        柱面积互相抵消，一段走势的力度会被算得远小于真实值。
        """

        effort = self._stroke_effort_detail(stroke)
        if effort.macd_area >= self.config.min_macd_area:
            return effort.macd_area
        return stroke.price_delta

    def _stroke_effort_detail(self, stroke: Stroke) -> "Effort":
        """带全部口径的力度明细，供背驰判定与前端展示使用。

        `min_macd_area` 的回退必须在这里做（而不是只在 _stroke_effort 里）：
        背驰判定走的是本方法，回退若不生效，MACD 面积极小（如短周期或
        构造数据）时三个条件会全部失真——曾导致设了 `min_macd_area=9999`
        的测试序列拿 MACD 面积去比，而不是回退到价格差。
        """

        effort = measure_effort(
            self._macd,
            stroke.start.index,
            stroke.end.index,
            direction=stroke.direction,
            closes=self._closes,
            ma_period=self.config.ma_period,
        )
        if effort.macd_area < self.config.min_macd_area:
            # 回退到价格幅度：MACD 面积太小（短周期、构造数据、极窄震荡）时
            # 三个条件会失真。此时把三个口径**统一**改用价格幅度——
            # 不能只换面积却留着 MACD 原值的 DIF/均线，那样量纲不可比，
            # 面积条件（量纲=价格）与 DIF 条件（量纲=价格差/EMA）的比较
            # 会互相干扰，看起来能跑其实判定已经失真。
            signed = stroke.price_delta if stroke.direction == "up" else -stroke.price_delta
            return replace(
                effort,
                macd_area=stroke.price_delta,
                macd_piece_area=stroke.price_delta,
                peak_area=stroke.price_delta,
                dif_extreme=signed,
                dea_extreme=signed,
                # 均线偏离度本身是「相对幅度」，与价格幅度不同量纲；
                # 置 0 让 ma_weaker 恒为 False —— 该条件下没有可用信息，
                # 硬凑一个值等于凭空造出背驰。
                ma_extreme=0.0,
            )
        return effort

    def _divergence_meta(
        self,
        previous: Stroke,
        current: Stroke,
    ) -> tuple[bool, dict]:
        """用三个条件判定背驰（p368），返回 (是否背驰, 写入 meta 的依据)。

        旧口径是单一「面积比 ≥ min_divergence_score」，现在按原文：
        ① DIF/DEA 不创新高 ② 同色柱面积变小 ③ 均线不创新高——
        任一成立即背驰，②③同时成立记为标准背驰。
        """

        previous_effort = self._stroke_effort_detail(previous)
        current_effort = self._stroke_effort_detail(current)
        verdict = judge_divergence(
            previous_effort,
            current_effort,
            current.direction,
            area_ratio_threshold=1.0,
            dif_margin=self.config.divergence_dif_margin,
            ma_margin=self.config.divergence_ma_margin,
            area_margin=self.config.divergence_area_margin,
        )
        meta = effort_to_meta(previous_effort, "previous_effort_detail")
        meta.update(effort_to_meta(current_effort, "current_effort_detail"))
        meta.update(divergence_to_meta(verdict))
        return verdict.is_divergent, meta


class WatchlistManager:
    def __init__(self, config: Optional[ChanConfig] = None) -> None:
        self.config = config or ChanConfig()

    def update(self, bars: Sequence[Bar], signals: Sequence[Signal], existing: Iterable[WatchItem] = ()) -> list[WatchItem]:
        ordered = ChanEngine._validate_bars(bars)
        symbol = ordered[0].symbol
        current_bar = ordered[-1]
        current_index = len(ordered) - 1
        items = [item for item in existing if item.symbol == symbol]
        # 「仍在跟踪」包含 OPPORTUNITY：二买确认之后价格仍可能跌回二买低点，
        # 必须继续参与判定，否则二买失败永远观测不到。
        active = next((item for item in items if item.state in TRACKING_STATES), None)

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

        # 1) 一买失败：收盘价跌破一买低点，整段下跌结构失效，优先级最高。
        #    已经二买确认的也同样处理——低点都破了，二买自然不成立。
        if current_bar.close < active.first_buy_low:
            if active.state != WatchState.FAILED:
                active.state = WatchState.FAILED
                active.exit_date = current_bar.date
                active.exit_reason = "跌破一买低点，关注失效"
            return items

        if second_buy is not None:
            if active.second_buy_date is None:
                active.state = WatchState.OPPORTUNITY
                active.second_buy_date = second_buy.date
                active.second_buy_price = second_buy.price
                active.exit_date = second_buy.date
                active.exit_reason = "二买确认，机会触发"
            active.meta.update(second_buy.meta)
            # 二买低点（回调底分型）与确认位置：即便某次扫描因 K 线变短没能再检出
            # 该二买信号，也能靠这两个字段续判「二买失败」。
            active.meta["second_buy_low"] = second_buy.anchor_price
            active.meta["second_buy_index"] = second_buy.bar_index

        if active.state != WatchState.OPPORTUNITY:
            # 还没等到二买：只看是否超时降级弱跟踪。
            if current_index - active.first_buy_index > self.config.max_wait_bars:
                active.state = WatchState.WEAK_TRACK
                active.exit_reason = f"超过 {self.config.max_wait_bars} 根K线未出现二买，降级弱跟踪"
            return items

        # 2) 已确认二买：考验窗口内收盘价跌回二买低点下方 → 二买失败。
        #    窗口用 max_wait_bars：超过窗口仍未破位视为「守住了」，
        #    之后若因大盘整体回落而破位，不该记在这笔二买账上。
        second_index = active.meta.get("second_buy_index")
        second_low = active.meta.get("second_buy_low")
        if second_index is None or second_low is None:
            return items
        if current_index - int(second_index) > self.config.max_wait_bars:
            # 考验期已过且收盘始终守在二买低点上方 → 这笔二买成功，正常归档。
            # 不归档会变成僵尸记录：状态永远停在 OPPORTUNITY，占着 active 位，
            # 后来出现的新一买无法开启新一轮跟踪，first_buy_date 永远是老日期
            # （实测有 591 天前的记录），关注圈与买点池的数字就永远对不上。
            active.state = WatchState.SECOND_BUY_OK
            active.exit_date = current_bar.date
            active.exit_reason = (
                f"二买后 {self.config.max_wait_bars} 根内守住二买低点，二买成功，跟踪结束"
            )
            # 同一批 signals 里如果有**更新的一买**（新一轮下跌结构结束），
            # 立刻开新一轮，不必等下一次扫描。门槛用「比被归档的二买更晚」，
            # 避免把同一轮里的老一买当新起点重复入圈。
            archived_second_index = int(second_index)
            newer = [s for s in first_buy_signals if s.bar_index > archived_second_index]
            if newer and not self._in_cooldown(items, newer[-1].bar_index):
                follow = newer[-1]
                items.append(
                    WatchItem(
                        symbol=symbol,
                        state=WatchState.WAIT_SECOND_BUY,
                        first_buy_date=follow.date,
                        first_buy_index=follow.bar_index,
                        first_buy_price=follow.price,
                        first_buy_low=follow.anchor_price,
                        last_update_date=current_bar.date,
                        meta=dict(follow.meta),
                    )
                )
            return items
        if current_bar.close < float(second_low):
            active.state = WatchState.SECOND_BUY_FAILED
            active.exit_date = current_bar.date
            active.exit_reason = (
                f"二买后 {self.config.max_wait_bars} 根内跌破二买低点，二买失败，剔除出圈"
            )
        return items

    def _in_cooldown(self, items: Sequence[WatchItem], candidate_index: int) -> bool:
        terminal = [item for item in items if item.state in COOLDOWN_STATES]
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




def macd_histogram(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> list[float]:
    if not closes:
        return []
    fast_line = ema(closes, fast)
    slow_line = ema(closes, slow)
    dif = [left - right for left, right in zip(fast_line, slow_line)]
    dea = ema(dif, signal)
    return [(left - right) * 2 for left, right in zip(dif, dea)]
