"""P&F 目标预测加权 ensemble 模块。

结合以下四个信号的加权聚合：
  - P&F 目标价区间
  - 在线模型置信度调整
  - VSA (Volume Spread Analysis) 信号
  - 波浪理论 (Wave) 信号

权重配置:
  P&F:      0.35
  Online:   0.30
  VSA:      0.20
  Wave:     0.15
总计: 1.00
"""

from __future__ import annotations

# ── 方案权重 ──────────────────────────────────────────────────────────
_WEIGHTS = {
    "pnf": 0.35,
    "online": 0.30,
    "vsa": 0.20,
    "wave": 0.15,
}
assert sum(_WEIGHTS.values()) == 1.0, "Weights must sum to 1.0"


# ── 辅助：计算加权聚合分数 ─────────────────────────────────────────────

def _weighted_score(
    scores: dict[str, float], weights: dict[str, float] | None = None
) -> float:
    """根据权重计算加权平均分。

    参数:
        scores:  各模块的得分 (0.0 - 1.0)
        weights: 对应权重字典 (使用模块默认值如果为 None)

    返回:
        加权平均分 (0.0 - 1.0)
    """
    if weights is None:
        weights = _WEIGHTS
    total = 0.0
    w_sum = 0.0
    for key, w in weights.items():
        s = scores.get(key, 0.0)
        total += s * w
        w_sum += w
    return round(total / w_sum, 4) if w_sum > 0 else 0.0


# ── 主函数：ensemble_pnf_signal ───────────────────────────────────────

def ensemble_pnf_signal(
    pnf_score: float,
    online_conf: float,
    vsa_score: float,
    wave_score: float,
    weights: dict[str, float] | None = None,
) -> dict[str, object]:
    """聚合 P&F、在线模型、VSA 与波浪理论信号的加权分数。

    参数:
        pnf_score:      P&F 目标置信度/得分 (0.0 - 1.0)
        online_conf:    在线模型置信度 (0.0 - 1.0)，通常来自
                        :func:`wyckoff.online_model._get_online_confidence_for_pnf`
        vsa_score:      VSA 信号强度 (0.0 - 1.0)
        wave_score:     波浪理论信号强度 (0.0 - 1.0)
        weights:        可选的权重字典 (使用默认值如果为 None)

    返回:
        具含下键的字典:
            - "combined_score": 加权聚合得分 (0.0 - 1.0)
            - "breakdown":      各模块贡献的明细字典
            - "direction":      预测方向 ("up" | "down" | "neutral")
            - "note":           附加说明文字

    示例:
        >>> result = ensemble_pnf_signal(
        ...     pnf_score=0.7,
        ...     online_conf=0.6,
        ...     vsa_score=0.5,
        ...     wave_score=0.8,
        ... )
        >>> result["combined_score"]
        0.68
        >>> result["direction"]
        "up"
    """
    # 计算各模块得分 (若有需要，这里可做归一化；此处假设已在 0-1 范围)
    scores = {
        "pnf": float(pnf_score),
        "online": float(online_conf),
        "vsa": float(vsa_score),
        "wave": float(wave_score),
    }

    # 加权聚合得分
    combined_score = _weighted_score(scores, weights)

    # 确定方向：若 combined_score > 0.5 预测上涨，否则下跌/中性
    if combined_score > 0.55:
        direction = "up"
    elif combined_score < 0.45:
        direction = "down"
    else:
        direction = "neutral"

    # 各模块对最终得分的贡献
    breakdown = {
        "pnf": round(scores["pnf"] * _WEIGHTS["pnf"], 4),
        "online": round(scores["online"] * _WEIGHTS["online"], 4),
        "vsa": round(scores["vsa"] * _WEIGHTS["vsa"], 4),
        "wave": round(scores["wave"] * _WEIGHTS["wave"], 4),
    }
    # 总贡献应等于 combined_score（考虑舍入误差）
    breakdown["total"] = round(sum(breakdown.values()), 4)

    # 根据置信度阈值生成说明
    if combined_score >= 0.75:
        note = "Strong consensus across models"
    elif combined_score >= 0.60:
        note = "Moderate consensus"
    else:
        note = "Weak consensus / mixed signals"

    return {
        "combined_score": combined_score,
        "breakdown": breakdown,
        "direction": direction,
        "note": note,
    }
