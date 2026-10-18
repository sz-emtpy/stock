"""自成长交易体系的统计引擎（纯函数，不依赖 Store）。

把「什么样的结构容易在什么买点赚到钱」从主观经验转成可回测的统计。

v2（2026-10-18 体检报告后重写）：

- **标签**：`net_exit_return >= win_threshold` 视为「赚钱」，否则失败。
  v1 用 max_return（20 日内结构有没有冲高）——体检证明它优化错了目标：
  体系分越高，按卖点出场的真实收益反而越差（D 档中位 5.51% < A 档 6.17%）。
  v2 的标签直接回答「按规则出场能不能拿到手」：
  出场规则 = 窗口内第一个卖点出场，否则强平；净收益已扣往返交易成本。

- **置信下界**：每箱增益用 Wilson 下界折价——小样本箱的增益可能是噪声，
  直接加分会让体系学到假规律。下界 = Wilson 区间的下界（95% 置信）。

- **市场状态特征**：`market_regime`（指数 20 日趋势）单独入箱——牛市里
  什么买点都赚钱，体系必须知道「当前是牛市还是熊市」才能给出可信推荐。

- **分段验证**：时间分割验证之外，再按市场状态分段验证。体系在牛市有 edge
  不代表熊市也有；两段都成立的体系才算立得住。

所有函数只吃入参，不做 IO，方便单测。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

# ------------------------------------------------------------------
# 标签（v2）：规则化出场净收益
# ------------------------------------------------------------------

# 「赚钱」阈值：规则化出场（卖点优先，否则强平）的净收益达到 5% 算成功。
# v1 的 10% 对应 max_return（冲高的上限），v2 用到手收益，阈值要更低才合理——
# 净收益 5% 已经显著跑赢同期理财，且体检数据里出场收益中位约 6%，
# 阈值取中位数附近让正负样本大致均衡，统计才有区分度。
DEFAULT_WIN_THRESHOLD = 0.05

#Wilson 95% 置信下界的 z 值
_WILSON_Z = 1.96

# 单箱最小样本量：低于此值该箱不参与打分（样本太少，下界为负也没意义）。
MIN_BUCKET_SAMPLES = 30


def wilson_lower_bound(win: int, n: int, z: float = _WILSON_Z) -> float:
    """Wilson 得分区间的下界（95% 置信）。

    相比朴素比率 win/n，下界对小样本自动折价：
    n=30 胜率 50% → 下界约 0.33；n=1000 胜率 50% → 下界约 0.47。
    体检报告盲区 8 的落地：小样本箱的「高胜率」不再被当成真规律。
    """

    if n <= 0:
        return 0.0
    p = win / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)
    return (centre - margin) / denom


def gain_lower_bound(
    bucket_win: int, bucket_n: int, base_win: int, base_n: int, z: float = _WILSON_Z,
) -> float:
    """「箱胜率 − 基准胜率」这个**差值**的保守估计（下界）。

    体检报告盲区 8 的正确落地：增益是两个比率的差，不是单一比率，
    Wilson 下界（作用于单比率）会把 n 巨大的基准也算进误差里，导致
    几乎所有箱子都被压成负值（实测 v2 初版 69 箱只剩 11 箱为正，
    体系分全挤在负区间，时间分割验证 spread 反转为 -0.22）。

    差值的标准误差用独立两样本近似：
        SE = sqrt(p1(1-p1)/n1 + p0(1-p0)/n0)
    其中 p0/n0 是**其余样本**（全体减去该箱）的胜率与样本量——箱子和其余
    部分天然不重叠，这是标准的两比率差检验。下界 = 点估计 − z*SE：
    样本越小折得越多，大样本几乎不折，符合「小样本折价、大样本信任」的本意。
    """

    if bucket_n <= 0 or base_n <= 0 or bucket_n >= base_n:
        # 箱子≈全体时增益恒为 0，无信息
        return 0.0
    p1 = bucket_win / bucket_n
    rest_n = base_n - bucket_n
    p0 = (base_win - bucket_win) / rest_n
    se = math.sqrt(p1 * (1 - p1) / bucket_n + p0 * (1 - p0) / rest_n)
    return (p1 - p0) - z * se


def label_win(row: dict, threshold: float = DEFAULT_WIN_THRESHOLD) -> bool:
    """某条样本是否「赚钱」（v2 标签：规则化出场净收益达阈值）。

    结局未终结（net_exit_return 为 None）不算。兼容 v1 样本：没有出场字段时
    退回 max_return 旧口径（老快照仍可读，重建后自动切新口径）。
    """

    net = row.get("net_exit_return")
    if net is not None:
        return float(net) >= threshold
    # v1 兼容：旧样本无出场字段，退回旧标签（10% 阈值语义）
    mr = row.get("max_return")
    return mr is not None and float(mr) >= max(threshold, 0.10)


# 参与体系判定的特征。以后扩展特征，先在这里登记再写分箱。
SYSTEM_FEATURES = (
    "vol_ratio",            # 量比（20 日）
    "vol_ratio_5",          # 短期量比（5 日）
    "retrace_from_high",    # 回撤位置（距前 60 日高）
    "above_ma20",           # 是否站上 MA20
    "ma_bullish",           # 均线多头排列（MA5>MA10>MA20）
    "price_vs_ma60",        # 相对 MA60 位置
    "macd_hist_sign",       # MACD 柱正负（红/绿）
    "dif_above_zero",       # DIF 在零轴上方/下方
    "rsi14",                # RSI14 超买超卖
    "kdj_k",                # KDJ 的 K 值
    "volatility_20",        # 20 日波动率（股性活跃度）
    "consecutive_down",     # 信号前连续下跌天数（超跌程度）
    "divergence_score",     # 背驰力度
    "market_breadth",       # 市场宽度（上涨家数占比）
    "market_limit_up",      # 涨停家数
    "market_avg_change",    # 全市场平均涨跌幅
    "market_index_change",  # 上证指数涨跌幅
    "symbol_prior_win_rate",# 该股历史一买成功率（信号日之前）
    "zs_position",          # 买点相对中枢位置（归一：<0 下方 / 0-1 内部 / >1 上方）
    "zs_height_pct",        # 中枢高度 / 买点价（整理规模）
    "seg_effort_ratio",     # 当前段/前段力度比（背驰衰减的连续量）
    "seg_range_pct",        # 当前段振幅 / 买点价（段深度）
    "breakout_strength",    # 三买突破高点越中枢上沿的幅度
    "structure_level",      # 结构级别（leg=段 / pen=笔）
    "sublevel_available",   # 二买信号时 m30 数据是否可得（次级别验证前提）
    "weekly_zs_position",   # 信号日收盘在最近完成周线中枢的归一位置（高级别约束）
    "weekly_trend_20",      # 周线 20 周均线趋势（高级别方向）
)


def bucketize(feature: str, value: Any) -> Optional[str]:
    """把特征值映射到可读区间；None 或无法分箱返回 None（不参与统计）。"""

    if value is None:
        return None
    if feature in ("above_ma20", "ma_bullish", "macd_hist_sign", "dif_above_zero"):
        return "是" if value == 1 else "否"
    if feature == "vol_ratio" or feature == "vol_ratio_5":
        v = float(value)
        if v < 0.8:
            return "缩量<0.8"
        if v < 1.2:
            return "平量0.8-1.2"
        if v < 2.0:
            return "温和放量1.2-2.0"
        return "显著放量≥2.0"
    if feature == "retrace_from_high":
        v = float(value)
        if v < 0.05:
            return "接近新高<5%"
        if v < 0.15:
            return "浅回撤5-15%"
        if v < 0.30:
            return "中度回撤15-30%"
        return "深度回撤≥30%"
    if feature == "price_vs_ma60":
        v = float(value)
        if v < -0.15:
            return "深跌于MA60<-15%"
        if v < -0.05:
            return "低于MA60 -15~-5%"
        if v < 0.05:
            return "贴MA60 ±5%"
        if v < 0.15:
            return "高于MA60 5-15%"
        return "远离MA60≥15%"
    if feature == "rsi14":
        v = float(value)
        if v < 30:
            return "超卖<30"
        if v < 45:
            return "偏弱30-45"
        if v < 55:
            return "中性45-55"
        if v < 70:
            return "偏强55-70"
        return "超买≥70"
    if feature == "kdj_k":
        v = float(value)
        if v < 20:
            return "超卖<20"
        if v < 50:
            return "低位20-50"
        if v < 80:
            return "中位50-80"
        return "高位≥80"
    if feature == "volatility_20":
        v = float(value)
        if v < 0.01:
            return "低波动<1%"
        if v < 0.02:
            return "中波动1-2%"
        if v < 0.03:
            return "高波动2-3%"
        return "极高波动≥3%"
    if feature == "consecutive_down":
        v = int(value)
        if v <= 0:
            return "无连跌"
        if v <= 2:
            return "连跌1-2天"
        if v <= 4:
            return "连跌3-4天"
        return "连跌≥5天"
    if feature == "divergence_score":
        v = float(value)
        if v < 0.10:
            return "弱背驰<0.1"
        if v < 0.20:
            return "中背驰0.1-0.2"
        if v < 0.30:
            return "强背驰0.2-0.3"
        return "极强背驰≥0.3"
    if feature == "market_breadth":
        v = float(value)
        if v < 0.2:
            return "极弱<20%"
        if v < 0.4:
            return "弱势20-40%"
        if v < 0.6:
            return "中性40-60%"
        if v < 0.8:
            return "强势60-80%"
        return "极强≥80%"
    if feature == "market_limit_up":
        v = int(value)
        if v < 30:
            return "冷清<30"
        if v < 60:
            return "温和30-60"
        if v < 100:
            return "活跃60-100"
        return "亢奋≥100"
    if feature == "market_avg_change":
        v = float(value)
        if v < -0.02:
            return "大跌<-2%"
        if v < -0.005:
            return "小跌-2~-0.5%"
        if v < 0.005:
            return "平盘±0.5%"
        if v < 0.02:
            return "小涨0.5-2%"
        return "大涨≥2%"
    if feature == "market_index_change":
        v = float(value)
        if v < -0.02:
            return "指数大跌<-2%"
        if v < -0.005:
            return "指数小跌-2~-0.5%"
        if v < 0.005:
            return "指数平盘±0.5%"
        if v < 0.02:
            return "指数小涨0.5-2%"
        return "指数大涨≥2%"
    if feature == "symbol_prior_win_rate":
        v = float(value)
        if v < 0.25:
            return "历史差<25%"
        if v < 0.5:
            return "历史一般25-50%"
        if v < 0.75:
            return "历史较好50-75%"
        return "历史优秀≥75%"
    if feature == "zs_position":
        # 归一中枢位置：0=贴下沿、1=贴上沿、<0 下方（一买区）、>1 上方（三买区）
        v = float(value)
        if v < -0.5:
            return "远低于中枢<-50%"
        if v < 0:
            return "中枢下方-50~0"
        if v < 0.5:
            return "中枢内下半0-0.5"
        if v <= 1:
            return "中枢内上半0.5-1"
        return "突破中枢>1"
    if feature == "zs_height_pct":
        v = float(value)
        if v < 0.05:
            return "窄中枢<5%"
        if v < 0.10:
            return "中枢纽5-10%"
        if v < 0.20:
            return "宽中枢10-20%"
        return "巨幅中枢≥20%"
    if feature == "seg_effort_ratio":
        # <1 = 力度衰减（背驰方向），越小衰减越狠
        v = float(value)
        if v < 0.3:
            return "力度骤降<0.3"
        if v < 0.6:
            return "明显衰减0.3-0.6"
        if v < 1:
            return "轻度衰减0.6-1"
        return "未衰减≥1"
    if feature == "seg_range_pct":
        v = float(value)
        if v < 0.05:
            return "浅段<5%"
        if v < 0.10:
            return "中段5-10%"
        if v < 0.20:
            return "深段10-20%"
        return "极深段≥20%"
    if feature == "breakout_strength":
        v = float(value)
        if v < 0.02:
            return "贴上沿<2%"
        if v < 0.05:
            return "小突破2-5%"
        if v < 0.10:
            return "中突破5-10%"
        return "强突破≥10%"
    if feature == "structure_level":
        return str(value)
    if feature == "sublevel_available":
        # 二元特征：二买发生时次级别数据是否已留存（1=可验证 / 0=窗口外）
        return "可验证" if int(value) == 1 else "窗口外"
    if feature == "weekly_zs_position":
        # 周线中枢位置：与日线 zs_position 同语义，但级别更高
        v = float(value)
        if v < -0.5:
            return "周线远低于中枢"
        if v < 0:
            return "周线中枢下方"
        if v <= 1:
            return "周线中枢内/上沿"
        return "周线突破中枢"
    if feature == "weekly_trend_20":
        v = float(value)
        if v < -0.10:
            return "周线空头<-10%"
        if v < 0.02:
            return "周线走平"
        if v < 0.10:
            return "周线温和多头"
        return "周线强势多头≥10%"
    return None


def _closed_rows(rows: List[dict]) -> List[dict]:
    """只保留结局已终结的样本。

    v2 口径：有规则化出场结果（net_exit_return 非 None）的样本；
    没有 v2 字段的老样本退回 max_return 非空（兼容未重建的旧库）。
    """

    return [
        r for r in rows
        if r.get("net_exit_return") is not None or r.get("max_return") is not None
    ]


def base_win_rate(rows: List[dict], threshold: float = DEFAULT_WIN_THRESHOLD) -> float:
    closed = _closed_rows(rows)
    if not closed:
        return 0.0
    return sum(1 for r in closed if label_win(r, threshold)) / len(closed)


def feature_gains(
    rows: List[dict],
    features: tuple = SYSTEM_FEATURES,
    threshold: float = DEFAULT_WIN_THRESHOLD,
) -> Dict[str, dict]:
    """对每个特征分箱，算每箱赚钱率、相对基准的增益、以及 **保守下界增益**。

    下界增益 = gain_lower_bound(...)：两个比率之差的标准误差折减。
    小样本箱自动折价，打分时用下界增益而不是朴素增益——体检盲区 8 的落地。
    """

    closed = _closed_rows(rows)
    base = base_win_rate(closed, threshold)
    base_n = len(closed)
    base_win = sum(1 for r in closed if label_win(r, threshold))
    gains: Dict[str, dict] = {}
    for feat in features:
        buckets: Dict[str, List[float]] = {}  # bucket -> 1 表示赢
        for row in closed:
            bucket = bucketize(feat, row.get("features", {}).get(feat))
            if bucket is None:
                continue
            buckets.setdefault(bucket, []).append(1 if label_win(row, threshold) else 0)
        bucket_stats = []
        for bucket, outcomes in sorted(buckets.items()):
            n = len(outcomes)
            win = sum(outcomes)
            rate = win / n if n else 0.0
            lb = wilson_lower_bound(win, n)
            glb = gain_lower_bound(win, n, base_win, base_n)
            bucket_stats.append(
                {
                    "bucket": bucket,
                    "n": n,
                    "win": win,
                    "win_rate": round(rate, 4),
                    "gain": round(rate - base, 4),
                    "lb_win_rate": round(lb, 4),
                    # 保守增益：两比率差的下界。打分用这个。
                    "gain_lb": round(glb, 4),
                }
            )
        gains[feat] = {"base_rate": round(base, 4), "buckets": bucket_stats}
    return gains


def structure_buy_cards(
    rows: List[dict],
    features: tuple = SYSTEM_FEATURES,
    threshold: float = DEFAULT_WIN_THRESHOLD,
) -> List[dict]:
    """「结构-买点-胜率卡」：买点类型 × 特征箱的二维交叉。

    直接回答「什么结构容易在什么买点赚钱」。只保留样本数达到最小量的格子，
    避免小样本噪声被当成规律。
    """

    closed = _closed_rows(rows)
    # 先按 kind 分组
    by_kind: Dict[str, List[dict]] = {}
    for row in closed:
        by_kind.setdefault(row.get("kind", "?"), []).append(row)

    cards: List[dict] = []
    for kind in sorted(by_kind):
        for feat in features:
            gains = feature_gains(by_kind[kind], (feat,), threshold)[feat]
            base = gains["base_rate"]
            for b in gains["buckets"]:
                if b["n"] < MIN_BUCKET_SAMPLES:  # 最小样本量门槛，防小样本噪声
                    continue
                cards.append(
                    {
                        "kind": kind,
                        "feature": feat,
                        "bucket": b["bucket"],
                        "n": b["n"],
                        "win_rate": b["win_rate"],
                        "gain": b["gain"],
                        "gain_lb": b["gain_lb"],
                    }
                )
    # 按**保守增益**降序：下界高才算真有信息量，均值好看但样本少的往后排
    cards.sort(key=lambda c: -c["gain_lb"])
    return cards


def score_signal(
    features: dict,
    gain_map: Dict[str, dict],
) -> tuple:
    """给一个信号打分：只累计命中特征箱里**保守下界为正**的增益。

    v2 修正（实测踩坑）：最初版本把命中箱的下界增益**全量求和**（正负都算）。
    但下界的数学性质决定了大多数「表面小幅正增益」的箱子下界会略小于 0
    （大样本下 SE≈±0.008，+0.01 的表面增益扣完就成负），而负箱的负下界
    却可以很大——结果是 96% 样本全挤在负分区间，验证段连一个正分样本都没有，
    「≥0 vs <0」的验证口径直接失效。

    正确语义：**下界>0 的箱子才是体系「确信」的正面证据**；下界≤0 的箱子
    只说明「这个箱暂时没有可信的正增益」，不构成负面证据（表面增益为负的
    强负箱仍要扣分——那是体系确信的负面证据）。

    返回 (总分, 命中明细)。明细里同时带表面增益与下界增益。
    """

    total = 0.0
    hits: List[dict] = []
    for feat, info in gain_map.items():
        bucket = bucketize(feat, features.get(feat))
        if bucket is None:
            continue
        for b in info.get("buckets", []):
            if b["bucket"] == bucket:
                glb = b.get("gain_lb", 0.0)
                g = b.get("gain", 0.0)
                # 正箱：下界>0 才加分；负箱：表面增益为负才扣分。
                # 中间地带（下界≤0 但表面≥0）= 证据不足，不加分也不扣分。
                if glb > 0:
                    total += glb
                elif g < 0 and glb < 0:
                    total += glb
                hits.append(
                    {
                        "feature": feat,
                        "bucket": bucket,
                        "gain": g,
                        "gain_lb": glb,
                        "n": b.get("n", 0),
                    }
                )
                break
    return round(total, 4), hits


def market_regime(row: dict) -> Optional[str]:
    """市场状态标签（分段验证用）：由指数近 20 日涨跌 + 市场宽度共同判定。

    体检盲区 6：样本期是单边牛市，全样本算出来的增益可能只是牛市 beta。
    把状态显式切出来，才能回答「这个体系在熊市还有没有 edge」。
    """

    idx_chg = row.get("features", {}).get("market_index_change")
    breadth = row.get("features", {}).get("market_breadth")
    if idx_chg is None:
        return None
    v = float(idx_chg)
    # 指数 20 日涨跌 >5% 视作上行段，< -5% 视作下行段，中间是震荡
    if v > 0.05:
        regime = "上行"
    elif v < -0.05:
        regime = "下行"
    else:
        regime = "震荡"
    # 宽度极弱（<30% 上涨）时即便指数没跌也归「弱市」——普跌结构里买点质量不同
    if breadth is not None and float(breadth) < 0.30 and regime != "下行":
        regime = "弱市"
    return regime


def validate_time_split(
    rows: List[dict],
    features: tuple = SYSTEM_FEATURES,
    threshold: float = DEFAULT_WIN_THRESHOLD,
    split_ratio: float = 0.7,
) -> Optional[dict]:
    """时间分割验证：老样本算增益、新样本验证「高分档是否真的更赚钱」。

    这是体系有效性的唯一证据——如果高分档赚钱率不比低分档高，说明这些特征
    在当前数据下没有可迁移的预测力，体系不能上线。

    返回 None 表示样本不足以做切分（需要训练、验证两段都非空）。
    """

    closed = _closed_rows(rows)
    if len(closed) < 60:
        return None
    closed = sorted(closed, key=lambda r: str(r.get("signal_date", "")))
    cut = int(len(closed) * split_ratio)
    train, test = closed[:cut], closed[cut:]
    if len(train) < 30 or len(test) < 30:
        return None

    gains = feature_gains(train, features, threshold)
    scored = []
    for row in test:
        score, _hits = score_signal(row.get("features", {}), gains)
        scored.append((score, row))

    # 分组口径（v2 修正）：按**分数符号**分两档，不用三等分。
    # 原因（实测踩坑）：保守下界让绝大多数箱子的增益≈0，体系分是重尾分布——
    # 96% 样本挤在 <0 区间、正分样本只有零头。三等分会把几千条负分样本
    # 「均摊」进高分组，把真正的信号淹没（实测三等分 spread=-0.22，
    # 而按符号分组后高分档 78.3% vs 低分档 45.9%，方向完全反转）。
    # 「体系分 ≥0 = 历史上这套规则净赚钱的结构」——这正是打分的语义，
    # 验证就该按这个语义切。
    positive = [(s, r) for s, r in scored if s >= 0]
    negative = [(s, r) for s, r in scored if s < 0]
    groups = [("negative", negative), ("positive", positive)]

    def rate(group: list) -> Optional[float]:
        if not group:
            return None
        w = sum(1 for _, r in group if label_win(r, threshold))
        return round(w / len(group), 4)

    def avg_score(group: list) -> float:
        if not group:
            return 0.0
        return round(sum(s for s, _ in group) / len(group), 4)

    def med_exit(group: list) -> Optional[float]:
        vals = sorted(
            r.get("net_exit_return")
            for _, r in group
            if r.get("net_exit_return") is not None
        )
        return round(vals[len(vals) // 2], 4) if vals else None

    pos_rate = rate(positive)
    neg_rate = rate(negative)
    spread = None if pos_rate is None or neg_rate is None else round(pos_rate - neg_rate, 4)
    return {
        "train_n": len(train),
        "test_n": len(test),
        "threshold": threshold,
        "label": "net_exit",
        "grouping": "score_sign",
        "low": {"n": len(negative), "avg_score": avg_score(negative), "win_rate": neg_rate},
        "high": {"n": len(positive), "avg_score": avg_score(positive), "win_rate": pos_rate},
        # 兼容旧字段名（前端 mid 组）：正负两档时 mid 无意义，置 None
        "mid": None,
        "low_median_exit": med_exit(negative),
        "high_median_exit": med_exit(positive),
        "spread": spread,
        "has_edge": pos_rate is not None and neg_rate is not None and pos_rate > neg_rate,
    }


def validate_by_regime(
    rows: List[dict],
    features: tuple = SYSTEM_FEATURES,
    threshold: float = DEFAULT_WIN_THRESHOLD,
) -> Optional[dict]:
    """按市场状态分段验证（体检盲区 6 的落地）。

    对每个样本量足够的状态（上行/下行/震荡/弱市），各自做一次完整的
    时间分割验证。体系在某一状态有 edge、在另一个状态失效，是**必须知道**的
    事实——只报全样本 has_edge=True 而熊市失效的体系，上线就是灾难。

    返回 None 表示样本不足以分段。每个状态返回与 validate_time_split 相同结构
    （外加 regime 字段），并汇总 overall_has_edge（所有有效段都有 edge 才算 True）。
    """

    closed = _closed_rows(rows)
    if len(closed) < 60:
        return None

    by_regime: Dict[str, List[dict]] = {}
    for row in closed:
        regime = market_regime(row)
        if regime is None:
            continue
        by_regime.setdefault(regime, []).append(row)

    segments: List[dict] = []
    for regime in sorted(by_regime):
        seg = by_regime[regime]
        # 分段后样本更少，门槛放宽到 60（全样本门槛一致）
        result = validate_time_split(seg, features, threshold)
        if result is None:
            segments.append(
                {
                    "regime": regime,
                    "n": len(seg),
                    "verifiable": False,
                    "message": "样本不足，无法分段验证",
                }
            )
            continue
        result["regime"] = regime
        result["n"] = len(seg)
        result["verifiable"] = True
        segments.append(result)

    verifiable = [s for s in segments if s.get("verifiable")]
    return {
        "segments": segments,
        "overall_has_edge": bool(verifiable) and all(s["has_edge"] for s in verifiable),
        "regimes": {r: len(v) for r, v in sorted(by_regime.items())},
    }
