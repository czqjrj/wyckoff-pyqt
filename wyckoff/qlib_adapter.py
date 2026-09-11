"""Qlib 适配层 - 与 Wyckoff 信号融合的可选量化因子层。

当安装了 qlib (>=1.6.0) 时，提供以下功能：
- fetch_qlib_features: 计算价格/技术因子
- fetch_alpha158_features: 计算 qlib Alpha158 全量 158 个因子
- train_qlib_lgbm: 基于 Alpha158 因子训练 LightGBM 涨跌模型
- qlib_signal_probability: 基于训练模型预测买卖概率
- calibrate_wyckoff_thresholds: 基于历史回测调整 Wyckoff 阈值

数据读取统一走 ``qlib.data.data.DatasetD.dataset``：qlib 0.9.7 的
``D.features`` 存在 ``inst_processors`` 参数错位 bug (microsoft/qlib#1949),
直接对 ``DatasetD.dataset`` 传位置参数可绕过。

当 qlib 未安装时，提供具备前向兼容性的占位实现，
确保主程序无 ImportError 降级为纯 Wyckoff 模式。
"""
from __future__ import annotations

import logging
import os
from typing import Any

from . import paths

logger = logging.getLogger(__name__)

_qlib_available: bool | None = None
_qlib_init_done: bool = False
_qlib_features_cache: dict[str, dict[str, float]] = {}

QLIB_MODEL_FILE = os.path.join(paths.DATA_DIR, "qlib_lgbm.joblib")

# 项目内 QLib 数据目录 (data/qlib/cn_data)。优先使用环境变量 QLIB_DATA_DIR
# 以便测试隔离; 若项目内目录不存在则回退到用户目录 ~/.qlib/qlib_data/cn_data。
_default_provider_uri = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "qlib", "cn_data")
if not os.path.isdir(_default_provider_uri):
    _legacy = os.path.join(os.path.expanduser("~"), ".qlib", "qlib_data", "cn_data")
    if os.path.isdir(_legacy):
        _default_provider_uri = _legacy
QLIB_DATA_DIR = os.environ.get("QLIB_DATA_DIR", _default_provider_uri)


def _is_qlib_available() -> bool:
    """检查 qlib 是否已安装并可用。"""
    global _qlib_available
    if _qlib_available is None:
        try:
            import qlib  # noqa: F401
            import qlib.data  # noqa: F401

            _ = qlib.__version__
            try:
                qlib.init(provider_uri=QLIB_DATA_DIR)
            except Exception:
                pass
            _qlib_available = True
        except ImportError:
            _qlib_available = False
        except Exception as e:
            logger.warning(f"qlib import/check failed: {e}")
            _qlib_available = False
    return _qlib_available


def _fetch_df(
    symbol: str,
    start_date: str,
    end_date: str,
    expressions: list[str],
) -> tuple:
    """通过 DatasetD.dataset 读取单只股票的一段因子数据。

    返回 (df, )。如果 qlib 尚未 init，先自动初始化。
    """
    global _qlib_init_done
    import qlib

    if not _qlib_init_done:
        try:
            qlib.init(provider_uri=QLIB_DATA_DIR)
        except Exception as e:
            logger.warning(f"qlib.init() failed: {e}")
        from qlib.config import C

        if C.provider_uri is not None:
            _qlib_init_done = True
    from qlib.data.data import DatasetD

    inst = symbol.upper()
    df = DatasetD.dataset([inst], list(expressions), start_date, end_date, "day")
    if df is not None and len(df) and "$" in str(df.columns[0]):
        df = df.copy()
        df.columns = [c[1:] if isinstance(c, str) and c.startswith("$") else c for c in df.columns]
    return df


def _as_single(df):
    """把 DatasetD 返回的多索引 df 变成单列交易日索引。"""
    if df is None:
        return df
    if df.index.nlevels > 1:
        try:
            df = df.droplevel(level="instrument")
        except Exception:
            df = df.reset_index(level="instrument", drop=True)
    return df


def fetch_qlib_features(
    symbol: str,
    start_date: str,
    end_date: str,
    factor_list: list | None = None,
) -> dict[str, float]:
    """计算给定期间 symbol 的 Qlib 因子值 (基于真实行情)。

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
    if factor_list is None:
        factor_list = ["momentum_10", "momentum_20", "mean_reversion_30", "volatility_10"]

    available = _is_qlib_available()
    if not available:
        logger.debug("qlib not available; returning synthetic features")
        return {fname: 0.0 for fname in factor_list}

    try:
        df = _fetch_df(symbol, start_date, end_date, ["$close", "$high", "$low", "$open", "$volume"])
    except Exception as e:
        logger.error(f"qlib fetch features failed: {e}")
        return {fname: 0.0 for fname in factor_list}
    if df is None or df.empty:
        logger.warning(f"qlib: no data for {symbol} {start_date}-{end_date}")
        return {fname: 0.0 for fname in factor_list}

    df = _as_single(df)
    if "close" not in df.columns:
        return {fname: 0.0 for fname in factor_list}

    close = df["close"].astype(float)
    features: dict[str, float] = {}
    for fname in factor_list:
        try:
            if "momentum" in fname.lower():
                n = int(fname.split("_")[-1]) if "_" in fname else 10
                ret = close.pct_change(n)
                features[fname] = float(ret.iloc[-1]) if len(ret) > 0 and ret.notna().iloc[-1] else 0.0
            elif "mean_reversion" in fname.lower():
                s = close.rolling(20).mean()
                std = close.rolling(20).std()
                if len(s) > 0 and not std.isna().iloc[-1] and std.iloc[-1] != 0:
                    z = (close.iloc[-1] - s.iloc[-1]) / std.iloc[-1]
                    features[fname] = float(z)
                else:
                    features[fname] = 0.0
            elif "volatility" in fname.lower():
                n = int(fname.split("_")[-1]) if "_" in fname else 10
                ret = close.pct_change()
                features[fname] = float(ret.rolling(n).std().iloc[-1]) if len(ret) > 1 else 0.0
            else:
                features[fname] = 0.0
        except Exception as e:
            logger.debug(f"factor {fname} compute failed: {e}")
            features[fname] = 0.0

    cache_key = f"{symbol}|{end_date}"
    _qlib_features_cache[cache_key] = features
    return features


def load_train_pool() -> list[str]:
    """加载训练股票池。

    优先从 ``data/train_pool.txt`` 读取 (每行一个 sh/sz 代码), 该文件由
    ``scripts/refresh_train_pool.py`` 生成 (东财成交额 Top 流动性样本)。
    文件不存在或为空时回退到内置的 40 只蓝筹池。
    """
    pool_file = os.path.join(paths.DATA_DIR, "train_pool.txt")
    if not os.path.isfile(pool_file):
        pool_file = os.path.join(paths.DATA_DIR, "data", "train_pool.txt")
    if os.path.isfile(pool_file):
        try:
            lines = [
                l.strip().lower()
                for l in open(pool_file, encoding="utf-8")
                if l.strip() and not l.strip().startswith("#")
            ]
            if lines:
                return lines
            logger.warning("train_pool.txt 为空, 使用内置池")
        except Exception as e:
            logger.warning(f"读取 train_pool.txt 失败: {e}")
    return [
        "sh600036", "sh601398", "sh601988", "sh601328", "sh600000",
        "sh601318", "sh601628", "sh600030", "sh601211", "sz300059",
        "sh600519", "sz000858", "sz000333", "sz000651", "sh601888",
        "sz002415", "sh600690", "sh600276", "sz300760", "sh603259",
        "sz000538", "sz002594", "sh601012", "sz300750", "sh600104",
        "sz002466", "sh600438", "sh688981", "sh603986", "sz002371",
        "sh600745", "sz300308", "sh601857", "sh600900", "sh601899",
        "sh601088", "sh600028", "sh601006", "sz000001", "sh600018",
    ]


# 威科夫看多事件 (供需逻辑: SC/PSY/AR 恐慌抛售+自动反弹, ST/Spring 震仓,
# SOS/JOC/LPS/BU 上行结构) vs 看空事件 (BC 买入高潮, UT/UTAD 上冲回落,
# LPSY/SOW 派发/弱势下攻)。
_BULL_EVENTS = frozenset(
    {"SC", "PSY", "AR", "ST", "Spring", "SOS", "JOC", "LPS", "BU", "Shakeout"})
_BEAR_EVENTS = frozenset({"BC", "UTAD", "UT", "LPSY", "SOW"})

_DOMAIN_FEATURES = (
    # VSA 量价变换 (与 vsa_classify 的 features 同定义, 按每根 K 线稠密化)
    "wy_vr", "wy_rw", "wy_cpos", "wy_trend", "wy_dir",
    "wy_cpos_trend", "wy_vr_cpos", "wy_vr_trend",
    # K 线外沿 (供给/需求余量)
    "wy_up_wick", "wy_dn_wick",
    # 技术指标 (Alpha158 未覆盖的合成段)
    "wy_rsi6", "wy_macd_hist", "wy_kdj_k", "wy_kdj_d", "wy_kdj_j",
    "wy_atr_rel", "wy_boll_pct", "wy_bw", "wy_obv_rel",
    # 事件上下文 (滚动窗口内威科夫事件计数)
    "wy_ev_bull20", "wy_ev_bear20", "wy_ev_net20",
    "wy_ev_bull60", "wy_ev_bear60", "wy_ev_net60",
    "wy_ev_conf20",
)


def compute_domain_features(
    symbol: str,
    start_date: str,
    end_date: str,
    events_window: tuple[int, ...] = (20, 60),
) -> tuple:
    """计算威科夫领域特征 (每根 K 线稠密化), 按交易日对齐 qlib 日历。

    对 symbol 从 qlib 本地数据读 OHLCV, 依次跑 add_indicators →
    find_pivots → detect_all, 提取:

    - VSA 量价特征: vr/rw/cpos/trend/dir + 交叉项 (与 vsa_classify.features 同定义)
    - K 线外沿: 上/下影线占整根振幅比例
    - 技术指标: RSI6/MACD 柱/KDJ/ATR(相对)/布林带位置与宽度/OBV 相对强度
    - 事件上下文: 滚动窗口内看多/看空事件计数、净方向、最大置信度

    返回 (DataFrame, feature_names); DataFrame index 为交易日, 与
    ``fetch_alpha158_features`` 的 index 对齐可直接 join。qlib 无该股数据或
    指标阶段失败时返回 (None, [])。
    """
    import numpy as np
    import pandas as pd

    try:
        from .indicators import add_indicators, find_pivots
        raw = _fetch_df(
            symbol, start_date, end_date,
            ["$open", "$high", "$low", "$close", "$volume"])
    except Exception as e:
        logger.debug(f"domain fetch failed for {symbol}: {e}")
        return None, []
    if raw is None or raw.empty:
        return None, []

    try:
        pdf = _as_single(raw)
        dts = list(pdf.index)
        d0 = pdf.reset_index(drop=True)
        d0["day"] = dts
        for c in ("open", "high", "low", "close", "volume"):
            d0[c] = d0[c].astype(float)

        ind = add_indicators(d0, symbol=symbol)
        pivots = find_pivots(ind, order=6)
        from .events import detect_all
        events = detect_all(ind, pivots)

        df = ind.copy()
        eps = 1e-9
        cl = df["close"].astype(float)
        hi = df["high"].astype(float)
        lo = df["low"].astype(float)
        vol = df["volume"].astype(float)
        rng = (hi - lo).clip(lower=eps)

        vy: dict[str, np.ndarray] = {}
        vr = (vol / df["vol_ma20"].replace(0, np.nan)).fillna(1.0)
        vy["wy_vr"] = vr
        vy["wy_rw"] = rng / (rng.rolling(20).mean().replace(0, np.nan)).fillna(rng)
        vy["wy_cpos"] = (cl - lo) / rng
        ma20 = df["price_ma20"]
        vy["wy_trend"] = (cl > ma20).astype(float)
        vy["wy_dir"] = df["direction"].astype(float)
        tr = 2 * vy["wy_trend"] - 1
        vy["wy_cpos_trend"] = vy["wy_cpos"] * tr
        vy["wy_vr_cpos"] = vy["wy_vr"] * vy["wy_cpos"]
        vy["wy_vr_trend"] = vy["wy_vr"] * tr
        vy["wy_up_wick"] = df["upper_wick"] / rng
        vy["wy_dn_wick"] = df["lower_wick"] / rng
        vy["wy_rsi6"] = df["rsi_6"]
        vy["wy_macd_hist"] = df["macd_hist"]
        vy["wy_kdj_k"] = df["kdj_k"]
        vy["wy_kdj_d"] = df["kdj_d"]
        vy["wy_kdj_j"] = df["kdj_j"]
        vy["wy_atr_rel"] = df["atr"] / cl
        bbw = (df["boll_up"] - df["boll_dn"]).replace(0, np.nan)
        vy["wy_boll_pct"] = (cl - df["boll_dn"]) / bbw
        vy["wy_bw"] = bbw / df["boll_mid"].replace(0, np.nan)
        obv = df["obv"].astype(float)
        vy["wy_obv_rel"] = (obv - obv.rolling(20).mean()) / (
            obv.rolling(20).std().replace(0, np.nan)).fillna(1.0)

        n_bars = len(df)
        ev_idx = np.array([e["idx"] for e in events], dtype=int)
        ev_bull = np.array([1 if e["type"] in _BULL_EVENTS else 0 for e in events])
        ev_bear = np.array([1 if e["type"] in _BEAR_EVENTS else 0 for e in events])
        ev_conf = np.array([float(e.get("conf", 0) or 0) for e in events])

        for w in events_window:
            key = str(w)
            bull = np.zeros(n_bars)
            bear = np.zeros(n_bars)
            net = np.zeros(n_bars)
            maxconf = np.zeros(n_bars)
            for i in range(n_bars):
                lo_idx = np.searchsorted(ev_idx, i - w + 1, side="left")
                hi_idx = np.searchsorted(ev_idx, i, side="right")
                if hi_idx > lo_idx:
                    sl = slice(lo_idx, hi_idx)
                    b = int(ev_bull[sl].sum())
                    s = int(ev_bear[sl].sum())
                    bull[i] = b
                    bear[i] = s
                    net[i] = (b - s) / (b + s + 1.0)
                    maxconf[i] = ev_conf[sl].max()
            vy[f"wy_ev_bull{key}"] = bull
            vy[f"wy_ev_bear{key}"] = bear
            vy[f"wy_ev_net{key}"] = net
            if w == min(events_window):
                vy["wy_ev_conf20"] = maxconf

        feat = pd.DataFrame(
            {k: (v.to_numpy() if isinstance(v, pd.Series) else v) for k, v in vy.items()})
        feat.index = pd.DatetimeIndex(dts, name="datetime")
        feat = feat[list(_DOMAIN_FEATURES)]
        feat = feat.replace([np.inf, -np.inf], np.nan)
        return feat, list(_DOMAIN_FEATURES)
    except Exception as e:
        logger.warning(f"domain features failed for {symbol}: {type(e).__name__}: {e}")
        return None, []


def fetch_alpha158_features(
    symbol: str,
    start_date: str,
    end_date: str,
) -> tuple:
    """计算 symbol 的 qlib Alpha158 全量因子。

    返回 (DataFrame, feature_names)，DataFrame 的 index 为交易日,
    列为 158 个因子名 (KMID, KLEN, ... ROC5, MA5, ...)。若 qlib
    不可用或读取失败，返回 (None, [])。
    """
    available = _is_qlib_available()
    if not available:
        return None, []

    try:
        from qlib.contrib.data.loader import Alpha158DL

        fields, names = Alpha158DL.get_feature_config()
        df = _fetch_df(symbol, start_date, end_date, fields)
    except Exception as e:
        logger.error(f"qlib fetch alpha158 failed for {symbol}: {e}")
        return None, []

    if df is None or df.empty:
        return None, names

    df = _as_single(df)
    df.columns = names
    return df, names


def _load_qlib_model():
    """加载已训练的 LightGBM 模型, 未训练或损坏时返回 None。"""
    try:
        import joblib

        if not os.path.exists(QLIB_MODEL_FILE):
            return None
        obj = joblib.load(QLIB_MODEL_FILE)
        if not isinstance(obj, dict) or "model" not in obj or "feature_names" not in obj:
            return None
        return obj
    except Exception as e:
        logger.warning(f"qlib model load failed: {e}")
        return None


def _spread_probabilities(preds: np.ndarray, auc: float) -> np.ndarray:
    """将模型原始输出按样本内分位数归一化, 放大区分度。

    模型原始概率在 A 股日频上高度集中于 0.35~0.65 (std≈0.05),
    永远无法穿越 0.6/0.4 的方向修正阈值。这里按预测值在整个
    样本内的经验分位数重新映射: 分位数 rank∈[0,1] →

        prob = 0.5 + (rank - 0.5) * spread
        spread = min(1.0, 0.3 + auc * 0.6)   # AUC 越高, 允许离中性越远

    效果: 强看多 (>75分位) → prob>0.6, 强看空 (<25分位) → prob<0.4。
    弱 AUC (0.5) 时 spread 收敛到 0.6, 仍有少量极端信号但整体贴近中性。
    """
    import numpy as np

    n = len(preds)
    if n == 0:
        return np.array([])
    ranks = (preds[None, :] <= preds[:, None]).mean(axis=1)
    spread = min(1.0, 0.3 + float(auc) * 0.6)
    prob = 0.5 + (ranks - 0.5) * spread
    return np.clip(prob, 0.01, 0.99)


def qlib_probability_series(
    feat_df: pd.DataFrame,
    model_obj: dict | None = None,
) -> np.ndarray | None:
    """对整段因子表做模型预测并分位数归一化, 返回每个交易日的 buy 概率。

    用于消融实验和实时推理的统一入口, 保证两者口径一致。
    """
    import numpy as np

    if model_obj is None:
        model_obj = _load_qlib_model()
    if model_obj is None or feat_df is None or feat_df.empty:
        return None
    fnames = model_obj["feature_names"]
    model = model_obj["model"]
    X = feat_df[fnames].reindex(columns=fnames).astype(float)
    if hasattr(model, "predict_proba"):
        buy_class = int(model_obj.get("buy_class", 1))
        raw = model.predict_proba(X)[:, buy_class]
    else:
        raw = model.predict(X)
        raw = [float(p) if 0 <= p <= 1 else 1 / (1 + np.exp(-p)) for p in raw]
    return _spread_probabilities(np.asarray(raw, dtype=float), float(model_obj.get("auc", 0.55)))


def qlib_signal_probability(
    symbol: str,
    datalen: int = 250,
    scale: int = 240,
) -> dict[str, float]:
    """基于 Qlib 训练模型预测当前周期的买卖概率。

    返回字典:
    {
        "prob_buy": float,  # 多头/买入概率 (0-1)
        "prob_sell": float, # 空头/卖出概率 (0-1)
        "confidence": float # 模型 confidence (0-1)
    }

    若模型未训练，返回基于 Alpha158 动量/均值回归的经验概率估计。
    若 qlib 未安装，返回基于 Wyckoff 结构的经验概率估计。
    """
    import numpy as np
    import pandas as pd

    available = _is_qlib_available()
    end_date = pd.Timestamp.now().strftime("%Y-%m-%d")
    start_date = (pd.Timestamp.now() - pd.Timedelta(days=365)).strftime("%Y-%m-%d")

    if available:
        model_obj = _load_qlib_model()
        if model_obj is not None:
            try:
                df, names = fetch_alpha158_features(symbol, start_date, end_date)
                if df is not None and len(df):
                    fnames = model_obj["feature_names"]
                    if any(f.startswith("wy_") for f in fnames):
                        dfeat, _ = compute_domain_features(
                            symbol, start_date, end_date)
                        if dfeat is not None and len(dfeat):
                            df = df.join(dfeat, how="inner")
                    series = qlib_probability_series(df, model_obj)
                    if series is not None and len(series):
                        prob_buy = float(series[-1])
                        prob_sell = 1.0 - prob_buy
                        confidence = float(model_obj.get("auc", 0.55))
                        return {
                            "prob_buy": min(max(prob_buy, 0.01), 0.99),
                            "prob_sell": min(max(prob_sell, 0.01), 0.99),
                            "confidence": confidence,
                        }
            except Exception as e:
                logger.error(f"qlib model inference error: {e}")

        try:
            features = fetch_qlib_features(symbol, start_date=start_date, end_date=end_date)
            prob_buy = 1 / (1 + np.exp(-features.get("momentum_10", 0) * 2 + features.get("mean_reversion_30", 0)))
            prob_sell = 1 / (1 + np.exp(features.get("momentum_10", 0) * 2 + features.get("mean_reversion_30", 0)))
            confidence = min(1.0, abs(features.get("momentum_10", 0)) + 0.3)
            return {
                "prob_buy": float(min(max(prob_buy, 0.01), 0.99)),
                "prob_sell": float(min(max(prob_sell, 0.01), 0.99)),
                "confidence": float(confidence),
            }
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


def train_qlib_lgbm(
    symbols: list[str] | None = None,
    start_date: str = "2019-01-01",
    end_date: str = "2026-07-03",
    horizon: int = 5,
    test_ratio: float = 0.2,
    num_boost_round: int = 200,
    save: bool = True,
) -> dict[str, Any] | None:
    """训练 LightGBM 涨跌模型。

    对每只股票提取 Alpha158 因子, 标签为未来 ``horizon`` 日收益的符号
    (>0 记为 1, 否则 0)。按时间顺序划分训练/验证集 (避免未来函数),
    训练二分类 LightGBM 并返回指标。``save=True`` 时将模型写入
    ``QLIB_MODEL_FILE``。

    symbols 默认取沪深主流的流动性样本 (与 backtest 基准池一致)。
    """
    available = _is_qlib_available()
    if not available:
        logger.warning("qlib not available; cannot train")
        return None

    try:
        import joblib
        import lightgbm as lgb
        import numpy as np
        import pandas as pd
    except ImportError as e:
        logger.warning(f"training deps missing: {e}")
        return None

    if symbols is None:
        symbols = [
            "sh600036", "sh601398", "sh601988", "sh601328", "sh600000",
            "sh601318", "sh601628", "sh600030", "sh601211", "sz300059",
            "sh600519", "sz000858", "sz000333", "sz000651", "sh601888",
            "sz002415", "sh600690", "sh600276", "sz300760", "sh603259",
            "sz000538", "sz002594", "sh601012", "sz300750", "sh600104",
            "sz002466", "sh600438", "sh688981", "sh603986", "sz002371",
            "sh600745", "sz300308", "sh601857", "sh600900", "sh601899",
            "sh601088", "sh600028", "sh601006", "sz000001", "sh600018",
        ]

    frames = []
    for sym in symbols:
        df, names = fetch_alpha158_features(sym, start_date, end_date)
        if df is None or len(df) < 60:
            continue
        cdf = _fetch_df(sym, start_date, end_date, ["$close"])
        if cdf is None or cdf.empty:
            continue
        cdf = _as_single(cdf)
        close = cdf["close"].astype(float)
        if len(close) != len(df):
            close = close.reindex(df.index)
        df = df.copy()
        df["__symbol"] = sym
        df["__return"] = close.shift(-horizon) / close - 1
        frames.append(df)
    if not frames:
        logger.warning("no usable data for training")
        return None

    data = pd.concat(frames, axis=0)
    data = data.dropna(subset=["__return"])
    if len(data) < 200:
        logger.warning(f"insufficient training rows: {len(data)}")
        return None

    data["__label"] = (data["__return"] > 0).astype(np.int8)
    feature_names = [c for c in names if c in data.columns]

    data = data.sort_index()
    n = len(data)
    split = int(n * (1 - test_ratio))
    train = data.iloc[:split]
    valid = data.iloc[split:]

    X_train = train[feature_names].astype(float)
    y_train = train["__label"].values
    X_valid = valid[feature_names].astype(float)
    y_valid = valid["__label"].values

    dtr = lgb.Dataset(X_train, label=y_train, feature_name=feature_names)
    dva = lgb.Dataset(X_valid, label=y_valid, feature_name=feature_names, reference=dtr)

    params = {
        "objective": "binary",
        "metric": "auc",
        "boosting_type": "gbdt",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "verbose": -1,
        "num_threads": 4,
        "seed": 42,
    }
    booster = lgb.train(
        params,
        dtr,
        num_boost_round=num_boost_round,
        valid_sets=[dva],
        callbacks=[lgb.early_stopping(20), lgb.log_evaluation(0)],
    )

    preds = booster.predict(X_valid, num_iteration=booster.best_iteration)
    from sklearn.metrics import accuracy_score, roc_auc_score

    auc = float(roc_auc_score(y_valid, preds)) if len(np.unique(y_valid)) > 1 else 0.5
    acc = float(accuracy_score(y_valid, (preds > 0.5).astype(int)))
    base_rate = float(y_valid.mean())

    result: dict[str, Any] = {
        "model": booster,
        "feature_names": feature_names,
        "horizon": horizon,
        "auc": auc,
        "acc": acc,
        "base_rate": base_rate,
        "buy_class": 1,
        "n_train": int(len(X_train)),
        "n_valid": int(len(X_valid)),
        "symbols": list(symbols),
        "train_start": str(data.index.get_level_values(-1).min()),
        "train_end": str(data.index.get_level_values(-1).max()),
        "trained_at": pd.Timestamp.now().isoformat(),
    }

    if save:
        joblib.dump(result, QLIB_MODEL_FILE)
        logger.info(f"qlib model saved to {QLIB_MODEL_FILE} (auc={auc:.3f}, acc={acc:.3f})")
    return result


def calibrate_wyckoff_thresholds(
    history_data: list,
    method: str = "quantile",
    quantile: float = 0.5,
) -> dict[str, float]:
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
        model_obj = _load_qlib_model()
        if model_obj is not None:
            # 用模型验证集的 AUC 对阈值做温和校准: 模型越准, 阈值容忍度越高
            auc = float(model_obj.get("auc", 0.55))
            default = _default_calibration()
            min_rr = default["min_rr"] * (1.0 - min(0.2, max(0.0, (auc - 0.5) * 0.6)))
            stop_pct = default["stop_pct"] * (1.0 + min(0.1, max(0.0, (auc - 0.5) * 0.3)))
            return {
                "min_rr": round(float(min_rr), 3),
                "stop_pct": round(float(stop_pct), 4),
                "confidence_cutoff": "中",
            }
        logger.info("qlib calibrate: using qlib modeling pipeline (placeholder)")
        return _default_calibration()

    # 降级：基于历史回测数据直接计算分位数阈值
    if not history_data:
        return _default_calibration()

    try:
        import numpy as np

        rrs = [float(item.get("rr", 0)) for item in history_data if isinstance(item, dict) and item.get("rr") is not None]
        stops = [
            float(item.get("stop_pct", 0.02))
            for item in history_data
            if isinstance(item, dict) and item.get("stop_pct") is not None
        ]
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


def _default_calibration() -> dict[str, float]:
    """默认校准值（基于项目现有经验经验）。"""
    return {
        "min_rr": 3.0,
        "stop_pct": 0.05,
        "confidence_cutoff": "中",
    }


def train_qlib_lgbm_v2(
    symbols: list[str] | None = None,
    start_date: str = "2019-01-01",
    end_date: str = "2026-07-03",
    horizon: int = 10,
    threshold: float = 0.01,
    test_ratio: float = 0.2,
    num_boost_round: int = 500,
    save: bool = True,
    use_domain: bool = True,
) -> dict[str, Any] | None:
    """V3 训练: 在 V2 四项改进基础上注入威科夫领域特征 + 扩充股票池。

    改进点:
    1. 标签噪声: |return| < threshold 的样本丢弃, 只保留明确涨跌
    2. 特征冗余: 皮尔逊相关 > 0.95 的特征组只保留最重要的一个
    3. 超参数: 加强正则化 (min_child_samples, lambda, max_depth)
    4. 多周期: 同时训练 5/10/20 日, 选最优周期
    5. 领域特征: VSA 量价 / 事件上下文 / 技术指标 稠密化并入
       (use_domain=True, 见 compute_domain_features)
    6. 股票池: 默认从 data/train_pool.txt 读取扩充池 (>200 只)
    """
    available = _is_qlib_available()
    if not available:
        logger.warning("qlib not available; cannot train v2")
        return None

    try:
        import joblib
        import lightgbm as lgb
        import numpy as np
        import pandas as pd
        from sklearn.metrics import accuracy_score, roc_auc_score
    except ImportError as e:
        logger.warning(f"training deps missing: {e}")
        return None

    if symbols is None:
        symbols = load_train_pool()

    logger.info(f"v3: pool size {len(symbols)}, use_domain={use_domain}")
    all_feature_names = list(_DOMAIN_FEATURES) if use_domain else []
    frames = []
    for sym in symbols:
        df, names = fetch_alpha158_features(sym, start_date, end_date)
        if df is None or len(df) < 80:
            continue
        all_feature_names.extend(n for n in names if n not in all_feature_names)
        if use_domain:
            dfeat, dnames = compute_domain_features(sym, start_date, end_date)
            if dfeat is not None and len(dfeat) >= 80:
                df = df.join(dfeat, how="inner")
                if len(df) < 80:
                    continue
                all_feature_names.extend(
                    n for n in dnames if n not in all_feature_names)
        cdf = _fetch_df(sym, start_date, end_date, ["$close"])
        if cdf is None or cdf.empty:
            continue
        cdf = _as_single(cdf)
        close = cdf["close"].astype(float)
        if len(close) != len(df):
            close = close.reindex(df.index)
        df = df.copy()
        df["__symbol"] = sym
        for h in (5, 10, 20):
            df[f"__ret{h}"] = close.shift(-h) / close - 1
        frames.append(df)

    if not frames:
        logger.warning("v2: no usable data")
        return None

    data = pd.concat(frames, axis=0)
    feature_names = [c for c in all_feature_names if c in data.columns]

    feat_corr = data[feature_names].corr().abs()
    upper = feat_corr.where(np.triu(np.ones(feat_corr.shape), k=1).astype(bool))
    drop_feats = set()
    for col in upper.columns:
        high_corr = upper.index[upper[col] > 0.95].tolist()
        if high_corr:
            candidates = [col] + high_corr
            variances = {c: data[c].var() for c in candidates if c in data.columns}
            keep = max(variances, key=variances.get)
            drop_feats.update(set(candidates) - {keep})
    feature_names = [f for f in feature_names if f not in drop_feats]
    logger.info(f"v2: kept {len(feature_names)} features (dropped {len(drop_feats)} redundant)")

    results = {}
    for h in (5, 10, 20):
        col = f"__ret{h}"
        tmp = data.dropna(subset=[col]).copy()
        if len(tmp) < 300:
            continue

        mask = tmp[col].abs() >= threshold
        tmp = tmp[mask].copy()
        tmp["__label"] = (tmp[col] > 0).astype(np.int8)
        logger.info(f"v2 h={h}: {len(tmp)} samples (noise removed: {(~mask).sum()})")

        if tmp["__label"].nunique() < 2:
            continue

        tmp = tmp.sort_index()
        n = len(tmp)
        split = int(n * (1 - test_ratio))
        train = tmp.iloc[:split]
        valid = tmp.iloc[split:]

        X_train = train[feature_names].astype(float)
        y_train = train["__label"].values
        X_valid = valid[feature_names].astype(float)
        y_valid = valid["__label"].values

        params = {
            "objective": "binary",
            "metric": "auc",
            "boosting_type": "gbdt",
            "learning_rate": 0.03,
            "num_leaves": 24,
            "max_depth": 6,
            "min_child_samples": 40,
            "reg_alpha": 0.1,
            "reg_lambda": 1.0,
            "feature_fraction": 0.7,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "verbose": -1,
            "num_threads": 4,
            "seed": 42,
            "is_unbalance": True,
        }
        dtr = lgb.Dataset(X_train, label=y_train, feature_name=feature_names)
        dva = lgb.Dataset(X_valid, label=y_valid, feature_name=feature_names, reference=dtr)
        booster = lgb.train(
            params,
            dtr,
            num_boost_round=num_boost_round,
            valid_sets=[dva],
            callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)],
        )

        preds = booster.predict(X_valid, num_iteration=booster.best_iteration)
        auc = float(roc_auc_score(y_valid, preds)) if len(np.unique(y_valid)) > 1 else 0.5
        acc = float(accuracy_score(y_valid, (preds > 0.5).astype(int)))
        base_rate = float(y_valid.mean())

        imp = booster.feature_importance(importance_type="gain")
        top_idx = np.argsort(imp)[::-1][:20]
        top_feats = [(feature_names[i], float(imp[i])) for i in top_idx]

        results[h] = {
            "model": booster,
            "feature_names": feature_names,
            "horizon": h,
            "auc": auc,
            "acc": acc,
            "base_rate": base_rate,
            "buy_class": 1,
            "n_train": int(len(X_train)),
            "n_valid": int(len(X_valid)),
            "n_dropped_noise": int((~mask).sum()),
            "n_features": len(feature_names),
            "top_features": top_feats,
            "symbols": list(symbols),
            "train_start": str(tmp.index.get_level_values(-1).min()),
            "train_end": str(tmp.index.get_level_values(-1).max()),
            "trained_at": pd.Timestamp.now().isoformat(),
        }
        logger.info(f"v2 h={h}: auc={auc:.4f} acc={acc:.4f} base={base_rate:.3f}")

    if not results:
        logger.warning("v2: no valid horizon results")
        return None

    best_h = max(results, key=lambda k: results[k]["auc"])
    best = results[best_h]
    best["all_horizons"] = {h: {"auc": r["auc"], "acc": r["acc"]} for h, r in results.items()}

    if save:
        joblib.dump(best, QLIB_MODEL_FILE)
        logger.info(f"v2 model saved: horizon={best_h} auc={best['auc']:.4f}")

    return best


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Wyckoff Qlib 模型训练/推理")
    parser.add_argument("--train", action="store_true", help="训练 LightGBM 涨跌模型")
    parser.add_argument("--train-v2", action="store_true", help="V2 训练: 改进标签/特征/超参数")
    parser.add_argument("--train-v3", action="store_true", help="V3 训练: 注入领域特征 + 扩充股票池")
    parser.add_argument("--no-domain", action="store_true", help="V3: 关闭领域特征 (仅 Alpha158)")
    parser.add_argument("--symbols", type=str, default="", help="逗号分隔的股票代码候选池")
    parser.add_argument("--start", type=str, default="2019-01-01")
    parser.add_argument("--end", type=str, default="2026-07-03")
    parser.add_argument("--horizon", type=int, default=5, help="预测未来 N 日涨跌")
    parser.add_argument("--threshold", type=float, default=0.01, help="V2: 收益阈值, 过滤噪声")
    args = parser.parse_args()

    if args.train_v2 or args.train_v3:
        sym_list = [s.strip().lower() for s in args.symbols.split(",") if s.strip()] or None
        res = train_qlib_lgbm_v2(
            symbols=sym_list, start_date=args.start, end_date=args.end,
            horizon=args.horizon, threshold=args.threshold,
            use_domain=args.train_v3 and not args.no_domain,
        )
        if res:
            print(f"\n=== {'V3' if args.train_v3 else 'V2'} 训练完成 ===")
            print(f"最优周期: {res['horizon']}日")
            print(f"AUC: {res['auc']:.4f}  准确率: {res['acc']:.4f}")
            print(f"训练集: {res['n_train']}  验证集: {res['n_valid']}")
            print(f"过滤噪声: {res['n_dropped_noise']} 样本")
            print(f"特征数: {res['n_features']}")
            if "all_horizons" in res:
                print(f"各周期 AUC: {res['all_horizons']}")
            print(f"Top-5 特征:")
            for name, imp in res.get("top_features", [])[:5]:
                print(f"  {name}: {imp:.1f}")
        else:
            print(f"{'V3' if args.train_v3 else 'V2'} training failed")
    elif args.train:
        sym_list = [s.strip().lower() for s in args.symbols.split(",") if s.strip()] or None
        res = train_qlib_lgbm(symbols=sym_list, start_date=args.start, end_date=args.end, horizon=args.horizon)
        if res:
            print(
                f"train complete: auc={res['auc']:.3f}, acc={res['acc']:.3f}, "
                f"base_rate={res['base_rate']:.3f}, rows(train/valid)={res['n_train']}/{res['n_valid']}"
            )
        else:
            print("train failed")
    else:
        print("use --train or --train-v2 to train the model")
