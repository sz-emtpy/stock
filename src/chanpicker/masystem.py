"""均线买卖系统的「道」——男上位/女上位、吻、分类应对。

严格按缠中说禅原文实现（页码对应《教你炒股票108课》张星星注解版，
全文见 /tmp/chan108.txt）。

原文出处与规则
--------------

**p84 · 第12课《一吻何能消愁？》——分类与应对，这是「道」的骨架**
> 「例如，对于一个简单的，由 **5 日均线与 10 日均线**构成的买卖系统，
> 首先，两者的体位构成一个完全分类：女上位是牛，男上位是熊。」
> 「**对于任何走势，首要判断的是体位：男上位还是女上位。**」

**p98 · 《买入卖出机制（1）》——吻的三种与买卖点定性**
> 「**飞吻**：短期均线略略走平后继续按原来趋势进行下去。」
> 「**唇吻**：短期均线靠近长期均线但不跌破或升破，然后按原来趋势继续下去。」
> 「**湿吻**：短期均线跌破或升破长期均线甚至出现反复缠绕，如胶似漆。」
> 「**女上位**：短期均线在长期均线之上。」**男上位**：短期均线在长期均线之下。」
>
> 「**第一类买点**：由**男上位最后一吻后**出现的**背驰式下跌**构成。」
> 「**第二类买点**：**女上位第一吻后**出现的下跌构成。」

**p98 · 原文对一买的数量要求**
> 「前面已经说过，这第一吻后的下跌一般不会构成买点，
> **必须是至少第二吻以后**。」（p98）
> 星注补例：「男上位第二吻和第三吻很明显处于同一个箱体……
> 你可以认为是 a+A+b+B+c，演化成了 a+A1。**但是，如果将第一吻前面部分走势
> 拿过来组合，就可以形成一个与下方同级别的箱体，所以也可以形成趋势一买。**」
>
> 也就是说：一吻**不必然排除**——若前面的走势能重组出同级别箱体，一吻后同样可成
> 趋势一买。这是原文留的口子，实现里要保留。

**p85 · 中继与转折的概率递进**
> 「女上位趋势出现的**第一次缠绕是中继的可能性极大**，如果是**第三、四次**出现，
> 这个缠绕是**转折的可能性就会加大**。」
> 「**女上位选择第一次出现缠绕的中继情况，而男上位的就相反，要寻找最后一次缠绕的
> 转折情况**，其后如果出现急跌却背弛，那是最佳的买入时机。
> **抄底不是不可以，但只能选择这种情况。**」
> 「没有人百分百确认那是最后一次缠绕，一般，**男上位后的第一次缠绕肯定不是，
> 从第二次开始都有可能**。」

**p100 · 买点定律（级别递归）**
> 「大级别的第二类买点由**次一级别相应走势的第一类买点**构成……
> 有了这个定律，**所有的买点都可以归结到第一类买点**。」

**p522 · 成交量不是必要条件**
> 「MACD 是 MACD，**和成交量没什么关系**。」——所以成交量只作辅助加分，
> 不作为一买的必要条件（原文 p85 提到量能过大易骗线，那是风险提示，不是判定条件）。

实现策略
--------
原文的均线系统是 5 日 / 10 日。工程上按周期缩放：`short_period` / `long_period`
默认取 `max(3, round(min_bars/40))` 之类——但为了让规则与原文**结构相通**
而非参数巧合，这里允许显式配置，默认在 `ChanConfig` 里给日线 5/10 的对应比例。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .indicators import ema

# 原文的三种吻（p98）
KISS_FLY = "fly"        # 飞吻：短期均线略走平后继续原趋势
KISS_LIP = "lip"        # 唇吻：短期均线靠近但不跌破/升破
KISS_WET = "wet"        # 湿吻：短期均线跌破/升破，甚至反复缠绕

# 体位（p98）
POSITION_FEMALE = "female"   # 女上位：短期均线在长期均线之上
POSITION_MALE = "male"       # 男上位：短期均线在长期均线之下
POSITION_TANGLE = "tangle"   # 缠绕：两者反复穿越，体位未定

# 缠绕性质（p85）
NATURE_CONTINUE = "relay"    # 中继：延续原体位
NATURE_REVERSE = "reverse"   # 转折：改变体位


@dataclass(frozen=True)
class Kiss:
    """一次「吻」= 一次缠绕事件。

    原文的三种吻在工程上按「短期均线与长期均线的关系」区分：
    - 飞吻：距离先收窄（走平）再重新拉开，方向不变
    - 唇吻：距离收窄到很近但**没有穿越**
    - 湿吻：发生了**穿越**（或反复穿越）
    """

    index: int                 # 这是第几次吻（从 1 开始）
    kind: str                   # fly / lip / wet
    start_index: int
    end_index: int
    start_date: str
    end_date: str
    min_distance: float         # 期间两线的最小相对距离
    crossings: int              # 穿越次数：0=唇吻, 1=湿吻
    position: str               # 吻发生时所处的体位（男上位/女上位）
    strength: float = 0.0       # 吻之前的走势力度（0~1，越大越有力）

    @property
    def is_crossing(self) -> bool:
        """湿吻才是真正的穿越；唇吻只是靠近。"""

        return self.crossings > 0


@dataclass(frozen=True)
class MaSystemState:
    """某一时点的均线买卖系统状态。"""

    date: str
    index: int
    short_ma: float
    long_ma: float
    position: str
    position_duration: int      # 当前体位已持续的 K 线数


@dataclass
class MaKissTracker:
    """追踪一串「吻」。

    原文的判定流程是：
    1. 先看**体位**（男上位还是女上位）——p84「首要判断的是体位」
    2. 再数这是**第几吻**——p98「必须是至少第二吻以后」
    3. 一买要**最后一吻之后**的背驰下跌——p98
    4. 二买要**女上位第一吻之后**的下跌——p98
    """

    short_period: int
    long_period: int
    short_ma: List[float] = field(default_factory=list)
    long_ma: List[float] = field(default_factory=list)
    position: str = POSITION_TANGLE
    kisses: List[Kiss] = field(default_factory=list)
    _open_index: Optional[int] = None
    _min_distance: float = float("inf")
    _crossings: int = 0
    _position_at_open: str = POSITION_TANGLE
    _strength: float = 0.0

    # 阈值：|短期-长期| / 长期 小于它算「缠绕中」，大于它算「体位明确」。
    # 取值参考：日线上 5/10 日均线的日常贴合度约 0.5%~2%，
    # 真正进入缠绕（多空胶着）通常在 1% 以内。3% 太松——会把整段盘整
    # 合并成一个「吻」（实测浦发银行 400 根只出 1 个吻、穿越 10 次），
    # 而原文是逐次计数（一吻/二吻/第三吻…）。
    tangle_threshold: float = 0.01
    # 一根 K 线都没缠住就断开、且间隔很短的，算噪声不算吻（p98 三种吻都是
    # 有意义的缠绕事件）
    min_kiss_bars: int = 2

    def __post_init__(self) -> None:
        if self.short_period >= self.long_period:
            raise ValueError(
                f"短期均线周期({self.short_period}) 必须小于长期({self.long_period})"
            )

    @classmethod
    def build(cls, closes: Sequence[float], short_period: int, long_period: int) -> "MaKissTracker":
        tracker = cls(short_period=short_period, long_period=long_period)
        tracker.short_ma = ema(list(closes), short_period)
        tracker.long_ma = ema(list(closes), long_period)
        return tracker

    def _distance(self, index: int) -> Optional[float]:
        """两线在 index 处的相对距离 |短期-长期|/长期。"""

        long_value = self.long_ma[index]
        if not long_value:
            return None
        return abs(self.short_ma[index] - long_value) / long_value

    def run(self, dates: Sequence[str]) -> None:
        """扫一遍 K 线，产出体位序列与「吻」列表。"""

        # 先跳过 EMA 未稳定的开头
        start = max(self.short_period, self.long_period)
        if len(dates) <= start:
            return

        position_duration = 0
        for index in range(start, len(dates)):
            long_ma = self.long_ma[index]
            short_ma = self.short_ma[index]
            if not long_ma:
                continue
            distance = self._distance(index)
            if distance is None:
                continue

            # --- 体位判定（p84）---
            if short_ma > long_ma:
                new_position = POSITION_FEMALE
            elif short_ma < long_ma:
                new_position = POSITION_MALE
            else:
                new_position = POSITION_TANGLE

            tangled = distance <= self.tangle_threshold

            # --- 吻的开启与结束 ---
            if tangled and self._open_index is None:
                # 开始一次缠绕
                self._open_index = index
                self._min_distance = distance
                self._crossings = 0
                self._position_at_open = new_position
                self._strength = self._strength_at(index)
            elif tangled and self._open_index is not None:
                self._min_distance = min(self._min_distance, distance)
                # 穿越计数：短期均线在长期均线两侧切换
                previous_sign = 1 if self.short_ma[index - 1] > self.long_ma[index - 1] else -1
                current_sign = 1 if short_ma > long_ma else -1
                if index > start and previous_sign != current_sign:
                    self._crossings += 1
            elif not tangled and self._open_index is not None:
                # 缠绕结束，记录这一吻（太短的算噪声丢弃）
                if index - self._open_index >= self.min_kiss_bars:
                    self._append_kiss(self._open_index, index - 1, dates)
                self._open_index = None

            # --- 体位持续计数 ---
            if new_position == self.position and not tangled:
                position_duration += 1
            else:
                position_duration = 0
            self.position = new_position

    def _strength_at(self, index: int) -> float:
        """吻之前的走势力度：这段涨幅/跌幅相对于近期均值的强度。

        p85 明确提到「出现第一次缠绕前，5 日线的走势必须是十分有力的，
        不能是疲软的玩意」——所以要衡量吻之前的推动力度。归一化到 0~1。
        """

        lookback = max(3, self.short_period)
        begin = max(0, index - lookback)
        if index <= begin:
            return 0.0
        net = abs(self.short_ma[index] - self.short_ma[begin])
        # 用最近波动幅度做基准，避免不同波动率下不可比
        changes = [
            abs(self.short_ma[i] - self.short_ma[i - 1])
            for i in range(begin + 1, index + 1)
        ]
        typical = sum(changes) / len(changes) if changes else 0.0
        if typical <= 0:
            return 0.0
        return min(1.0, net / (typical * lookback))

    def _append_kiss(self, start_index: int, end_index: int, dates: Sequence[str]) -> None:
        kind = self._classify_kiss(self._crossings, self._min_distance, start_index)
        self.kisses.append(
            Kiss(
                index=len(self.kisses) + 1,
                kind=kind,
                start_index=start_index,
                end_index=end_index,
                start_date=dates[start_index] if start_index < len(dates) else "",
                end_date=dates[end_index] if end_index < len(dates) else "",
                min_distance=round(self._min_distance, 6),
                crossings=self._crossings,
                position=self._position_at_open,
                strength=round(self._strength, 4),
            )
        )

    def _classify_kiss(self, crossings: int, min_distance: float, start_index: int) -> str:
        """p98 三种吻的区分。

        - **湿吻**：发生了穿越（短期均线跌破或升破长期均线）
        - **唇吻**：靠得很近但没穿越
        - **飞吻**：短暂走平后原趋势继续（距离收窄但未达缠绕阈值）

        这里按「穿越次数」优先判湿吻/唇吻；都没穿越时，
        用最小距离区分飞吻（更远）与唇吻（很近）。
        """

        if crossings > 0:
            return KISS_WET
        if min_distance <= self.tangle_threshold * 0.4:
            return KISS_LIP
        return KISS_FLY

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def position_at(self, index: int) -> str:
        """某根 K 线所处的体位。"""

        if index < len(self.short_ma) and index < len(self.long_ma):
            if not self.long_ma[index]:
                return POSITION_TANGLE
            return POSITION_FEMALE if self.short_ma[index] > self.long_ma[index] else POSITION_MALE
        return POSITION_TANGLE

    def kisses_before(self, index: int) -> List[Kiss]:
        """某根 K 线之前已经完成的吻（按时间序）。"""

        return [k for k in self.kisses if k.end_index < index]

    def kiss_covering(self, index: int) -> Optional[Kiss]:
        """把 index 包含在内的那个吻（若 index 落在某次缠绕过程中）。

        判定买点时必须先排掉这种情况：如果背驰低点正好落在一次**尚未结束**的
        缠绕内部，`kisses_before` 只会返回更早的吻，误以为「已经离开最后一吻」，
        从而把一个仍在缠绕中的位置放行。
        """

        for kiss in self.kisses:
            if kiss.start_index <= index <= kiss.end_index:
                return kiss
        return None

    def last_kiss_before(self, index: int) -> Optional[Kiss]:
        """紧邻 index 之前的最后一吻。

        p98：一买要「**最后一吻后**」出现的背驰式下跌。
        """

        done = self.kisses_before(index)
        return done[-1] if done else None

    def last_kiss_of_position_before(self, index: int, position: str) -> Optional[Kiss]:
        """某个体位下的最后一吻。"""

        done = [k for k in self.kisses_before(index) if k.position == position]
        return done[-1] if done else None

    def first_kiss_of_position_before(self, index: int, position: str) -> Optional[Kiss]:
        """某个体位下的第一吻（p98 二买：「女上位**第一吻**后」）。"""

        done = [k for k in self.kisses_before(index) if k.position == position]
        return done[0] if done else None

    def kiss_count_of_position(self, position: str) -> int:
        """某个体位下累计出现的吻数。"""

        return sum(1 for k in self.kisses if k.position == position)

    def kiss_run_count_before(self, index: int, position: str) -> int:
        """index 之前，**当前这一轮同体位**里连续出现的吻数。

        p98 说的「必须是至少第二吻以后」，指的是**这一轮男上位里**
        经历了几次缠绕，不是全程累计次数。早先误用 `kiss_count_of_position`
        （全程累计），会导致：第一轮男上位的第一个吻之后，背驰低点被
        误判为「已经有过多次吻」而放行——实测就把本该拒掉的信号放了过去。
        """

        run = 0
        for kiss in self.kisses_before(index):
            if kiss.position == position:
                run += 1
            else:
                # 遇到异体位的吻，说明这一轮结束了，重新计数
                run = 0
        return run


@dataclass(frozen=True)
class BuyPointRule:
    """一条买点的原文规则判定结果。"""

    kind: str                       # FIRST_BUY / SECOND_BUY
    satisfied: bool
    reasons: List[str] = field(default_factory=list)
    violations: List[str] = field(default_factory=list)
    kiss_index: int = 0
    position: str = ""
    meta: dict = field(default_factory=dict)

    def to_meta(self) -> dict:
        return {
            "rule_kind": self.kind,
            "rule_satisfied": self.satisfied,
            "rule_position": self.position,
            "rule_kiss_index": self.kiss_index,
            "rule_reasons": list(self.reasons),
            "rule_violations": list(self.violations),
            **self.meta,
        }


def judge_first_buy(
    tracker: MaKissTracker,
    divergence_index: int,
    segment_low_index: int,
    below_center: bool,
    min_kisses: int = 1,
    require_last_kiss: bool = True,
) -> BuyPointRule:
    """按 p98 判定一买：「男上位最后一吻后出现的背驰式下跌」。

    参数
    ----
    `divergence_index` 背驰段确认所在的 K 线索引
    `segment_low_index` 背驰段的低点 K 线索引
    `below_center` 该低点是否在本级别中枢之下（p208 用 ZG，一买用 ZD）
    `min_kisses` 男上位至少要有几吻才认可
        原文 p98 说「必须是至少第二吻以后」= min_kisses=2；
        星注同时给了「一吻若能与前面走势重组出同级别箱体，也可以形成趋势一买」
        的例外。默认取 1 表示**一吻也认**（放宽，但结构相通），
        由调用方按需改成 2。
    `require_last_kiss` 是否要求这次背驰发生在「最后一吻之后」。
        原文用「最后一吻」，但同一时点无法知道某吻是不是真的最后一吻
        （p85：「没有人百分百确认那是最后一次缠绕」），
        所以工程上放宽为「最近的吻之后」——即倒着找最后一吻。

    返回的 `violations` 列出未满足的原文条件，便于前端说明「差在哪」。
    """

    reasons: List[str] = []
    violations: List[str] = []

    position = tracker.position_at(divergence_index)
    if position == POSITION_MALE:
        reasons.append("男上位（p84：首要判断体位）")
    else:
        violations.append(
            f"不在男上位（当前 {position}），p98 一买要求「男上位最后一吻后」"
        )

    last_kiss = tracker.last_kiss_of_position_before(segment_low_index, POSITION_MALE)
    kiss_index = 0
    covering = tracker.kiss_covering(segment_low_index)
    if covering is not None:
        violations.append(
            f"背驰低点仍落在第 {covering.index} 吻的缠绕过程中（{covering.start_date}~"
            f"{covering.end_date}），尚未脱离缠绕"
        )
    elif last_kiss is None:
        violations.append("此前没有出现过男上位的吻，p98 要求「最后一吻之后」")
    else:
        kiss_index = last_kiss.index
        if last_kiss.end_index >= segment_low_index:
            violations.append("背驰低点不在最后一吻之后")
        else:
            reasons.append(
                f"男上位第 {last_kiss.index} 吻之后（{last_kiss.end_date} 结束）出现背驰下跌"
            )

    male_kisses = tracker.kiss_run_count_before(segment_low_index, POSITION_MALE)
    if male_kisses >= min_kisses:
        reasons.append(f"本轮男上位已出现 {male_kisses} 吻（阈值 {min_kisses}）")
    else:
        violations.append(
            f"本轮男上位只有 {male_kisses} 吻，少于要求的 {min_kisses} 吻"
            "（p98：「必须是至少第二吻以后」）"
        )

    if not below_center:
        violations.append("低点未跌破本级别中枢 ZD，不构成趋势背驰")

    satisfied = not violations
    return BuyPointRule(
        kind="FIRST_BUY",
        satisfied=satisfied,
        reasons=reasons,
        violations=violations,
        kiss_index=kiss_index,
        position=position,
        meta={
            "rule_male_kisses": male_kisses,
            "rule_last_kiss_date": last_kiss.end_date if last_kiss else "",
        },
    )


def judge_second_buy(
    tracker: MaKissTracker,
    first_buy_index: int,
    pullback_low_index: int,
    holds_first_buy_low: bool,
) -> BuyPointRule:
    """按 p98 判定二买：「女上位第一吻后出现的下跌」。

    p100 买点定律从「级别」角度把二买归结为一买；本函数实现的是 p98
    那句更操作化的定性——**女上位第一吻之后的下跌**。
    """

    reasons: List[str] = []
    violations: List[str] = []

    position = tracker.position_at(pullback_low_index)
    if position == POSITION_FEMALE:
        reasons.append("女上位（p84：首要判断体位）")
    else:
        violations.append(
            f"不在女上位（当前 {position}），p98 二买要求「女上位第一吻后」"
        )

    covering = tracker.kiss_covering(pullback_low_index)
    if covering is not None:
        violations.append(
            f"回调低点仍落在第 {covering.index} 吻的缠绕过程中，尚未脱离缠绕"
        )
    first_kiss = tracker.first_kiss_of_position_before(pullback_low_index, POSITION_FEMALE)
    if first_kiss is None:
        violations.append("此前没有出现过女上位的吻，p98 要求「第一吻后」")
        kiss_index = 0
    else:
        kiss_index = first_kiss.index
        if first_kiss.end_index >= pullback_low_index:
            violations.append("回调低点不在女上位第一吻之后")
        else:
            reasons.append(
                f"女上位第 {first_kiss.index} 吻之后（{first_kiss.end_date} 结束）出现下跌"
            )

    if not holds_first_buy_low:
        violations.append("回调跌破一买低点，不构成二买")

    satisfied = not violations
    return BuyPointRule(
        kind="SECOND_BUY",
        satisfied=satisfied,
        reasons=reasons,
        violations=violations,
        kiss_index=kiss_index,
        position=position,
        meta={
            "rule_female_kisses": female_kiss_count(tracker),
            "rule_first_kiss_date": first_kiss.end_date if first_kiss else "",
        },
    )


def male_kiss_count(tracker: MaKissTracker) -> int:
    return tracker.kiss_count_of_position(POSITION_MALE)


def female_kiss_count(tracker: MaKissTracker) -> int:
    return tracker.kiss_count_of_position(POSITION_FEMALE)


# ----------------------------------------------------------------------
# p100 买点定律：大级别的第二类买点由次一级别相应走势的第一类买点构成
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class SublevelBuyConfirmation:
    """次级别一买对大级别二买的验证结果（p100）。

    原文（p100/p101）：
    > 「**大级别的第二类买点由次一级别相应走势的第一类买点构成。**
    > 例如，周线上的第二类买点由日线上相应走势的第一类买点构成。
    > 有了这个缠中说禅买点定律，**所有的买点都可以归结到第一类买点**。」

    落地上不能直接用它替换同级别判定——p100 的「周线二买 ← 日线一买」需要
    真正的次级别数据，而数据源对分钟周期只保留约 9 个交易日。所以这里做成
    **验证层**：同级别口径先出候选，再拿次级别一买做二次确认，两者在 meta 里
    同时保留，便于对照与回溯。
    """

    symbol: str
    available: bool
    sublevel_period: str
    sublevel_first_buys: list = field(default_factory=list)
    matched_date: Optional[str] = None
    days_back: Optional[int] = None
    message: str = ""

    def to_meta(self) -> dict:
        return {
            "sublevel_rule": {
                "available": self.available,
                "sublevel_period": self.sublevel_period,
                "sublevel_first_buy_count": len(self.sublevel_first_buys),
                "sublevel_matched_date": self.matched_date,
                "sublevel_days_back": self.days_back,
                "sublevel_message": self.message,
            }
        }


def confirm_by_sublevel_first_buy(
    symbol: str,
    second_buy_date: str,
    sublevel_bars: Optional[Sequence[Any]],
    sublevel_period: str = "m30",
    engine=None,
    lookback_days: int = 5,
) -> SublevelBuyConfirmation:
    """用次级别一买验证某个二买（p100 买点定律）。

    参数
    ----
    `second_buy_date` 大级别二买的信号日（形如 `2026-07-14`）
    `sublevel_bars` 该股的次级别 K 线；为空则无法验证（`available=False`）
    `lookback_days` 二买之后多少个交易日内的次级别一买算作「相应走势」

    原文说「**相应走势**的第一类买点」，也就是同一段走势里、时点最近的那个
    次级别一买，所以按时间倒着找：`second_buy_date` 当天或其后 `lookback_days`
    个交易日内，最近的一个次级别一买。
    """

    if not sublevel_bars:
        return SublevelBuyConfirmation(
            symbol=symbol,
            available=False,
            sublevel_period=sublevel_period,
            message=f"本地无 {sublevel_period} 数据，无法按 p100 买点定律做次级别验证",
        )

    if engine is not None:
        sub_signals = [s for s in engine.scan(sublevel_bars).signals if s.kind.value == "FIRST_BUY"]
    else:
        sub_signals = []

    base = str(second_buy_date)[:10]
    base_day = base.replace("-", "")
    # 次级别时间戳形如 `2026-07-14 10:30`，取日期部分比较
    sub_dates = [(str(s.date)[:10], str(s.date)) for s in sub_signals]
    ordered = sorted(sub_dates, key=lambda pair: pair[1], reverse=True)

    for day, raw_date in ordered:
        compact = day.replace("-", "")
        if compact < base_day:
            # 次级别一买早于二买 → 不属于「相应走势」
            continue
        gap = _trading_day_gap(base_day, compact)
        if gap is None or gap > lookback_days:
            continue
        return SublevelBuyConfirmation(
            symbol=symbol,
            available=True,
            sublevel_period=sublevel_period,
            sublevel_first_buys=ordered,
            matched_date=raw_date,
            days_back=gap,
            message=(
                f"p100 买点定律已验证：{sublevel_period} 于 {raw_date} 出现一买，"
                f"构成该日线二买的次级别依据"
            ),
        )

    return SublevelBuyConfirmation(
        symbol=symbol,
        available=True,
        sublevel_period=sublevel_period,
        sublevel_first_buys=ordered,
        message=(
            f"未在二买之后 {lookback_days} 个交易日内找到 {sublevel_period} 一买，"
            "p100 买点定律未获次级别验证"
        ),
    )


def _trading_day_gap(base_compact: str, other_compact: str) -> Optional[int]:
    """两个 `YYYYMMDD` 之间相差的自然日（粗粒度足够，只用于窗口过滤）。"""

    from datetime import date

    try:
        base = date(int(base_compact[:4]), int(base_compact[4:6]), int(base_compact[6:8]))
        other = date(int(other_compact[:4]), int(other_compact[4:6]), int(other_compact[6:8]))
    except (ValueError, IndexError):
        return None
    return (other - base).days


# ----------------------------------------------------------------------
# 卖点：原文说「卖就反过来就可以了」（p84），故与买点严格镜像。
# 一卖 = 女上位最后一吻后出现的背驰式上涨
# 二卖 = 男上位第一吻后出现的反弹
# ----------------------------------------------------------------------


def judge_first_sell(
    tracker: MaKissTracker,
    divergence_index: int,
    segment_high_index: int,
    above_center: bool,
) -> BuyPointRule:
    """一卖：一买的镜像（p84「买的方式明白了，卖就反过来就可以了」）。

    结构与 `judge_first_buy` 完全对称，只是把「男上位最后一吻后的下跌」
    换成「女上位最后一吻后的上涨」。
    """

    reasons: List[str] = []
    violations: List[str] = []

    position = tracker.position_at(divergence_index)
    if position == POSITION_FEMALE:
        reasons.append("女上位（一卖为一买镜像）")
    else:
        violations.append(f"不在女上位（当前 {position}），一卖要求「女上位最后一吻后」")

    last_kiss = tracker.last_kiss_of_position_before(segment_high_index, POSITION_FEMALE)
    kiss_index = 0
    covering = tracker.kiss_covering(segment_high_index)
    if covering is not None:
        violations.append(
            f"背驰高点仍落在第 {covering.index} 吻的缠绕过程中，尚未脱离缠绕"
        )
    elif last_kiss is None:
        violations.append("此前没有出现过女上位的吻")
    else:
        kiss_index = last_kiss.index
        if last_kiss.end_index >= segment_high_index:
            violations.append("背驰高点不在最后一吻之后")
        else:
            reasons.append(
                f"女上位第 {last_kiss.index} 吻之后（{last_kiss.end_date} 结束）出现背驰上涨"
            )

    if not above_center:
        violations.append("高点未升破本级别中枢 ZG，不构成趋势背驰")

    satisfied = not violations
    return BuyPointRule(
        kind="FIRST_SELL",
        satisfied=satisfied,
        reasons=reasons,
        violations=violations,
        kiss_index=kiss_index,
        position=position,
        meta={"rule_female_kisses": female_kiss_count(tracker)},
    )


def judge_second_sell(
    tracker: MaKissTracker,
    pullback_high_index: int,
    below_first_sell_high: bool,
) -> BuyPointRule:
    """二卖：一买的镜像——「男上位第一吻后出现的反弹」。"""

    reasons: List[str] = []
    violations: List[str] = []

    position = tracker.position_at(pullback_high_index)
    if position == POSITION_MALE:
        reasons.append("男上位（二卖为一买镜像）")
    else:
        violations.append(f"不在男上位（当前 {position}），二卖要求「男上位第一吻后」")

    covering = tracker.kiss_covering(pullback_high_index)
    if covering is not None:
        violations.append(
            f"反弹高点仍落在第 {covering.index} 吻的缠绕过程中，尚未脱离缠绕"
        )
    first_kiss = tracker.first_kiss_of_position_before(pullback_high_index, POSITION_MALE)
    if first_kiss is None:
        violations.append("此前没有出现过男上位的吻")
        kiss_index = 0
    else:
        kiss_index = first_kiss.index
        if first_kiss.end_index >= pullback_high_index:
            violations.append("反弹高点不在男上位第一吻之后")
        else:
            reasons.append(
                f"男上位第 {first_kiss.index} 吻之后（{first_kiss.end_date} 结束）出现反弹"
            )

    if not below_first_sell_high:
        violations.append("反弹超过一卖高点，二卖不成立")

    satisfied = not violations
    return BuyPointRule(
        kind="SECOND_SELL",
        satisfied=satisfied,
        reasons=reasons,
        violations=violations,
        kiss_index=kiss_index,
        position=position,
        meta={"rule_male_kisses": male_kiss_count(tracker)},
    )
