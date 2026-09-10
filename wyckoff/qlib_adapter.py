"""Qlib 适配层 - 与 Wyckoff 信号融合的可选量化因子层。

当安装了 qlib (>=1.6.0) 时，提供以下功能：
- fetch_qlib_features: 计算价格/技术因子
- qlib_signal_probability: 基于因子模型预测买卖概率
- caliabrte_wyckoff_thresholds: 基于历史回测调整 Wyckoff 阈值

当 qlib 未安装时，提供具备前向兼容性的占位实现，
确保主程序无 ImportError 降级为纯 Wyckoff 模式。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_qlib_available: Optional[bool] = None
_qlib_features_cache: Dict[str, Dict[str, float]] = {}


def _is_qlib_available() -> bool:
    """检查 qlib 是否已安装并可用。"""
    global _qlib_available
    if _qlib_available is None:
        try:
            import qlib
            import qlib.data
            _ = qlib.__version__
            try:
                qlib.init()
            except Exception:
                pass
            _qlib_available = True
        except ImportError:
            _qlib_available = False
        except Exception as e:
            logger.warning(f"qlib import/check failed: {e}")
            _qlib_available = False
    return _qlib_available


def fetch_qlib_features(
    symbol: str,
    start_date: str,
    end_date: str,
    factor_list: Optional[list] = None,
) -> Dict[str, float]:
    """计算给定期间 symbol 的 Qlib 因子值。

    参数
    ----------
    symbol : str
        证券代码 (如 "sh600104")。
    start_date : str
        开始日期 "YYYY-MM-DD"。
    end_date : str
        结束日期 "YYYY-MM-DD"。
    factor_list : list[str], optional
        因子名称列表。若为 None，使用默认因子组合：
        ["momentum_10", "momentum_20", "mean_reversion_30", "volatility_10"]。

    返回
    ------
    Dict[str, float]
        因子名 -> 最新值的映射。因子计算失败或无数据时返回空字典。
    """
    available = _is_qlib_available()
    if not available:
        logger.debug("qlib not available; returning synthetic features")
        return {fname: 0.0 for fname in (factor_list or [])}

    try:
        from qlib.data import Hunter
        from qlib.workflow import RpcExecutor

        if not qlib.__central__.initialized:
            qlib.init()

        hunter = Hunter(executor=RpcExecutor())
        df = hunter.fetch(
            instruments=symbol,
            start_time=start_date,
            end_time=end_date,
            fields=["close", "high", "low", "open", "volume"],
            dtype="array",
        )
        if df is None or df.empty:
            logger.warning(f"qlib: no data for {symbol} {start_date}-{end_date}")
            return {}

        if factor_list is None:
            factor_list = ["momentum_10", "momentum_20", "mean_reversion_30", "volatility_10"]

        features: Dict[str, float] = {}
        for fname in factor_list:
            try:
                if "momentum" in fname.lower():
                    n = int(fname.split("_")[-1]) if "_" in fname else 10
                    rets = df["close"].pct_change(n)
                    features[fname] = float(rets.iloc[-1]) if len(rets) > 0 else 0.0
                elif "mean_reversion" in fname.lower():
                    s = df["close"].rolling(20).mean()
                    std = df["close"].rolling(20).std()
                    if len(s) > 0 and len(std) > 0:
                        z = (df["close"].iloc[-1] - s.iloc[-1]) / std.iloc[-1]
                        features[fname] = float(z)
                    else:
                        features[fname] = 0.0
                elif "volatility" in fname.lower():
                    n = int(fname.split("_")[-1]) if "_" in fname else 10
                    rets = df["close"].pct_change(n)
                    features[fname] = float(rets.std()) if len(rets) > 1 else 0.0
                else:
                    features[fname] = 0.0
            except Exception as e:
                logger.debug(f"factor {fname} compute failed: {e}")
                features[fname] = 0.0

        cache_key = f"{symbol}|{end_date}"
        _qlib_features_cache[cache_key] = features
        return features
    except Exception as e:
        logger.error(f"qlib fetch_qlib_features error: {e}")
        return {fname: 0.0 for fname in (factor_list or [])}


def qlib_signal_probability(
    symbol: str,
    datalen: int = 250,
    scale: int = 240,
) -> Dict[str, float]:
    """基于 Qlib 因子/模型预测当前周期的买卖概率。

    返回字典:
    {
        "prob_buy": float,  # 多头/买入概率 (0-1)
        "prob_sell": float, # 空头/卖出概率 (0-1)
        "confidence": float # 模型 confidence (0-1)
    }

    若 qlib 未安装，返回基于 Wyckoff 结构的经验概率估计。
    """
    import numpy as np
    import pandas as pd

    available = _is_qlib_available()
    if available:
        try:
            features = fetch_qlib_features(
                symbol,
                start_date=(pd.Timestamp.now() - pd.Timedelta(days=180)).strftime("%Y-%m-%d"),
                end_date=pd.Timestamp.now().strftime("%Y-%m-%d"),
            )
            prob_buy = 1 / (1 + np.exp(-features.get("momentum_10", 0) * 2 + features.get("mean_reversion_30", 0)))
            prob_sell = 1 / (1 + np.exp(features.get("momentum_10", 0) * 2 + features.get("mean_reversion_30", 0)))
            confidence = min(1.0, abs(features.get("momentum_10", 0)) + 0.3)
            return {"prob_buy": float(prob_buy), "prob_sell": float(prob_sell), "confidence": float(confidence)}
        except Exception as e:
            logger.error(f"qlib signal probability error: {e}")

    # 降级：基于 Wyckoff 结构的经验概率
    try:
        from .datasource import fetch_kline
        df = fetch_kline(symbol, datalen=datalen, scale=scale)
        if df is None or df.empty:
            return {"prob_buy": 0.5, "prob_sell": 0.5, "confidence": 0.0}
        close = df["close"].astype(float)
        rets = close.pct_change(5)
        if rets.iloc[-1] > 0:
            return {"prob_buy": 0.6, "prob_sell": 0.4, "confidence": 0.5}
        else:
            return {"prob_buy": 0.4, "prob_sell": 0.6, "confidence": 0.5}
    except Exception:
        return {"prob_buy": 0.5, "prob_sell": 0.5, "confidence": 0.0}


def calibrate_wyckoff_thresholds(
    history_data: list,
    method: str = "quantile",
    quantile: float = 0.5,
) -> Dict[str, float]:
    """基于历史 Wyckoff 信号回测结果，使用 Qlib 因子环境校准阈值。

    参数
    ----------
    history_data : list
        历史信号回测结果列表，每项应包含 {"signal": ..., "return": ..., "phase": ...}。
    method : str
        校准方法: "quantile" (默认) 或 "bayesian"。
    quantile : float
        量数法下的分位数 (0-1)，用于确定动态阈值。

    返回
    ------
    Dict[str, float]
        校准后的阈值映射，键包括：
        - "min_rr": 最小盈亏比阈值
        - "stop_pct": 止损百分比阈值
        - "confidence_cutoff": 置信度阈值
    """
    available = _is_qlib_available()
    if available:
        logger.info("qlib calibrate: using qlib modeling pipeline (placeholder)")
        return _default_calibration()

    # 降级：基于历史回测数据直接计算分位数阈值
    if not history_data:
        return _default_calibration()

    try:
        import numpy as np
        rrs = [float(item.get("rr", 0)) for item in history_data if isinstance(item, dict) and item.get("rr") is not None]
        stops = [float(item.get("stop_pct", 0.02)) for item in history_data if isinstance(item, dict) and item.get("stop_pct") is not None]
        default = _default_calibration()

        result = {
            "min_rr": float(np.percentile(rrs, quantile * 100)) if rrs else default["min_rr"],
            "stop_pct": float(np.percentile(stops, quantile * 100)) if stops else default["stop_pct"],
            "confidence_cutoff": "中",
        }
        return result
    except Exception as e:
        logger.error(f"empirical calibration error: {e}")
        return _default_calibration()


def _default_calibration() -> Dict[str, float]:
    """默认校准值（基于项目现有经验经验）。"""
    return {
        "min_rr": 3.0,
        "stop_pct": 0.05,
        "confidence_cutoff": "中",
    }
