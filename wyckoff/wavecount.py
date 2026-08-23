"""自动波浪计数 (艾略特波浪理论): 基于枢轴序列识别推动浪/修正浪结构。

与旧 elliott_wave 的单波段斐波那契不同, 本模块:
  - 在枢轴序列上滑动窗口, 自动识别完整 5浪推动 (impulse) 或 3浪修正 (ABC);
  - 输出当前浪位 (第几浪 / 推动 vs 修正), 供结论区与图表标注;
  - 跨浪位计算斐波那契回撤/扩展汇聚带;
  - 结合威科夫阶段/事件做交叉验证 (波浪位置 × 派发/吸筹语境)。

规则要点 (不追求完美艾略特, 只取有统计意义的近似):
  - 推动浪: 浪2/浪4 不重叠浪1顶 (下跌推动反向), 浪3非最短, 浪4回撤有限;
  - 修正浪: 三浪 ABC, 通常回撤推动浪的 0.382~0.618;
  - 计数自右向左: 从最新枢轴向左回溯最长的合法 5浪/3浪, 剩余结构标为
    "未完成/新起点", 与威科夫"阶段待确认"的保守口径一致。
"""
from dataclasses import dataclass, field


# ── 数据结构 ──
@dataclass
class WavePoint:
    """波浪结构中的关键点。"""
    idx: int
    price: float
    wave: str          # "1".."5", "A","B","C", 或 "" (未归类)
    kind: str          # "impulse" 或 "corrective"
    direction: str     # "up" / "down"


@dataclass
class WaveCount:
    """一次波浪计数结果。"""
    kind: str                       # "impulse" / "corrective" / "none"
    direction: str                  # 推动/修正的总体方向
    points: list[WavePoint]         # 已识别浪位点 (按时间序)
    waves: list[dict]               # [{wave,start_idx,end_idx,start,end,direction,label}]
    position: str                   # "浪3中" / "浪4回调" / "ABC回调" 等描述
    position_wave: str              # 当前处于的浪号 "1".."5","A","B","C",""
    done: bool                      # 当前浪位是否结构完成
    fib_confluence: list[dict]      # [{level, price, kind("回撤"/"扩展")}]
    next_target: float | None    # 下一个扩展目标
    invalidation: float | None   # 结构失效价
    quality: float                  # 0~1 结构质量分
    detail: list[str] = field(default_factory=list)


# ── 工具 ──
def _seq(seq, i):
    """seq[i] → (type, price, idx); 越界返回 None。"""
    if 0 <= i < len(seq):
        return seq[i]
    return None


def _alt(seq, i):
    """seq[i] 是否为类型 t (type in "high"/"low")。"""
    s = _seq(seq, i)
    return s if s else None


def _is_impulse(pts):
    """pts: [(type,price,idx)] 长度5, 交替 low-high-low-high-low (上升) 或反之。
    校验: 浪2不破浪1起点, 浪4不破浪1顶, 浪3非最短。"""
    if len(pts) != 5:
        return False
    t0, p0, i0 = pts[0]
    t1, p1, i1 = pts[1]
    t2, p2, i2 = pts[2]
    t3, p3, i3 = pts[3]
    t4, p4, i4 = pts[4]
    if t0 == "low" and t1 == "high" and t2 == "low" and t3 == "high" and t4 == "low":
        up = True
    elif t0 == "high" and t1 == "low" and t2 == "high" and t3 == "low" and t4 == "high":
        up = False
    else:
        return False
    if up:
        if not (p0 < p2 < p4):
            return False
        if not (p1 < p3):
            return False
        if p2 < p0 or p4 < p2:
            return False
        # 浪3 (w2 = 浪2低→浪3高) 不得短于浪1和浪5
        w1, w2, w3 = p1 - p0, p3 - p1, p4 - p3
        if w2 < w1 * 0.9 or w2 < w3 * 0.9:
            return False
    else:
        if not (p0 > p2 > p4):
            return False
        if not (p1 > p3):
            return False
        if p2 > p0 or p4 > p2:
            return False
        w1, w2, w3 = p0 - p1, p1 - p3, p3 - p4
        if w2 < w1 * 0.9 or w2 < w3 * 0.9:
            return False
    return True


def _is_abc(pts):
    """pts: [(type,price,idx)] 长度4: high-low-high-low (下跌修正) 或反之 (上升修正)。
    ABC 修正: 三浪, B 浪不得过度超出 A 起点 (否则趋势未反转, 属更高一级推动)。"""
    if len(pts) != 4:
        return False
    t0, p0, _ = pts[0]
    t1, p1, _ = pts[1]
    t2, p2, _ = pts[2]
    t3, p3, _ = pts[3]
    if t0 == "high" and t1 == "low" and t2 == "high" and t3 == "low":
        # 下跌修正: A=高→低, B=低→高, C=高→低
        # B 浪反弹不得超过 A 起点 (一旦超过, 是更高一级的上升推动)
        if not (p1 < p0 and p3 < p2):
            return False
        return p2 < p0 * 1.02
    if t0 == "low" and t1 == "high" and t2 == "low" and t3 == "high":
        # 上升修正: A=低→高, B=高→低, C=低→高
        if not (p1 > p0 and p3 > p2):
            return False
        return p2 > p0 * 0.98
    return False


def _wave_label_seq(pts, start_wave="1"):
    """为 impulse 5 点分配浪号 1-5; corrective 4 点分配 A,B,C。"""
    labels = []
    if len(pts) == 5:
        labels = ["1", "2", "3", "4", "5"]
    elif len(pts) == 4:
        labels = ["A", "B", "C", ""]
    return labels


def count_waves(pivots, max_waves: int = 5):
    """主入口: 从枢轴序列识别最近一组完整波浪结构。

    返回 WaveCount; 找不到合法结构时 kind="none"。"""
    seq = [(p["type"], p["price"], p["idx"]) for p in sorted(
        pivots, key=lambda p: p["idx"])]
    if len(seq) < 4:
        return WaveCount("none", "", [], [], "结构不足以计数", "", False, [], None, None, 0.0)

    # 从最新枢轴向左回溯: 尝试 (5 点推动浪) 或 (4 点 ABC 修正)。
    # 推动浪(5浪)层级高于修正浪(ABC), 同一区间两者都成立时优先推动浪。
    best_impulse = None
    best_abc = None
    n = len(seq)
    for end in range(n - 1, max(2, n - 3) - 1, -1):
        # 5浪: seq[end-4..end] 交替
        if end - 4 >= 0:
            pts = seq[end - 4:end + 1]
            if _is_impulse(pts):
                best_impulse = _build_impulse(pts, seq, end)
                if best_impulse is not None:
                    break
        # ABC: seq[end-3..end] 交替
        if end - 3 >= 0:
            pts = seq[end - 3:end + 1]
            if _is_abc(pts) and best_abc is None:
                best_abc = _build_abc(pts, seq, end)
    if best_impulse is not None:
        return best_impulse
    if best_abc is not None:
        return best_abc
    return WaveCount("none", "", [], [], "未识别到完整波浪结构", "", False, [], None, None, 0.0)


def _build_impulse(pts, seq, end):
    """pts[0..4] 是合法推动浪。扩展为 WaveCount。"""
    direction = "up" if pts[0][0] == "low" else "down"
    waves = []
    for k in range(4):
        waves.append({
            "wave": str(k + 1),
            "start_idx": pts[k][2], "end_idx": pts[k + 1][2],
            "start": pts[k][1], "end": pts[k + 1][1],
            "direction": direction,
            "label": f"{k+1}浪",
        })
    # 浪5结束点 = pts[4] (最后一浪终点)
    w5_start, w5_end = pts[3][1], pts[4][1]
    # 当前浪位: 若最新枢轴就是 pts[4], 推动浪完成 → 待回调; 否则可能已进入回调
    last_idx = seq[-1][2] if seq else -1
    done = pts[4][2] >= last_idx - 2
    if direction == "up":
        pos = "5浪上升完成, 关注A-B-C回调" if done else "上升推动结构, 浪5临近"
        position_wave = "5" if done else "5"
        invalidation = pts[0][1]
    else:
        pos = "5浪下跌完成, 关注A-B-C反弹" if done else "下跌推动结构, 浪5临近"
        position_wave = "5" if done else "5"
        invalidation = pts[0][1]
    # 斐波那契: 整段推动 0->浪5顶
    w0, w5 = pts[0][1], pts[4][1]
    swing = w5 - w0
    confluence = []
    for level, ratio in ((0.382, 0.382), (0.5, 0.5), (0.618, 0.618)):
        confluence.append({"level": f"{level:.3f}", "price": round(w5 - swing * ratio, 2),
                           "kind": "回撤" if direction == "up" else "回撤"})
    for level, ratio in ((1.272, 1.272), (1.618, 1.618)):
        confluence.append({"level": f"{level:.3f}", "price": round(w5 + swing * ratio, 2),
                           "kind": "扩展" if direction == "up" else "扩展"})
    points = [WavePoint(p[2], p[1], str(k + 1), "impulse", direction)
              for k, p in enumerate(pts)]
    quality = 0.75
    return WaveCount("impulse", direction, points, waves, pos, position_wave, done,
                     confluence, confluence[-1]["price"] if confluence else None,
                     invalidation, quality)


def _build_abc(pts, seq, end):
    """pts[0..3] 是合法 ABC 修正。扩展为 WaveCount。"""
    direction = "up" if pts[0][0] == "low" else "down"
    waves = []
    for k in range(3):
        waves.append({
            "wave": ["A", "B", "C"][k],
            "start_idx": pts[k][2], "end_idx": pts[k + 1][2],
            "start": pts[k][1], "end": pts[k + 1][1],
            "direction": direction,
            "label": f"{['A','B','C'][k]}浪",
        })
    done = pts[3][2] >= (seq[-1][2] if seq else -1) - 2
    if direction == "up":
        # 向上修正 (下跌后反弹 low-high-low-high): C末端是反弹高点
        pos = "A-B-C 反弹完成" if done else "A-B-C 反弹中"
        position_wave = "C" if done else "C"
        invalidation = pts[0][1]
    else:
        # 向下修正 (上涨后回调 high-low-high-low): C末端是回调低点
        pos = "A-B-C 回调完成" if done else "A-B-C 回调中"
        position_wave = "C" if done else "C"
        invalidation = pts[0][1]
    # 斐波那契: 整段修正 (回撤位方向 = 修正方向; 上升修正→回撤支撑, 下降修正→反弹阻力)
    w0, w3 = pts[0][1], pts[3][1]
    swing = w3 - w0
    confluence = []
    for level, ratio in ((0.382, 0.382), (0.5, 0.5), (0.618, 0.618)):
        kind = "阻力" if direction == "down" else "支撑"
        confluence.append({"level": f"{level:.3f}", "price": round(w0 + swing * ratio, 2),
                           "kind": kind})
    # 修正浪无标准扩展目标; C浪参考位 = A浪终点 ± A浪幅度 (等长目标)
    a_len = abs(pts[1][1] - pts[0][1])
    c_tgt = pts[1][1] - a_len if direction == "up" else pts[1][1] + a_len
    points = [WavePoint(p[2], p[1], ["A", "B", "C", ""][k], "corrective", direction)
              for k, p in enumerate(pts)]
    return WaveCount("corrective", direction, points, waves, pos, position_wave, done,
                     confluence, round(c_tgt, 2),
                     invalidation, 0.6)


def wave_points_for_chart(pivots, max_points: int = 12):
    """提取最近一段波浪结构的 (idx, price, wave_label) 序列, 供图表折线标注。"""
    wc = count_waves(pivots)
    if wc.kind == "none" or not wc.points:
        return []
    return [(p.idx, p.price, p.wave) for p in wc.points]
