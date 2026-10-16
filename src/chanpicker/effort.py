"""力度计算与背驰判定——严格按缠中说禅原文口径实现。

原文依据（页码对应《教你炒股票108课》张星星注解版，全文导出见 /tmp/chan108.txt）：

- **p125**：「肯定是片和片比」——MACD 柱面积要**分片**比较，同色才算一段，
  不是把正负混在一起累加。
- **p295**：「看 MACD 柱子的面积不需要全出来，一般柱子伸长的力度变慢时，
  把已经出现的面积乘 2，就可以当成是该段的面积。」——这就是折算规则。
- **p408**：「MACD 就可以了，一般人没必要用本 ID 那个指标……只要结合好中枢、
  背驰，95% 以上是一点问题没有的。」——MACD 口径被原文背书。
- **p11621（p368）**：回抽 0 轴只看黄白线「和什么柱子无关」；但背驰判定时
  「黄白线不创新高/新低 **或** 柱子面积变小，两个出现一个就要注意，两个都出现更标准」。
- **p66**：原文没有「多头排列」这个词（全书零命中），对应说法是「男上位/女上位」。
- **p522**：「MACD 是 MACD，和成交量没什么关系」——所以成交量不参与力度判定。

三个合法力度口径（原文口径，不含网络上流传的「时间×价格幅度」，p690 明确未采纳）：
1. 同色柱逐片面积（最常用）
2. 趋势平均力度 = 面积 ÷ 持续时间（p408 背书，时效性好）
3. 均线围合面积（「缠中说禅趋势力度」，最精确但需自编指标，只作交叉验证）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence


@dataclass(frozen=True)
class MacdState:
    """一段 K 线序列上的 MACD 三条线。

    注意 `hist` **不乘 2**。缠论里的「红绿柱」就是 DIF−DEA 本身，
    软件里常见的 ×2 只是显示需要；乘了会让所有面积口径整体放大一倍，
    阈值就得跟着调，容易在换指标时对不上。
    """

    dif: List[float]
    dea: List[float]
    hist: List[float]

    def __len__(self) -> int:
        return len(self.dif)


def macd_state(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> MacdState:
    """算出 DIF / DEA / 柱。柱 = DIF − DEA，不做 ×2。"""

    from .core import ema

    if not closes:
        return MacdState([], [], [])
    fast_line = ema(closes, fast)
    slow_line = ema(closes, slow)
    dif = [left - right for left, right in zip(fast_line, slow_line)]
    dea = ema(dif, signal)
    hist = [left - right for left, right in zip(dif, dea)]
    return MacdState(dif=dif, dea=dea, hist=hist)


@dataclass(frozen=True)
class EffortPiece:
    """一段同色 MACD 柱（「一片」）。原文 p125：片和片比。"""

    start_index: int
    end_index: int
    sign: int          # +1 红柱 / -1 绿柱
    area: float        # 已确定部分的面积（同色柱绝对值累加）
    extended: bool     # 末片是否还在伸长（面积按 ×2 折算）

    @property
    def bars(self) -> int:
        return self.end_index - self.start_index + 1


@dataclass(frozen=True)
class Effort:
    """一段走势的力度汇总。三个口径都在这里，供背驰判定交叉验证。"""

    direction: str                    # up / down
    start_index: int
    end_index: int
    pieces: List[EffortPiece] = field(default_factory=list)
    macd_area: float = 0.0            # 口径1：同色柱逐片面积（含折算）
    macd_piece_area: float = 0.0      # 未折算的原始面积（诊断用）
    peak_area: float = 0.0            # 最大单片面积
    dif_extreme: float = 0.0          # DIF 在段内的极值（正/负同向）
    dea_extreme: float = 0.0          # DEA 在段内的极值
    ma_extreme: float = 0.0           # 均线偏离度极值（交叉验证用）
    elapsed_bars: int = 0

    @property
    def bars(self) -> int:
        return max(1, self.end_index - self.start_index + 1)

    @property
    def average_effort(self) -> float:
        """口径2：趋势平均力度 = 面积 ÷ 持续时间（p408）。"""

        return self.macd_area / self.bars


def split_pieces(hist: Sequence[float], start_index: int, end_index: int) -> List[EffortPiece]:
    """把 [start_index, end_index] 切成同色片。

    原文 p125：「肯定是片和片比」——正负不能混着加，一红一绿相邻算两片。
    零值归到前一片（避免把一段连续柱切成碎片）。
    """

    pieces: List[EffortPiece] = []
    current_start: Optional[int] = None
    current_sign = 0
    current_area = 0.0

    for index in range(start_index, end_index + 1):
        value = hist[index]
        sign = 1 if value > 0 else (-1 if value < 0 else 0)
        if sign == 0:
            # 零柱不改变颜色，累加进当前片（面积仍按 0 算）
            if current_start is not None:
                current_area += 0.0
            continue
        if current_start is None or sign != current_sign:
            if current_start is not None:
                pieces.append(
                    EffortPiece(current_start, index - 1, current_sign, current_area, extended=False)
                )
            current_start = index
            current_sign = sign
            current_area = 0.0
        current_area += abs(value)

    if current_start is not None:
        pieces.append(
            EffortPiece(current_start, end_index, current_sign, current_area, extended=True)
        )
    return pieces


def measure_effort(
    macd: MacdState,
    start_index: int,
    end_index: int,
    direction: str,
    closes: Optional[Sequence[float]] = None,
    ma_extreme: Optional[float] = None,
    ma_period: int = 20,
) -> Effort:
    """计算一段走势的力度。

    末片（还在伸长的那片）按 p295 折算：已出现面积 ×2 当作该段面积。
    这样在最后阶段就能判出背驰，不必等整段走完。

    `ma_extreme` 传 None 时会用 `closes` 按 `direction` 算有向均线偏离；
    显式传入则跳过计算（调用方已经有值时用）。
    """

    left = max(0, min(start_index, end_index))
    right = min(len(macd.hist) - 1, max(start_index, end_index))
    if right < left or not macd.hist:
        return Effort(direction=direction, start_index=left, end_index=right)

    if ma_extreme is None:
        ma_extreme = (
            ma_deviation_extreme(closes, left, right, ma_period, direction) if closes else 0.0
        )

    pieces = split_pieces(macd.hist, left, right)
    total = 0.0
    raw_total = 0.0
    peak = 0.0
    for position, piece in enumerate(pieces):
        area = piece.area
        if piece.extended:
            area *= 2  # p295：柱子伸长的力度变慢时，把已出现的面积 ×2
        total += area
        raw_total += piece.area
        peak = max(peak, area)

    segment_dif = macd.dif[left : right + 1]
    segment_dea = macd.dea[left : right + 1]

    def extreme(values: Sequence[float]) -> float:
        if not values:
            return 0.0
        return max(values) if direction == "up" else min(values)

    return Effort(
        direction=direction,
        start_index=left,
        end_index=right,
        pieces=pieces,
        macd_area=total,
        macd_piece_area=raw_total,
        peak_area=peak,
        dif_extreme=extreme(segment_dif),
        dea_extreme=extreme(segment_dea),
        ma_extreme=ma_extreme,
        elapsed_bars=right - left + 1,
    )


def ma_deviation_extreme(
    closes: Sequence[float], start_index: int, end_index: int, period: int = 20, direction: str = "down"
) -> float:
    """口径3：收盘价对均线的最大**有向**偏离（均线围合面积的简化代理）。

    原文的「缠中说禅走势力度」用均线围合面积精确计算，需自编指标；
    这里用最大有向偏离做同向代理，只作**交叉验证**，不单独决定信号
    （p408：MACD 口径已足够，不必自编精确指标）。

    **必须带方向**（带符号返回）：判定要比较的是「这一段相对均线偏得多远」，
    上涨段看正向、下跌段看负向。写成 `max(正向, -负向)`（两边都取正数）
    就只比大小不记方向，ma_weaker 会恒为 False，均线这条背驰条件等于从未生效。
    """

    from .core import ema

    if not closes:
        return 0.0
    left = max(0, min(start_index, end_index))
    right = min(len(closes) - 1, max(start_index, end_index))
    if right < left or len(closes) < period:
        return 0.0
    ma = ema(closes, period)
    window = closes[left : right + 1]
    ma_window = ma[left : right + 1]
    if not window or not ma_window:
        return 0.0
    diffs = [(price - average) / average if average else 0.0 for price, average in zip(window, ma_window)]
    return max(diffs) if direction == "up" else min(diffs)


@dataclass(frozen=True)
class DivergenceVerdict:
    """背驰判定结果。三个条件各自记录，便于前端展示「哪里弱了」。"""

    is_divergent: bool
    dif_weaker: bool          # ① DIF/DEA 不创新高(新低)
    area_weaker: bool         # ② 同色柱面积变小
    ma_weaker: bool           # ③ 均线不创新高(新低)
    score: float              # 强度（0~1），用于排序
    previous: Effort
    current: Effort
    standard: bool            # ②③同时成立 = 标准背驰（p368「更标准」）

    @property
    def conditions_met(self) -> int:
        return sum((self.dif_weaker, self.area_weaker, self.ma_weaker))


def judge_divergence(
    previous: Effort,
    current: Effort,
    direction: str,
    area_ratio_threshold: float = 1.0,
    dif_margin: float = 0.0,
    ma_margin: float = 0.0,
    area_margin: float = 0.0,
) -> DivergenceVerdict:
    """判定后一段相对前一段是否背驰。

    三个条件（p368：「黄白线不创新高/新低 **或** 柱子面积变小，两个出现一个
    就要注意，两个都出现更标准」）：

    ① DIF/DEA 不创新高（新低）
    ② 同色柱面积变小
    ③ 均线不创新高（新低）

    任一成立即视为背驰；②与③同时成立记为**标准背驰**。

    四个阈值参数的语义统一为「**还要额外满足多少才认可**」，正值一律更严格：

    - `area_ratio_threshold`：面积容许的额外放大倍数（1.0 = 严格变小；1.1 = 允许大 10% 也算弱）
    - `area_margin`：面积至少还要缩小这个比例才算弱（0.05 = 需缩 5% 以上）
    - `dif_margin` / `ma_margin`：DIF/DEA 与均线需越过这个绝对容差

    判定式：`current < previous * (area_ratio_threshold - area_margin)`。
    之所以不用「相加」：那会让 margin 越大反而越容易判弱，与参数名相反。
    """

    if direction == "up":
        dif_weaker = current.dif_extreme < previous.dif_extreme - dif_margin
        dea_weaker = current.dea_extreme < previous.dea_extreme - dif_margin
        ma_weaker = current.ma_extreme < previous.ma_extreme - ma_margin
    else:
        dif_weaker = current.dif_extreme > previous.dif_extreme + dif_margin
        dea_weaker = current.dea_extreme > previous.dea_extreme + dif_margin
        ma_weaker = current.ma_extreme > previous.ma_extreme + ma_margin

    dif_condition = dif_weaker or dea_weaker
    area_threshold = previous.macd_area * max(0.0, area_ratio_threshold - area_margin)
    area_weaker = current.macd_area < area_threshold

    is_divergent = dif_condition or area_weaker or ma_weaker
    standard = area_weaker and ma_weaker

    # 强度：三个条件各占 1/3，再加上面积衰减的幅度
    base = conditions_met(dif_condition, area_weaker, ma_weaker) / 3.0
    ratio = current.macd_area / previous.macd_area if previous.macd_area else 1.0
    shrink = max(0.0, min(1.0, 1.0 - ratio))
    score = round(base * 0.7 + shrink * 0.3, 6)

    return DivergenceVerdict(
        is_divergent=is_divergent,
        dif_weaker=dif_condition,
        area_weaker=area_weaker,
        ma_weaker=ma_weaker,
        score=score,
        previous=previous,
        current=current,
        standard=standard,
    )


def conditions_met(dif_condition: bool, area_weaker: bool, ma_weaker: bool) -> int:
    return sum((bool(dif_condition), bool(area_weaker), bool(ma_weaker)))


def effort_to_meta(effort: Effort, label: str) -> Dict[str, object]:
    """把力度结果打包进信号 meta，供前端展示与排查。"""

    return {
        label: {
            "area": round(effort.macd_area, 6),
            "pieces": len(effort.pieces),
            "peak_area": round(effort.peak_area, 6),
            "average_effort": round(effort.average_effort, 6),
            "dif_extreme": round(effort.dif_extreme, 6),
            "dea_extreme": round(effort.dea_extreme, 6),
            "ma_extreme": round(effort.ma_extreme, 6),
            "bars": effort.bars,
        }
    }


def divergence_to_meta(verdict: DivergenceVerdict) -> Dict[str, object]:
    """背驰三条件的结果，存进 meta 让前端能解释「为什么算背驰」。"""

    return {
        "divergence_detail": {
            "is_divergent": verdict.is_divergent,
            "standard": verdict.standard,
            "dif_weaker": verdict.dif_weaker,
            "area_weaker": verdict.area_weaker,
            "ma_weaker": verdict.ma_weaker,
            "conditions_met": verdict.conditions_met,
            "score": verdict.score,
            "previous_area": round(verdict.previous.macd_area, 6),
            "current_area": round(verdict.current.macd_area, 6),
            "area_ratio": round(
                verdict.current.macd_area / verdict.previous.macd_area
                if verdict.previous.macd_area
                else 1.0,
                6,
            ),
            "previous_average": round(verdict.previous.average_effort, 6),
            "current_average": round(verdict.current.average_effort, 6),
        }
    }
