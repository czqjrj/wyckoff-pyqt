"""阈值注册表 — 集中登记全量调查得出的分桶/权重阈值 (A3)。

动机: SC/BC 环境门、SOW 放量门、高位布林过滤、fusion 弱事件半权等阈值,
都是全量调查(一次调研)后写死在各模块里的常量。分布漂移后不重算就永远生效,
且散落各处无法审计。

每条登记 (CalibEntry) 都带:
  - as_of:   调查日期 (ISO "YYYY-MM-DD")
  - buckets: 分桶 (数值区间桶 [lo, hi) 或类型桶), 每个携带调查样本量 n
  - default: 未命中 / 样本不足/过期时消费方回退的默认值 (即"无调查结论"时的行为)
  - source:  调查存档出处, 供审计

消费方统一走 bucket_value()/resolve() 读取, 自动跳过"样本不足或过期"的桶,
回退 default —— 避免一次性固化阈值在分布漂移后继续生效。

值语义由 value_kind 说明, 由消费方按其业务解释返回值:
  - "add":   conf 加分/减分 (加法调整, default 一般 0)
  - "gate":  门控 (value=0 关断, 1 放行, default 放行)
  - "set":   覆盖原值 (value 为取代值, default 为原行为值)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

_INF = float("inf")


@dataclass(frozen=True)
class Bucket:
    """单个分桶。数值桶用 [lo, hi) 半开区间 (lo/hi None 表示 -/+inf);

    类型桶 (lo/hi 均 None) 用 label 匹配 key。
    """

    label: str
    value: float
    n: int | None  # 调查样本量; None=无记录(不参与样本量检查)
    lo: float | None = None
    hi: float | None = None
    hit_rate: float | None = None  # 0..1 调查命中率, 仅作文档审计用

    def contains(self, key: object) -> bool:
        if self.lo is None and self.hi is None:
            return key == self.label
        if not isinstance(key, (int, float, bool)):
            return False
        x = float(key)
        lo = self.lo if self.lo is not None else -_INF
        hi = self.hi if self.hi is not None else _INF
        return lo <= x < hi


@dataclass(frozen=True)
class CalibEntry:
    key: str
    as_of: str  # ISO "YYYY-MM-DD"
    feature: str  # 观测值文档名 (审计用)
    buckets: tuple[Bucket, ...]
    default: float
    hit_rate: float | None = None  # 该维度整体平均命中率, 审计用
    source: str = ""
    value_kind: str = "add"  # add | gate | set
    min_n: int | None = None  # 分桶最低生效样本; None=不检查
    max_age_days: int = 365


def _as_date(today: date | None) -> date:
    return today if today is not None else date.today()


def is_expired(entry: CalibEntry, today: date | None = None) -> bool:
    try:
        as_of = date.fromisoformat(entry.as_of)
    except (TypeError, ValueError):
        return True
    return (_as_date(today) - as_of).days > entry.max_age_days


def resolve(entry: CalibEntry, key: object, today: date | None = None) -> Bucket | None:
    """命中且新鲜(样本足、未过期)的桶返回之; 否则返回 None, 消费方回退 default。"""
    if is_expired(entry, today):
        return None
    for b in entry.buckets:
        if not b.contains(key):
            continue
        if entry.min_n is not None and (b.n is None or b.n < entry.min_n):
            return None  # 样本不足 → 视为无调查结论
        return b
    return None


def bucket_value(entry: CalibEntry, key: object, today: date | None = None) -> float:
    """生效桶的 value; 未命中/样本不足/过期一律回退 entry.default。"""
    b = resolve(entry, key, today)
    return b.value if b else entry.default


# ---- 注册表 ----
# 来源存档:
#   docs/event_env_gate_results.txt  (SC/BC 环境门, 2026-09-15, 5345 只)
#   docs/sow_tighten_results.txt     (SOW 放量门,   2026-09-15, 5345 只)
REGISTRY: dict[str, CalibEntry] = {
    "sc_env_gate": CalibEntry(
        key="sc_env_gate",
        as_of="2026-09-15",
        feature="prior_r20",
        value_kind="add",
        min_n=800,
        source="docs/event_env_gate_results.txt",
        buckets=(
            Bucket("前置深跌(≤-15%)", value=8, lo=None, hi=-0.15,
                   n=1025, hit_rate=0.704),
            Bucket("前置中跌(-15~-8%)", value=0, lo=-0.15, hi=-0.08,
                   n=1109, hit_rate=0.593),
            Bucket("前置横盘/上涨(>-8%)", value=-15, lo=-0.08, hi=None,
                   n=1358, hit_rate=0.518),
        ),
        default=0.0,
    ),
    "bc_env_gate": CalibEntry(
        key="bc_env_gate",
        as_of="2026-09-15",
        feature="prior_r20",
        value_kind="add",
        min_n=800,
        source="docs/event_env_gate_results.txt",
        buckets=(
            Bucket("前置横盘(≤+4%)", value=-12, lo=None, hi=0.04,
                   n=4677, hit_rate=0.478),
            Bucket("前置中涨(+4~+15%)", value=0, lo=0.04, hi=0.15,
                   n=5124, hit_rate=0.542),
            Bucket("前置深涨(≥+15%)", value=8, lo=0.15, hi=None,
                   n=5925, hit_rate=0.605),
        ),
        default=0.0,
    ),
    "bc_uptrend": CalibEntry(
        key="bc_uptrend",
        as_of="2026-09-15",
        feature="up_i",
        value_kind="add",
        min_n=800,
        source="docs/event_env_gate_results.txt",
        buckets=(
            Bucket("上升趋势内", value=-5, lo=1, hi=None,
                   n=9517, hit_rate=0.530),
        ),
        default=0.0,
    ),
    "sow_vol_gate": CalibEntry(
        key="sow_vol_gate",
        as_of="2026-09-15",
        feature="vr (vol/vol_ma20)",
        value_kind="add",
        min_n=20,
        source="docs/sow_tighten_results.txt",
        buckets=(
            Bucket("平凡放量(<1.6)", value=-8, lo=None, hi=1.6,
                   n=230, hit_rate=0.665),
            Bucket("中量(1.6~2.2)", value=2, lo=1.6, hi=2.2,
                   n=56, hit_rate=0.714),
            Bucket("深层放量(≥2.2)", value=8, lo=2.2, hi=None,
                   n=20, hit_rate=0.850),
        ),
        default=0.0,
    ),
    "sos_joc_boll_cap": CalibEntry(
        key="sos_joc_boll_cap",
        as_of="2026-09-15",
        feature="boll_pct",
        value_kind="gate",
        source="wyckoff/events.py detect_joc_lps_bu (高位突破易失败注释)",
        buckets=(
            Bucket("低位/中位(≤0.8)", value=1, lo=None, hi=0.8,
                   n=None, hit_rate=0.50),
            Bucket("高位(>0.8)", value=0, lo=0.8, hi=None,
                   n=None, hit_rate=0.45),
        ),
        default=1.0,
    ),
    "weak_event_half": CalibEntry(
        key="weak_event_half",
        as_of="2026-09-15",
        feature="event type",
        value_kind="set",
        source="wyckoff/fusion.py (37股回测 SOS 48.8% / JOC 44.2% vs 基准 47.6%)",
        buckets=(
            Bucket("SOS", value=0.5, n=37, hit_rate=0.488),
            Bucket("JOC", value=0.5, n=37, hit_rate=0.442),
        ),
        default=1.0,
    ),
}


def get(key: str) -> CalibEntry:
    try:
        return REGISTRY[key]
    except KeyError:
        raise KeyError(f"calib_registry: 未知阈值键 {key!r}") from None
