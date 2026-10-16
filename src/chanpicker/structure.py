"""中枢构建——严格按缠中说禅原文口径（p152 / p153）。

原文依据（《教你炒股票108课》张星星注解版，全文见 /tmp/chan108.txt）：

**中枢定义（p152）**
> 「某级别走势类型中，被**至少三个连续次级别走势类型**所重叠的部分，
> 称为缠中说禅走势中枢。」
>
> 「换言之，缠中说禅走势中枢就是至少三个连续次级别走势类型重叠部分所构成。」

**最低级别的退化定义（p152）**——这条对我们最关键：
> 「对**最后不能分解的级别**，其缠中说禅走势中枢就不能用『至少三个连续
> 次级别走势类型所重叠』定义，而定义为**至少三个该级别单位 K 线重叠部分**。」

我们的日线图就是「最后不能分解的级别」（再往下没有更细的本级别分解），
所以中枢 = 至少三段连续 K 线走势的重叠区间。这是本模块的主口径。

**盘整与趋势（p152）**
> 「盘整：某完成的走势类型**只包含一个**中枢，就称为该级别的盘整。」
> 「趋势：某完成的走势类型至少包含**两个以上依次同向**的中枢，就称为该级别的趋势。」

**分解定理（p153）**
> 「任何级别的任何走势类型，都至少由**三段以上次级别走势类型**构成。」

**与旧实现的根本差异**：旧代码用「3 笔重叠」定中枢，那是把笔当成了走势类型。
原文 p1351 专门强调「笔不能构成中枢」——笔是最低一级的结构，不含
「三个次级别运动」的含义。两者判定出的中枢位置、数量都会不同。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class TrendLeg:
    """次级别走势类型的一段（构成中枢与买卖点的统一单位）。

    对「最后不能分解的级别」（我们的日线）来说，一段就是连续 `min_legs`
    根 K 线走行形成的最小单元——即 p152 对最低级别中枢的定义
    （「至少三个该级别单位 K 线重叠部分」）所用的粒度。

    **为什么买卖点也必须用段而不是笔**：中枢按 p152 由「至少三个连续
    次级别走势类型」重叠而成（原文 p1351 专门强调「笔不能构成中枢」）。
    若中枢用段、买卖点用笔，两套索引体系会对不上——实测中枢终点在
    index 11、向上突破笔起点在 index 10，三买的「起点 >= 中枢终点」
    永远匹配不上，信号静默消失。口径必须统一在同一层级。
    """

    start_index: int
    end_index: int
    high: float
    low: float
    open_price: float = 0.0
    close_price: float = 0.0

    @property
    def direction(self) -> str:
        """段的走向：由起点收盘到终点收盘决定。"""

        return leg_direction(self.open_price, self.close_price)

    @property
    def bars(self) -> int:
        return self.end_index - self.start_index + 1

    @property
    def price_delta(self) -> float:
        """段的价差（力度回退时作为 MACD 面积的等价口径）。"""

        return abs(self.close_price - self.open_price)

    @property
    def is_up(self) -> bool:
        return self.direction == "up"

    @property
    def is_down(self) -> bool:
        return self.direction == "down"


def leg_direction(start_price: float, end_price: float) -> str:
    """一段走势的方向：终点相对起点。"""

    return "up" if end_price >= start_price else "down"


def leg_meta(leg: TrendLeg, effort: Optional[object] = None) -> Dict[str, Any]:
    """把一段次级别走势类型的起止坐标打包进信号 meta。

    与笔层面的 `segment_meta` 字段一致，前端画背驰段时可以复用同一套渲染逻辑。
    `effort` 是该段的力度（Effort 对象），带上后前端能直接在端点标出力度值，
    不必再去 meta 里翻 `previous_effort_detail` 之类的键。
    """

    data: Dict[str, Any] = {
        "start_index": leg.start_index,
        "start_price": leg.open_price,
        "end_index": leg.end_index,
        "end_price": leg.close_price,
        "high": leg.high,
        "low": leg.low,
        "direction": leg.direction,
    }
    if effort is not None:
        data["area"] = round(effort.macd_area, 6)
        data["average_effort"] = round(effort.average_effort, 6)
        data["bars"] = effort.bars
    return data


def build_legs(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Optional[Sequence[float]] = None,
    min_legs: int = 3,
) -> List[TrendLeg]:
    """把 K 线序列切成次级别走势类型段。

    「最后不能分解的级别」的构造法：连续取 `min_legs` 根 K 线为一段，
    取该段最高与最低作为段的高低点。这正是 p152 对最低级别中枢的定义
    （「至少三个该级别单位 K 线重叠部分」）所用的粒度。

    不足 `min_legs` 根的尾巴丢弃——凑不满三个单位 K 线就不构成一段，
    这与「至少三个」的要求一致。

    `closes` 传入时用于判定段方向（走势类型必须有方向，盘整/趋势才分得清）。
    """

    legs: List[TrendLeg] = []
    total = len(highs)
    index = 0
    while index + min_legs <= total:
        stop = index + min_legs
        open_price = closes[index] if closes is not None else highs[index]
        close_price = closes[stop - 1] if closes is not None else lows[stop - 1]
        legs.append(
            TrendLeg(
                start_index=index,
                end_index=stop - 1,
                high=max(highs[index:stop]),
                low=min(lows[index:stop]),
                open_price=open_price,
                close_price=close_price,
            )
        )
        index = stop
    return legs


@dataclass(frozen=True)
class Zhongshu:
    """缠中说禅走势中枢。

    与旧实现的同名类字段不同：新增 `leg_count`（由几个次级别走势类型构成）
    与 `state`（延伸 / 扩展 / 新生），用于区分 p152 定义的盘整与趋势。
    """

    start_index: int
    end_index: int
    zd: float
    zg: float
    leg_count: int = 3
    state: str = "new"  # new / extended / expanded

    @property
    def valid(self) -> bool:
        return self.zd <= self.zg

    @property
    def height(self) -> float:
        return self.zg - self.zd


def overlap_of(legs: Sequence[TrendLeg]) -> Optional[tuple[float, float]]:
    """一组走势类型的重叠区间。

    原文 p167 给的公式：走势类型 A、B、C 的高低点为 a1/a2、b1/b2、c1/c2，
    则「中枢的区间就是 (max(a2,b2,c2), min(a1,b1,c1))」。
    也就是：ZG = 各段高点的最小值，ZD = 各段低点的最大值。
    重叠为空（ZD > ZG）时返回 None——这三段不构成中枢。
    """

    if not legs:
        return None
    zg = min(leg.high for leg in legs)
    zd = max(leg.low for leg in legs)
    if zd > zg:
        return None
    return zd, zg


def detect_zhongshu_by_legs(
    legs: Sequence[TrendLeg],
    extend: bool = True,
) -> List[Zhongshu]:
    """按「至少三个连续次级别走势类型重叠」构建中枢。

    滑动窗口取每三个连续段的交集；`extend=True` 时继续向后合并
    所有仍与当前中枢区间有重叠的段（中枢延伸，p152：延伸时区间不变）。
    """
    centers: List[Zhongshu] = []
    if len(legs) < 3:
        return centers

    index = 0
    while index + 3 <= len(legs):
        window = legs[index : index + 3]
        overlap = overlap_of(window)
        if overlap is None:
            index += 1
            continue
        zd, zg = overlap
        end_leg = index + 2

        if extend:
            # 中枢延伸：后续段仍与 [zd, zg] 有重叠就并进来，区间**不变**
            probe = end_leg + 1
            while probe < len(legs):
                leg = legs[probe]
                if leg.low <= zg and leg.high >= zd:  # 与中枢区间有交集
                    end_leg = probe
                    probe += 1
                else:
                    break

        centers.append(
            Zhongshu(
                start_index=legs[index].start_index,
                end_index=legs[end_leg].end_index,
                zd=zd,
                zg=zg,
                leg_count=end_leg - index + 1,
                state="extended" if end_leg > index + 2 else "new",
            )
        )
        # 从中枢结束后继续，避免同一个中枢被反复计入
        index = end_leg + 1
    return centers


def classify_trend(centers: Sequence[Zhongshu], direction: str = "up") -> str:
    """按 p152 区分盘整与趋势。

    > 「盘整：完成的走势类型**只包含一个**中枢。」
    > 「趋势：至少包含**两个以上依次同向**的中枢。」

    这里用中枢在 K 线轴上的位置关系近似「依次同向」：两个以上中枢
    且后一个的高点不低于前一个（上涨方向），判为趋势。
    """

    if len(centers) < 2:
        return "consolidation"
    for previous, current in zip(centers, centers[1:]):
        if direction == "up" and current.zg < previous.zg:
            return "consolidation"
        if direction == "down" and current.zd > previous.zd:
            return "consolidation"
    return f"{direction}_trend"


def has_trend_divergence_leg(
    legs: Sequence[TrendLeg],
    current_index: int,
    centers: Sequence[Zhongshu],
    direction: str = "down",
    max_centers: int = 2,
) -> bool:
    """当前段之前是否构成「同向趋势」——p116「没有趋势，没有背驰」的前置条件。

    原文（p116，第 15 课《没有趋势没有背驰》）：
    > 「**没有趋势，没有背驰。在盘整中是无所谓『背驰』的**，这点是必须特别明确的。」

    判据：当前背驰段之前，若能找到 `max_centers` 个**依次同向**的中枢，
    才是趋势；只有一个中枢属于盘整，那里的背驰不算一买。
    这条限制是信号密度从 14 个/股压回可用范围的关键。

    `current_index` 是背驰段的起点；只看它之前的中枢，避免把后续形成的中枢
    拿来为已经发生的背驰背书。
    """

    earlier = [center for center in centers if center.end_index < current_index]
    if len(earlier) < max_centers:
        return False
    window = earlier[-max_centers:]
    if direction == "down":
        # 下跌趋势：后一个中枢的低点应更低
        return window[-1].zd < window[0].zd
    return window[-1].zg > window[0].zg


def merge_overlapping(centers: Sequence[Zhongshu]) -> List[Zhongshu]:
    """合并区间重叠的中枢（中枢扩展，p206 的三种命运之一）。

    延伸（区间不变、段数变多）在 detect_zhongshu_by_legs 里已处理；
    这里是处理「后一个中枢的区间与前一个有交叠」——按扩展处理，
    区间取两者的并集。
    """

    merged: List[Zhongshu] = []
    for center in centers:
        if merged and center.start_index <= merged[-1].end_index:
            previous = merged[-1]
            if center.zg >= previous.zd and center.zd <= previous.zg:
                merged[-1] = Zhongshu(
                    start_index=previous.start_index,
                    end_index=center.end_index,
                    zd=min(previous.zd, center.zd),
                    zg=max(previous.zg, center.zg),
                    leg_count=previous.leg_count + center.leg_count,
                    state="expanded",
                )
                continue
        merged.append(center)
    return merged
