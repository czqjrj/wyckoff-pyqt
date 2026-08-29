"""L4 特征级在线校准模型: 结构特征 + 类型 one-hot + 强 L2 逻辑回归。

分层校准方案中 L4 是核心层:
  - 输入: 信号 bar 的结构特征 (量比/波幅/收盘位置/趋势/60日区间位/布林位/
    波动率分位/共振数/方向) + 事件类型 one-hot + 当前 conf。
  - 输出: P(up|X), 映射为方向相关的可靠性 (0-100), 与经验校准混合后接管 conf。
  - 正则: 样本量小 (单类型 9~80 条), 用强 L2 (SGDClassifier alpha) 抗过拟合。
  - 门控: 只有训练标签与样本外 AUC 达到门槛才接管 conf (数据驱动, 而非拍脑袋)。

训练数据来自信号准确度库 (record_signals 捕获的 features + 之后真实行情评估
的 ret_20)。按信号日期 70%/30% 时序切分, 只上报样本外表现。
存储: ~/.wyckoff/wx_online_model.json (系数快照, 供运行期零依赖推理)。
"""
import json
import math
import os
import sys
import time

import numpy as np

from .config import EVENT_COLORS, event_dir
from .paths import ONLINE_MODEL_FILE

try:
    from sklearn.linear_model import SGDClassifier
except Exception:  # pragma: no cover - 环境缺 sklearn 时降级为不可用
    SGDClassifier = None

MODEL_VERSION = 3
FEATURE_VERSION = 3

# 特征定义 (与 events.event_confidence 捕获的 feat 字段对齐)
# v2 追加 L5 威科夫语境特征 (wyckoff/context.py enrich 落库):
#   阶段 one-hot / 交易区间位置·年龄·宽度 / 因果长度 / 量能萎缩比 /
#   RS 百分位 / 指数相位对齐 / 板块强度百分位 (预留)
CONT_FEATURES = ["vr", "rw", "cpos", "trend", "pos60", "boll_pct",
                 "bw_pct", "reson", "conf", "dir",
                 "ph_acc", "ph_dis", "ph_mup", "ph_mkd",
                 "tr_pos", "tr_age_n", "tr_wid_n",
                 "base_len_n", "vol_shrink", "rs_pct", "idx_align", "sec_pct"]
_EVENT_TYPES = tuple(dict.fromkeys(EVENT_COLORS.keys()))  # 与检测器全集一致 (含 PSY)

# VSA 标签 (与 vsa.py 检测器全集对齐): 纳入类型 one-hot, 让模型能区分 VSA 类型。
try:
    from .vsa import ALL_VSA_LABELS as _VSA_TYPES
except Exception:  # pragma: no cover - 不可用时回退内置全集
    _VSA_TYPES = ("ND", "NS", "SC", "BC", "SV", "UT", "SPR", "ER", "EF",
                  "DEM", "SUP", "ABS", "CHOC", "EVR",
                  "UPT", "TEST", "ETR", "ETF", "TRU", "TRD")
TYPE_FEATURES = [f"type_{t}" for t in _EVENT_TYPES] + \
                [f"vtype_{t}" for t in _VSA_TYPES]
FEATURES = CONT_FEATURES + TYPE_FEATURES
_FEAT_INDEX = {f: i for i, f in enumerate(FEATURES)}

# 缺失特征的中性填充 (boll_pct 中轨; 其余来自 context.SAFE_FILL)
try:
    from .context import CONTEXT_FEAT_KEYS as _CTX_KEYS
    from .context import SAFE_FILL as _CTX_SAFE_FILL
    _NEUTRAL_FILL = {"boll_pct": 0.5, **_CTX_SAFE_FILL}
except Exception:  # pragma: no cover - context 不可用时退回 v1 行为
    _NEUTRAL_FILL = {"boll_pct": 0.5}
    _CTX_KEYS = ()

# 接管 conf 的启用开关与门槛 (数据驱动门控)
USE_MODEL_CONF = True
MODEL_MIN_TRAIN = 60      # 训练标签数下限
MODEL_MIN_OOS = 15        # 样本外标签数下限
MODEL_MIN_AUC = 0.55      # 样本外 AUC 下限 (随机=0.5, 无区分度不接管)
MODEL_MAX_BLEND = 0.70    # 模型可靠性最多占 conf 的比例
MODEL_HORIZON = 20        # 标签周期 (与 win_rate_of 校准口径一致)

# 在线学习 (SGDClassifier) 超参: 强 L2 正则 + 最优学习率
_L2_ALPHA = 1e-3
_MAX_ITER = 2000


# ── 特征向量 ──

def feature_vector(e) -> np.ndarray:
    """把事件/记录 dict 映射为定长特征向量 (缺失特征以中性值填充)。

    VSA 记录 (kind=vsa) 用 vtype_ one-hot 区分类型; 事件记录用 type_ one-hot。
    """
    x = np.zeros(len(FEATURES), dtype=float)
    f = e.get("feat") or e.get("features") or {}
    for i, name in enumerate(CONT_FEATURES):
        v = f.get(name)
        if v is None or (isinstance(v, float) and not math.isfinite(v)):
            v = _NEUTRAL_FILL.get(name, 0.0)  # 缺失/NaN → 中性安全值
        if name == "dir" and v == 0:
            v = _record_dir(e)
        x[i] = float(v)
    etype = e.get("type", "?")
    if e.get("kind") == "vsa":
        ti = _FEAT_INDEX.get(f"vtype_{etype}")
        if ti is not None:
            x[ti] = 1.0
    else:
        ti = _FEAT_INDEX.get(f"type_{etype}")
        if ti is not None:
            x[ti] = 1.0
    x[_FEAT_INDEX["conf"]] = float(e.get("conf", 50)) / 100.0
    return x


# ── 状态存储 ──

def _load_state():
    try:
        with open(ONLINE_MODEL_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_state(state):
    import os

    from ._shared import atomic_write_json
    os.makedirs(os.path.dirname(ONLINE_MODEL_FILE), exist_ok=True)
    atomic_write_json(ONLINE_MODEL_FILE, state)


# ── 训练 ──

def _sig_date_ts(r):
    """把信号日期解析为时间戳 (用于时序切分); 解析失败返回 None。"""
    try:
        import pandas as pd
        return pd.Timestamp(r.get("date", "")).timestamp()
    except Exception:
        return None


def _record_dir(r):
    """记录标称方向: 事件用 event_dir, VSA 用 vsa_dir; 缺省按类型推断。"""
    kind = r.get("kind", "event")
    try:
        if kind == "vsa":
            from .config import vsa_dir
            return vsa_dir(str(r.get("type", "")))
        from .config import event_dir
        return event_dir(str(r.get("type", "")))
    except Exception:
        return 0


def labeled_rows(records, horizon=MODEL_HORIZON):
    """筛选带特征 + 已评估标签的信号记录 (事件 + VSA)。"""
    out = []
    for r in records or []:
        if r.get("kind") not in ("event", "vsa"):
            continue
        if not r.get("features"):
            continue
        res = (r.get("results") or {}).get(str(horizon))
        if not res or res.get("ret") is None:
            continue
        ts = _sig_date_ts(r)
        if ts is None:
            continue
        out.append((r, float(res["ret"]), ts))
    return out


def _n_ctx_labeled(rows):
    """带 L5 语境特征的已标注样本数 (校准中心展示语境覆盖度)。"""
    if not _CTX_KEYS:
        return 0
    try:
        return int(sum(
            1 for r, _, _ in rows
            if any(k in (r.get("features") or {}) for k in _CTX_KEYS)))
    except Exception:
        return 0


def train_model(records=None, horizon=MODEL_HORIZON, oos_frac=0.3, seed=42):
    """全量重训在线校准模型并保存状态。

    按信号日期时序切分: 前 1-oos_frac 训练 / 后 oos_frac 样本外评估。
    返回状态 dict (无论是否达到接管门槛都保存, 供校准中心展示积累进度)。
    """
    if SGDClassifier is None:
        return _load_state()
    from .signal_accuracy import load_signals
    if records is None:
        records = load_signals()
    rows = labeled_rows(records, horizon=horizon)
    if len(rows) < 5:
        state = _load_state()
        state["n_labels"] = len(rows)
        state["n_ctx_labels"] = _n_ctx_labeled(rows)
        state["n_train"] = 0
        state["trained_at"] = time.time()
        state["ready"] = False
        _save_state(state)
        return state

    rows.sort(key=lambda r: r[2])  # 按信号日期升序
    split = int(len(rows) * (1.0 - oos_frac))
    split = max(1, min(len(rows) - 1, split))
    train, oos = rows[:split], rows[split:]

    Xt = np.array([feature_vector(r) for r, _, _ in train])
    yt = np.array([1 if ret > 0 else 0 for _, ret, _ in train])
    if len(np.unique(yt)) < 2 or len(train) < 5:
        state = _load_state()
        state.update({"n_labels": len(rows), "n_ctx_labels": _n_ctx_labeled(rows),
                      "n_train": len(train),
                      "n_oos": len(oos), "trained_at": time.time(),
                      "ready": False, "note": "训练集无正/负样本区分"})
        _save_state(state)
        return state

    # 类别不平衡 (Spring 85% vs SOS 50%): 用全训练集频率估算均衡权重。
    try:
        from sklearn.utils.class_weight import compute_class_weight
        cw = compute_class_weight("balanced", classes=np.array([0, 1]), y=yt)
        class_weight = {0: float(cw[0]), 1: float(cw[1])}
    except Exception:
        class_weight = None

    clf = SGDClassifier(loss="log_loss", penalty="l2", alpha=_L2_ALPHA,
                        max_iter=_MAX_ITER, tol=1e-5, random_state=seed,
                        class_weight=class_weight, learning_rate="optimal")
    # 全量重训收敛; "在线"语义由"记录不断积累 + 每次重训 + 运行期零延迟推理"承载。
    clf.fit(Xt, yt)

    # 样本外评估
    Xo = np.array([feature_vector(r) for r, _, _ in oos])
    yo = np.array([1 if ret > 0 else 0 for _, ret, _ in oos])
    prob = clf.predict_proba(Xo)[:, 1]
    auc_oos = _auc(yo, prob) if len(oos) >= 2 and len(np.unique(yo)) >= 2 else None
    ic_oos = _spearman(prob, np.array([ret for _, ret, _ in oos])) if len(oos) >= 3 else None
    acc_oos = float(((prob > 0.5).astype(int) == yo).mean())
    coef = clf.coef_[0]

    state = {
        "version": MODEL_VERSION,
        "feat_version": FEATURE_VERSION,
        "features": list(FEATURES),
        "horizon": horizon,
        "trained_at": time.time(),
        "n_labels": len(rows),
        "n_ctx_labels": _n_ctx_labeled(rows),
        "n_train": len(train),
        "n_oos": len(oos),
        "auc_oos": round(float(auc_oos), 4) if auc_oos is not None else None,
        "ic_oos": round(float(ic_oos), 4) if ic_oos is not None else None,
        "acc_oos": round(acc_oos, 4),
        "intercept": float(clf.intercept_[0]),
        "coef": [float(v) for v in coef],
        "ready": _ready(state_ready_check={
            "feat_version": FEATURE_VERSION,
            "n_train": len(train), "n_oos": len(oos), "auc_oos": auc_oos}),
    }
    _save_state(state)
    return state


def _ready(state_ready_check):
    """接管门槛判定 (与 apply_model_conf 一致)。

    额外要求状态文件的 feat_version 与当前代码一致: 特征集升级后旧模型
    系数维度失配, 必须静默失效等待重训, 绝不能用旧系数配新特征向量。"""
    if int(state_ready_check.get("feat_version", 0) or 0) != FEATURE_VERSION:
        return False
    n_train = int(state_ready_check.get("n_train", 0))
    n_oos = int(state_ready_check.get("n_oos", 0))
    auc = state_ready_check.get("auc_oos")
    return bool(n_train >= MODEL_MIN_TRAIN and n_oos >= MODEL_MIN_OOS
                and auc is not None and auc >= MODEL_MIN_AUC)


def model_status():
    """当前模型状态 (校准中心展示用)。无状态时返回空 dict。"""
    st = _load_state()
    if not st:
        return st
    st["ready"] = _ready(st)
    st["blend"] = round(_blend_weight(st.get("n_train", 0)), 3)
    return st


# ── 运行期推理 (conf 接管) ──

def _blend_weight(n_train):
    """模型权重随标签数爬坡: 0.3 → MODEL_MAX_BLEND。"""
    if n_train < MODEL_MIN_TRAIN:
        return 0.0
    return min(MODEL_MAX_BLEND, 0.3 + n_train / 2000.0)


def apply_model_conf(events):
    """把事件 conf 与模型 P(up|X) 混合 (方向相关的可靠性)。

    仅在模型达到接管门槛时生效; 涨跌停等硬性低置信档 (conf<=5) 不动。
    返回被改写的事件数。
    """
    if not USE_MODEL_CONF:
        return 0
    st = _load_state()
    if not _ready(st):
        return 0
    coef = np.array(st["coef"], dtype=float)
    intercept = float(st["intercept"])
    w = _blend_weight(int(st.get("n_train", 0)))
    n_apply = 0
    for e in events or []:
        conf = e.get("conf")
        if not isinstance(conf, (int, float)):
            continue
        if conf <= 5:
            continue
        z = float(feature_vector(e) @ coef) + intercept
        p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
        # 模型学习的是 P(up); 对空头信号反向取"看跌可信度" (方向化命中)。
        d = _record_dir(e)
        rel = p if d > 0 else (1.0 - p if d < 0 else 0.5)
        new_conf = int(round(min(100, max(0, conf * (1.0 - w) + rel * 100 * w))))
        if new_conf != conf:
            e["conf"] = new_conf
            n_apply += 1
    return n_apply


# ── 评估工具 (无 scipy 依赖) ──

def _auc(y, prob):
    """AUC (Mann-Whitney U): P(正样本得分 > 负样本得分), 平分同分对。"""
    y = np.asarray(y, dtype=float)
    prob = np.asarray(prob, dtype=float)
    pos = np.where(y == 1)[0]
    neg = np.where(y == 0)[0]
    if pos.size == 0 or neg.size == 0:
        return None
    correct = 0.0
    for p in pos:
        hi = (prob[p] > prob[neg]).sum()
        eq = (prob[p] == prob[neg]).sum()
        correct += int(hi) + 0.5 * int(eq)
    return float(correct / (pos.size * neg.size))


def _rankdata(x):
    x = np.asarray(x, dtype=float)
    n = x.size
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(n, dtype=float)
    ranks[order] = np.arange(1, n + 1)
    i = 0
    while i < n:
        j = i + 1
        while j < n and x[order[j]] == x[order[i]]:
            j += 1
        mean_rank = (i + 1 + j) / 2.0
        ranks[order[i:j]] = mean_rank
        i = j
    return ranks


def _spearman(a, b):
    ra = _rankdata(a)
    rb = _rankdata(b)
    if ra.std() < 1e-12 or rb.std() < 1e-12:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])


# ── 自动重训 / 定时任务 (与 accuracy.py / pnf_accuracy.py 同一套模式) ──

def run_auto_model_retrain():
    """无头重训入口: 供 accuracy --eval 链式调用 / cron / CLI。
    返回模型状态 dict; 无新标签时 train_model 本身就是全量重训, 幂等安全。"""
    return train_model()


def _sched_command():
    """生成供 cron / Windows 计划任务执行的重训命令。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --model-train'
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return f'cd "{proj}" && "{sys.executable}" -m wyckoff.online_model --train --quiet'


def install_cron(hour=None, minute=11):
    """Linux: 在 crontab 安装/移除每日重训任务 (默认每日 15:11, 紧随 15:01 评估)。
    hour=None 时移除。"""
    import subprocess
    try:
        cur = subprocess.check_output(["crontab", "-l"], stderr=subprocess.STDOUT,
                                      text=True)
    except subprocess.CalledProcessError:
        cur = ""
    lines = [l for l in cur.splitlines() if "wyckoff.online_model" not in l]
    if hour is not None:
        hour = max(0, min(23, int(hour)))
        minute = max(0, min(59, int(minute)))
        lines.append(f"{minute} {hour} * * * {_sched_command()} >> /dev/null 2>&1")
    new = "\n".join(lines).strip() + "\n"
    subprocess.run(["crontab", "-"], input=new, text=True, check=True)
    return hour is not None


def install_task(hour="15:11", remove=False):
    """Windows: 创建/移除"威科夫模型重训"计划任务。"""
    import subprocess
    if os.name != "nt":
        print("install_task 仅支持 Windows; Linux 请用 --install-cron")
        return
    if remove:
        subprocess.run(["schtasks", "/Delete", "/TN", "WyckoffModelTrain", "/F"])
        return
    from .paths import DATA_DIR
    bat = os.path.join(DATA_DIR, "wx_model_train_daily.bat")
    with open(bat, "w", encoding="utf-8") as f:
        f.write(f"@echo off\n{_sched_command()}\n")
    subprocess.run(["schtasks", "/Create", "/TN", "WyckoffModelTrain",
                    "/SC", "DAILY", "/ST", hour, "/TR", bat, "/F"], check=True)


if __name__ == "__main__":
    import sys as _sys
    _quiet = "--quiet" in _sys.argv
    if "--train" in _sys.argv or "--model-train" in _sys.argv:
        st = run_auto_model_retrain()
        if not _quiet:
            print(json.dumps(model_status(), ensure_ascii=False, indent=2))
            print(f"\n重训完成: 标签 {st.get('n_labels', 0)} 条 "
                  f"(训练 {st.get('n_train', 0)} / 样本外 {st.get('n_oos', 0)}), "
                  f"AUC={st.get('auc_oos')}, 接管conf={'是' if st.get('ready') else '否'}")
    elif "--status" in _sys.argv:
        print(json.dumps(model_status(), ensure_ascii=False, indent=2))
    elif "--install-cron" in _sys.argv:
        i = _sys.argv.index("--install-cron")
        arg = _sys.argv[i + 1] if len(_sys.argv) > i + 1 and ":" in _sys.argv[i + 1] \
            else "15:11"
        hh, mm = (int(v) for v in arg.split(":", 1))
        install_cron(hh, mm)
        print(f"已安装每日 {hh:02d}:{mm:02d} 的模型自动重训任务 (紧随 15:01 数据评估)")
    elif "--uninstall-cron" in _sys.argv:
        install_cron(None)
        print("已移除模型自动重训任务")
    elif "--install-task" in _sys.argv:
        i = _sys.argv.index("--install-task")
        hour = _sys.argv[i + 1] if len(_sys.argv) > i + 1 else "15:11"
        install_task(hour)
        print(f"已安装每日 {hour} 的模型重训计划任务")
    elif "--uninstall-task" in _sys.argv:
        install_task(remove=True)
        print("已移除模型重训计划任务")
    else:
        print("用法: python -m wyckoff.online_model --train [--quiet] / "
              "--status / --install-cron [HH:MM] / --uninstall-cron / "
              "--install-task [HH:MM] / --uninstall-task")
