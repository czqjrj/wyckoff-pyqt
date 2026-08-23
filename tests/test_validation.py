"""准确性验证层测试 (wyckoff.validation):
1. Rank IC: 置信度与收益的 Spearman 秩相关 + 分档胜率曲线;
2. Bootstrap CI: 胜率 95% 置信区间 + 小样本标注;
3. 随机入场显著性: 置换检验区分"优于随机"与"与随机无异";
4. 样本外校准: win_rate_of_oos 只用 before_ts 之前样本 (无前瞻);
5. fusion OOS 参数: _winrate_weight 按 before_ts 走历史数据。
"""
import numpy as np
import pandas as pd

from wyckoff.fusion import _winrate_weight
from wyckoff.validation import (
    bootstrap_winrate_ci,
    rank_ic,
    significance_table,
    validation_ai_interpret,
    validation_lines,
    validation_verdict,
    win_rate_of_oos,
    winrate_ci_table,
)


def _rec(kind, type_, conf, ret, date="2024-06-01"):
    return {"kind": kind, "type": type_, "conf": conf,
            "date": date, "results": {"20": {"ret": ret}}}


def _mk_records(n=200, seed=1):
    """构造 n 条事件记录: 高置信→高收益 (IC>0), 便于断言方向。"""
    rng = np.random.default_rng(seed)
    recs = []
    for i in range(n):
        conf = 20 + (i % 80)
        ret = (conf - 50) / 500.0 + rng.normal(0, 0.02)
        recs.append(_rec("event", "Spring", conf, ret,
                         date=f"2024-{(i % 12) + 1:02d}-{15:02d}"))
    return recs


# ───────────────────────── 1. Rank IC ─────────────────────────

def test_rank_ic_positive_when_conf_predicts():
    """置信度越高收益越高 → Spearman IC > 0, 分档胜率单调。"""
    recs = _mk_records(200, seed=3)
    ic = rank_ic(recs, horizon=20)
    assert ic["n"] == 200
    assert ic["spearman"] is not None and ic["spearman"] > 0.1
    assert not ic["insufficient"]


def test_rank_ic_insufficient_marked():
    """样本不足 30 → insufficient=True, spearman 仍尽力给出。"""
    recs = [_rec("event", "Spring", c, c / 1000.0) for c in range(10, 30)]
    ic = rank_ic(recs, min_n=30)
    assert ic["insufficient"] is True
    assert ic["n"] == 20


def test_rank_ic_empty():
    """无记录 → 全空安全。"""
    ic = rank_ic([], kind="event")
    assert ic["n"] == 0 and ic["by_band"] == {}


def test_rank_ic_bands_exist():
    """高置信分档 (≥80) 胜率应高于低置信 (<40)。"""
    recs = _mk_records(300, seed=5)
    ic = rank_ic(recs, min_n=30)
    hi = ic["by_band"].get("≥80", {})
    lo = ic["by_band"].get("<40", {})
    assert hi and lo, f"分档缺失: {list(ic['by_band'])}"
    assert hi["win"] > lo["win"]


# ───────────────────────── 2. Bootstrap CI ─────────────────────────

def test_bootstrap_ci_bounds():
    """CI 覆盖观测胜率, 下界<=胜率<=上界。"""
    rng = np.random.default_rng(1)
    rets = list(rng.normal(0.01, 0.05, 100))
    ci = bootstrap_winrate_ci(rets, n_boot=200, seed=1)
    assert ci is not None
    assert ci["ci_lo"] <= ci["win"] <= ci["ci_hi"]
    assert ci["n"] == 100


def test_bootstrap_ci_small_sample():
    """样本 <3 → None; n<20 → insufficient 标注。"""
    assert bootstrap_winrate_ci([0.1, 0.2]) is None
    recs = [_rec("event", "TEST", 50, r) for r in (0.1, -0.1, 0.2)]
    t = winrate_ci_table(recs, min_n=3)
    assert "TEST" in t["types"]
    assert t["types"]["TEST"]["insufficient"] is True


# ───────────────────────── 3. 随机入场显著性 ─────────────────────────

def test_significance_detects_skill_signal():
    """强信号类型 (均值远超池内其他类型) → p 小, sig_5=True。"""
    rng = np.random.default_rng(2)
    recs = [_rec("event", "SOS", 50, rng.normal(0.0, 0.03)) for _ in range(120)]
    recs += [_rec("event", "JOC", 50, rng.normal(0.0, 0.03)) for _ in range(120)]
    recs += [_rec("event", "Spring", 50, rng.normal(0.06, 0.03)) for _ in range(30)]
    sig = significance_table(recs, min_n=8, n_perm=300, seed=2)
    assert sig is not None
    assert "Spring" in sig["types"]
    assert sig["types"]["Spring"]["sig_5"] is True
    assert sig["types"]["Spring"]["p"] < 0.05


def test_significance_no_pool():
    """全池不足 20 → None (无法检验)。"""
    recs = [_rec("event", "Spring", 50, 0.01) for _ in range(10)]
    assert significance_table(recs, min_n=3) is None


# ───────────────────────── 4. 样本外校准 OOS ─────────────────────────

def test_oos_only_uses_prior_samples():
    """win_rate_of_oos 只用 before_ts 之前的记录: 剔除之后的高胜率样本 → 胜率下降。"""
    recs = []
    for d in ("2024-01-05", "2024-01-10", "2024-01-15", "2024-02-01"):
        recs.append(_rec("event", "Spring", 50, 0.05, date=d))  # 全部上涨
    # before 1月20日: 应只含前三根 (前两交易日), 胜率 100%
    w = win_rate_of_oos(recs, "event", "Spring", pd.Timestamp("2024-01-20"),
                        min_n=1, baseline=0.5)
    assert abs(w - 1.0) < 1e-9
    # before 1月1日: 无样本 → baseline
    w2 = win_rate_of_oos(recs, "event", "Spring", pd.Timestamp("2023-12-31"),
                         min_n=1, baseline=0.5)
    assert abs(w2 - 0.5) < 1e-9


def test_oos_filters_kind_and_type():
    """跨 kind/type 不串样本。"""
    recs = [_rec("event", "SOS", 50, 0.1, date="2024-01-05"),
            _rec("vsa", "SPR", 0, 0.9, date="2024-01-05")]
    w = win_rate_of_oos(recs, "event", "SOS", pd.Timestamp("2024-06-01"),
                        min_n=1, baseline=0.5)
    assert abs(w - 1.0) < 1e-9
    w2 = win_rate_of_oos(recs, "event", "SOS", pd.Timestamp("2024-06-01"),
                         min_n=10, baseline=0.5)
    assert abs(w2 - 0.5) < 1e-9


def test_oos_uses_signal_dates_correctly():
    """记录 date 缺失时安全跳过 (不计入 OOS 样本), 样本不足 → baseline。"""
    recs = [_rec("event", "Spring", 50, 0.1)]
    w = win_rate_of_oos(recs, "event", "Spring", pd.Timestamp("2024-06-01"),
                        min_n=1, baseline=0.5)
    assert abs(w - 0.5) < 1e-9


# ───────────────────────── 5. fusion OOS 接入 ─────────────────────────

def test_winrate_weight_before_ts_path():
    """_winrate_weight 带 before_ts → 走 OOS (无历史样本→baseline→1.0)。"""
    w = _winrate_weight("event", "Spring", direction=1,
                        before_ts=pd.Timestamp("2000-01-01"))
    assert abs(w - 1.0) < 1e-9


def test_validation_lines_render():
    """validation_lines 可渲染, 不抛错。"""
    recs = _mk_records(100, seed=7)
    lines = validation_lines(recs, horizon=20)
    assert isinstance(lines, list) and lines
    assert any("置信度IC" in l for l in lines)
    assert any("随机入场基准" in l for l in lines)


# ───────────────────────── 6. 准确性规则化解读 ─────────────────────────

def test_verdict_empty_records():
    """无记录 → 提示文本, 不抛错。"""
    assert "暂无信号记录" in validation_verdict([])


def test_verdict_positive_ic():
    """IC 明显>0 且有显著优于随机的类型 → 解读说有区分度、可依赖。"""
    rng = np.random.default_rng(11)
    recs = [_rec("event", "SOS", 50, rng.normal(0.0, 0.03)) for _ in range(120)]
    recs += [_rec("event", "JOC", 50, rng.normal(0.0, 0.03)) for _ in range(120)]
    recs += [_rec("event", "Spring", 100 - (i % 80),
                  (80 - (i % 80)) / 400.0 + rng.normal(0, 0.02))
             for i in range(30)]
    v = validation_verdict(recs)
    assert "有区分度" in v
    assert "可依赖" in v


def test_verdict_inverted_scoring():
    """置信度与收益负相关 → 解读警示打分方向反了。"""
    rng = np.random.default_rng(3)
    recs = [_rec("event", "Spring", 100 - (i % 80), (i % 80) / 500.0 + rng.normal(0, 0.02))
            for i in range(200)]
    v = validation_verdict(recs)
    assert "反了" in v or "不预判" in v


def test_ai_interpret_offline_fallback():
    """无 settings → ai=None, rule 可用。"""
    recs = _mk_records(100, seed=5)
    r = validation_ai_interpret(recs, settings=None)
    assert r["ai"] is None
    assert r["rule"] and "有区分度" in r["rule"]


def test_ai_interpret_with_settings_no_key():
    """有 settings 但无 API Key → ai=None, 回退规则解读。"""
    recs = _mk_records(100, seed=6)
    r = validation_ai_interpret(recs, settings={"ai_api_key": ""})
    assert r["ai"] is None
    assert r["rule"]


def test_accuracy_ai_prompt_long_only_constraint():
    """准确性 AI 解读 prompt 必须声明 A股只能做多, 禁止做空指令。"""
    from wyckoff.validation import _ACCURACY_AI_PROMPT
    assert "只能做多、不能做空" in _ACCURACY_AI_PROMPT
    assert "减仓/离场/回避" in _ACCURACY_AI_PROMPT
    assert "做空" in _ACCURACY_AI_PROMPT
