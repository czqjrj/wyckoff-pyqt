"""产业链图谱: 上中下游映射 / 板块强度百分位 / 传导状态 (四击法第3击)。

- CHAINS 静态配置: 节点名与东财行业板块名严格对齐 (_load_board_map 同源,
  496个板块), 板块改名/新增链条只需改本表。
- 板块强度百分位: push2 clist 分页扫全部行业板块 (按当日主力净流入 f62
  降序即排名), 内存缓存30分钟; akshare THS 兜底; 全失败 → {} (fail-soft)。
- chain_snapshot(): UI 链路图与结论共用的快照 (节点强度+传导判定+个股定位)。
- apply_sector_strength(): 填充 context 预留的 sec_pct 特征 (仅近期事件,
  当前快照对刚发生的信号因果成立, 历史事件填充会前视泄漏 → 保持缺省)。
- snapshot_board_strength(): 把当日全板块强度写成周期性快照 (wx_board_snap.json),
  供回填层用 strength_at() 按信号日期就近取当时真实强度 (P4 无偏采样)。
"""
import json
import os
import time
from threading import Lock

import pandas as pd

from ._shared import atomic_write_json
from .fundamental import _get
from .paths import DATA_DIR

BOARD_SNAP_FILE = os.path.join(DATA_DIR, "wx_board_snap.json")

# 兼容包装: 当前核心 fundamental._get 接收完整 URL (而非相对 path)。
# 旧开发线的 em_get 只传 "/api/..." 相对路径, 这里补全东财 push2 主机。
_EM_BASE = "https://push2.eastmoney.com"


def _em_get(path, params, headers, timeout=4, retries=1):
    return _get(_EM_BASE + path, params, headers,
                timeout=timeout, retries=retries)


# ── 产业链静态映射 (tier: upstream 上游 / midstream 中游 / downstream 下游) ──
CHAINS = [
    {"name": "锂电·新能源车",
     "upstream": ["能源金属", "锂", "钴", "镍"],
     "midstream": ["电池", "电池化学品", "锂电专用设备"],
     "downstream": ["乘用车", "电动乘用车", "汽车零部件",
                    "汽车电子电气系统"]},
    {"name": "光伏·电网",
     "upstream": ["硅料硅片", "光伏主材"],
     "midstream": ["光伏电池组件", "光伏加工设备", "逆变器", "光伏辅材"],
     "downstream": ["光伏发电", "电网设备", "电力"]},
    {"name": "半导体",
     "upstream": ["半导体材料", "电子化学品Ⅱ"],
     "midstream": ["半导体设备", "数字芯片设计", "模拟芯片设计",
                   "集成电路制造", "集成电路封测"],
     "downstream": ["消费电子", "消费电子零部件及组装", "通信设备"]},
    {"name": "钢铁·机械",
     "upstream": ["铁矿石", "焦煤", "动力煤"],
     "midstream": ["冶钢原料", "普钢", "特钢Ⅱ"],
     "downstream": ["工程机械", "轨交设备Ⅱ", "汽车零部件"]},
    {"name": "地产·家居",
     "upstream": ["水泥", "玻璃玻纤", "装修建材"],
     "midstream": ["房屋建设Ⅱ", "房地产开发"],
     "downstream": ["家居用品", "定制家居", "白色家电", "装修装饰Ⅱ"]},
    {"name": "医药",
     "upstream": ["化学原料", "中药Ⅱ"],
     "midstream": ["化学制剂", "原料药", "生物制品", "疫苗", "医疗器械"],
     "downstream": ["医药商业", "线下药店", "医疗服务"]},
    {"name": "白酒·食品",
     "upstream": ["粮食种植", "包装印刷"],
     "midstream": ["白酒Ⅱ", "食品加工", "调味发酵品Ⅱ"],
     "downstream": ["零食", "超市", "百货"]},
]
_TIERS = ("upstream", "midstream", "downstream")

# 板块名 → [(链序号, tier)] 定位索引 (模块加载时构建)
_LOCATE = {}
for _ci, _c in enumerate(CHAINS):
    for _t in _TIERS:
        for _n in _c[_t]:
            _LOCATE.setdefault(_n, []).append((_ci, _t))

_STR_CACHE = {}
_STR_TTL = 1800  # 强度百分位 30 分钟 (盘中缓变)
_LOCK = Lock()


def board_strength():
    """全行业板块强度百分位 {板块名: 0.0~1.0} (1=最强)。

    主源: push2 clist 分页扫 fs=m:90+t:2, fid=f62 降序返回即排名
    (当日主力净流入); 兜底: akshare THS 行业汇总 (已按净流入降序)。
    排名分位 = 1 - i/(n-1)。失败返回 {}。"""
    now = time.time()
    with _LOCK:
        c = _STR_CACHE.get("__all__")
        if c and now - c[0] < _STR_TTL:
            return c[1]
    names = []
    for pn in range(1, 7):
        from .fundamental import _EM_UT, _clist_get
        r = _clist_get({"pn": str(pn), "pz": "100", "po": "1", "np": "1",
                        "fltt": "2", "invt": "2", "ut": _EM_UT, "fid": "f62",
                        "fs": "m:90+t:2", "fields": "f12,f14,f3,f62"})
        if r is None:
            break
        try:
            diff = ((r.json().get("data") or {}).get("diff")) or []
        except (ValueError, KeyError):
            break
        if not diff:
            break
        for d in diff:
            nm = str(d.get("f14") or "").strip()
            if nm:
                names.append(nm)
        if len(diff) < 100:
            break
    out = _rank_to_pct(names)
    if not out:
        out = _rank_ths()
    with _LOCK:
        _STR_CACHE["__all__"] = (now, out)
    return out


def _rank_ths():
    """akshare THS 兜底: 复用 fundamental 的同花顺行业统计 (净流入降序)。"""
    try:
        from .fundamental import _fetch_board_stats_ths
        stats = [s for s in (_fetch_board_stats_ths() or [])
                 if s.get("live")]
        return _rank_to_pct([s["name"] for s in stats])
    except Exception:
        return {}


def _rank_to_pct(names_desc):
    """降序名单 → {name: 百分位}; 空列表/重名取最高位。"""
    names_desc = [n for n in names_desc if n]
    n = len(names_desc)
    if n < 30:  # 板块数过少视为抓取不完整, 不给排名 (避免全0假数据)
        return {}
    denom = max(1, n - 1)
    out = {}
    for i, nm in enumerate(names_desc):
        p = round(1.0 - i / denom, 4)
        if nm not in out or p > out[nm]:
            out[nm] = p
    return out


def sector_strength_pct(name):
    """板块名 → 强度百分位 [0,1]; 数据不可用/不在板块表 → None。"""
    if not name:
        return None
    return board_strength().get(str(name).strip())


# ── P4: 板块强度周期快照 (历史无偏采样) ──
# 当前 sec_pct 只有"今天"的强度, 对历史事件填充会前视泄漏, 故 context.enrich
# 里恒缺省。补法: 每次 live 分析 (apply_sector_strength) 或周期任务把当日
# 全板块强度记为时间序列, 回填层按信号日期就近取当时强度 —— 因果成立。
_SNAP_MAX = 520      # 约 2 年 (每周一条)
_SNAP_MIN_GAP = 3600  # 同一天内不重复写文件
_SNAP_LAST = {"ts": 0.0}


def _load_snaps():
    try:
        with open(BOARD_SNAP_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_snaps(snaps):
    try:
        os.makedirs(os.path.dirname(BOARD_SNAP_FILE), exist_ok=True)
        atomic_write_json(BOARD_SNAP_FILE, snaps[-_SNAP_MAX:])
    except Exception:
        pass


def snapshot_board_strength(min_interval=_SNAP_MIN_GAP):
    """把当日全行业板块强度百分位追加进周期快照 (节流, fail-soft)。

    返回 {"boards": 写入板块数, "ts": 快照时间戳, "saved": 是否落盘}。
    网络不可用 (如 WYCKOFF_NO_NET=1) 时强度为 {} → 不写快照, saved=False。
    """
    now = time.time()
    if now - _SNAP_LAST["ts"] < min_interval:
        return {"boards": 0, "ts": None, "saved": False, "throttled": True}
    strengths = board_strength()
    if not strengths:
        return {"boards": 0, "ts": None, "saved": False, "throttled": False}
    snaps = [s for s in _load_snaps()
             if s.get("ts", 0) < now - min_interval]
    snaps.append({"ts": int(now), "strengths": strengths})
    _save_snaps(snaps)
    _SNAP_LAST["ts"] = now
    return {"boards": len(strengths), "ts": int(now), "saved": True,
            "throttled": False}


def strength_at(board_name, ts, max_gap_days=45):
    """信号日期 ts 时该板块的强度百分位 (取之前最近快照), 无则 None。

    max_gap_days: 快照距今过远 (快照断档) 视为不可用, 回退缺省而非陈旧数据。
    """
    import numbers
    if not board_name or not ts:
        return None
    if isinstance(ts, numbers.Real):
        # 数字输入按 epoch 秒处理 (时间戳 >秒量级视为纳秒, 折算回秒)
        t = float(ts)
        if abs(t) >= 1e12:
            t = t / 1e9
    else:
        try:
            t = pd.Timestamp(ts).timestamp()
        except Exception:
            return None
    best = None
    for s in _load_snaps():
        st = s.get("ts", 0)
        if st <= t and (best is None or st > best[0]):
            best = (st, s.get("strengths") or {})
    if best is None:
        return None
    if (t - best[0]) > max_gap_days * 86400:
        return None
    v = best[1].get(str(board_name).strip())
    return None if v is None else float(v)


def install_snapshot_cron(hour=None, minute=30):
    """Linux: 安装/移除每日板块强度快照任务 (默认 08:30 盘前)。

    hour=None 时移除。快照写入自动节流 (1 小时/条), 每日执行只是兜底
    保证频度; 快照供回填层按日期取当时 sec_pct (P4 无偏采样)。"""
    def _cmd():
        import sys as _sys
        if getattr(_sys, "frozen", False):
            return f'"{_sys.executable}" --board-snapshot'
        import os as _os
        proj = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        return f'cd "{proj}" && "{_sys.executable}" -m wyckoff.chain --snapshot'

    try:
        from ._shared import install_schedule
    except ImportError:
        return False
    try:
        return install_schedule(
            "WyckoffBoardSnap", _cmd(),
            time_str=f"{hour if hour is not None else 8:02d}:{minute:02d}",
            remove=hour is None,
            cron_markers=("wyckoff.chain",))
    except Exception:
        return False


def main(argv=None):
    import sys as _sys
    if "--snapshot" in (_sys.argv if argv is None else argv):
        r = snapshot_board_strength()
        if r["saved"]:
            print(f"板块强度快照已写: {r['boards']} 个板块 @ {r['ts']}")
        elif r["throttled"]:
            print("快照写入被节流 (1 小时内已写过), 跳过")
        else:
            print("板块强度不可用 (网络/离线), 未写快照")
        return 0
    print("用法: python -m wyckoff.chain --snapshot")
    return 0


def board_bk_code(name):
    """板块名 → 东财 BK 代码 (供成份股查询); 失败返回 None。"""
    if not name:
        return None
    try:
        from .fundamental import _suggest_board
        return _suggest_board(str(name))
    except Exception:
        return None


def locate(sector_name):
    """板块名 → [(chain_name, tier), ...]; 不在图谱内返回 []。"""
    return [(CHAINS[ci]["name"], t) for ci, t in _LOCATE.get(sector_name, [])]


def chain_home(sector_name):
    """板块名 → (chain_name, tier) 或 (None, None) (不在图谱内)。"""
    loc = _LOCATE.get(sector_name)
    if not loc:
        return None, None
    ci, t = loc[0]
    return CHAINS[ci]["name"], t


def _beneficiary_tiers(trans):
    """给定的链条传导方向 → 受益(补涨/兑现)环节集合。

    - 上游→下游 (成本推动/资源景气): 中下游为兑现受益环节, 上游已领涨;
    - 下游→上游 (需求拉动): 中上游为兑现受益环节, 下游已领涨。
    """
    if trans == "上游→下游":
        return {"midstream", "downstream"}
    if trans == "下游→上游":
        return {"upstream", "midstream"}
    return set()


def _trans_strength(chain, tier):
    """个股所处环节在当前传导中是否受益 (回落集判断)。"""
    return tier in _beneficiary_tiers(chain.get("trans") or "")


def chain_factor_for(sector_name, ts=None):
    """计算个股所在产业链的复合因子 (含传导方向), 用于交易加分/门禁。

    返回 dict 或 None (个股不在图谱 / 板块强度不可用):
      {pct, tier, trans, score, tone}
        pct     板块强度百分位 [0,1] (ts 给定用历史快照 strength_at, 否则当日)
        tier    个股所处环节 upstream/midstream/downstream
        trans   所在链当前传导方向 "上游→下游"/"下游→上游"/""
        score   复合分数 [-1, 1]
                = trans(±1 受益/逆传导) × pct 强度加权: 受益环节(含中游)取 +当下游
                  驱动或 +当地上游驱动; 其余环节取 -0.5~+0.5 的强度中性。
        tone    bullish/bearish/neutral

    无前视: ts 给定时按该日期之前的最近强度快照取 (strength_at), 否则当日 live。
    数据不全时返回 None 而非硬编码 0, 由调用方决定是否回退到无因子状态。
    """
    if not sector_name:
        return None
    cn, tier = chain_home(sector_name)
    if cn is None:
        return None
    if ts is not None:
        pct = strength_at(sector_name, ts)
    else:
        pct = sector_strength_pct(sector_name)
    if pct is None:
        return None
    try:
        snap = chain_snapshot(sector_name)
        cur = next((s for s in snap if s["name"] == cn), None)
        trans = cur["trans"] if cur else ""
        if cur is not None:
            a = cur["avg"]
            up = a.get("upstream")
            mid = a.get("midstream")
            dn = a.get("downstream")
            if trans == "上游→下游" and None not in (up, mid, dn):
                grad = up - dn
            elif trans == "下游→上游" and None not in (up, mid, dn):
                grad = dn - up
            else:
                grad = 0.0
        else:
            trans, grad = "", 0.0
    except Exception:
        return {"pct": pct, "tier": tier, "trans": "",
                "score": _neutral_score(pct), "tone": _tone_neutral(pct)}

    if trans:
        if _trans_strength({"trans": trans}, tier):
            # 受益环节 + 强度梯度 + 自身强度 → 强正向
            score = 0.5 * pct + 0.5 * min(1.0, grad)
            tone = "bullish"
        else:
            # 逆传导环节 (已领涨/被挤压): 即便板块强也打折扣
            score = 0.25 * pct - 0.25
            tone = "bearish"
    else:
        score = _neutral_score(pct)
        tone = _tone_neutral(pct)
    return {"pct": pct, "tier": tier, "trans": trans,
            "score": round(float(score), 4), "tone": tone}


def _neutral_score(pct):
    return float(pct) * 2.0 - 1.0   # 强度映射到 [-1, 1]


def _tone_neutral(pct):
    if pct >= 0.8:
        return "bullish"
    if pct <= 0.2:
        return "bearish"
    return "neutral"


def chain_conf_adjust(sector_name, conf, ts=None):
    """A: 把产业链因子换算成 conf 加分 (整数, 可选负), 用于选股/排序门禁。

    返回加分整数; 链条数据不可用或个股不在图谱时返回 0 (不改变现有行为)。
    强链受益环节加分, 逆传导/弱链减分。ts 给定时用历史快照 (回测无前视),
    否则用当日 live 强度。
    """
    try:
        cf = chain_factor_for(sector_name, ts=ts)
    except Exception:
        return 0
    if cf is None:
        return 0
    score = cf["score"]
    adj = int(round(score * 4))   # score∈[-1,1] → adj∈[-4,4]
    conf = int(conf)
    pull = max(6, int(conf * 0.08))   # 加分上限约为 conf 的 8%, 不低于 6 分
    return max(-pull, min(pull, adj))


def chain_cap_key(sector_name):
    """C: 组合同链限仓用分组键 (链条名或 None)。"""
    cn, _tier = chain_home(sector_name)
    return cn


def _tier_avg(nodes):
    vals = [nd["pct"] for nd in nodes if nd.get("pct") is not None]
    if len(vals) < max(1, len(nodes) // 2):  # 覆盖过半才有效
        return None
    return sum(vals) / len(vals)


def transmission(tiers_pcts):
    """三档平均强度的传导判定。

    上≥中≥下 且梯度≥0.15 → "上游→下游" (成本推动/资源景气);
    下≥中≥上 且梯度≥0.15 → "下游→上游" (需求拉动);
    其余 → "" (未形成有序传导)。"""
    up, mid, dn = tiers_pcts
    if None in (up, mid, dn):
        return ""
    if up >= mid >= dn and up - dn >= 0.15:
        return "上游→下游"
    if dn >= mid >= up and dn - up >= 0.15:
        return "下游→上游"
    return ""


def chain_snapshot(sector_name=None):
    """UI 链路图快照。返回链列表:

    [{"name": 链名,
      "tiers": {"upstream": [{"name","pct"}, ...], ...},
      "avg": {"upstream": float|None, ...},
      "trans": "上游→下游"|"" ,
      "highlight": [(tier, name), ...]}]

    sector_name 给定时 highlight 标出该板块所在节点; 数据不可用时 pct=None。
    """
    strength = board_strength()
    hi_names = {sector_name} if sector_name else set()
    out = []
    for c in CHAINS:
        tiers = {t: [{"name": n, "pct": strength.get(n)}
                     for n in c[t]] for t in _TIERS}
        avg = {t: _tier_avg(tiers[t]) for t in _TIERS}
        highlight = [(t, n) for t in _TIERS for n in c[t]
                     if n in hi_names]
        out.append({"name": c["name"], "tiers": tiers, "avg": avg,
                    "trans": transmission((avg["upstream"], avg["midstream"],
                                           avg["downstream"])),
                    "highlight": highlight})
    return out


def chain_evidence(sector_name):
    """产业链证据条目 [(text, tone)] 供 build_confirm_section (四击法确认)。

    只在有明确传导方向或所处环节极端强弱时输出, 无信号保持安静。"""
    if not sector_name:
        return []
    ev = []
    try:
        snaps = chain_snapshot(sector_name)
    except Exception:
        return []
    for s in snaps:
        own = s["highlight"]
        if not own:
            continue
        tier, _nm = own[0]
        trans = s["trans"]
        if trans == "上游→下游":
            tone = "bullish" if tier != "upstream" else "neutral"
            ev.append((f"产业链: [{s['name']}] 上游强势向中下游传导 · "
                       f"所处{'上游' if tier == 'upstream' else '中下游'}环节", tone))
        elif trans == "下游→上游":
            tone = "bullish" if tier != "downstream" else "neutral"
            ev.append((f"产业链: [{s['name']}] 下游景气向上游传导 · "
                       f"所处{'下游' if tier == 'downstream' else '中上游'}环节", tone))
        else:
            a = s["avg"][tier]
            if a is not None and a <= 0.25:
                ev.append((f"产业链: [{s['name']}] 所处环节强度垫底"
                           f"(后25%), 缺乏链条共振", "bearish"))
            elif a is not None and a >= 0.85:
                ev.append((f"产业链: [{s['name']}] 所处环节强度领先"
                           f"(前15%)", "bullish"))
    return ev[:2]  # 个股跨多链时最多取两条证据


def apply_sector_strength(events, sec_pct, n_total, recent=10):
    """把当前板块强度百分位写入近期事件的 feat.sec_pct (context 预留钩子)。

    仅填最近 recent 根K线内的事件 (n_total=分析窗口长度): 当前板块快照对
    "刚发生"的信号因果成立; 对更早的历史事件填充会把今天的强度泄漏进过去
    (回填路径由 backfill_ctx 显式声明 sec_pct 恒缺省)。
    sec_pct 或 n_total 缺失时不填。返回写入事件数。"""
    if sec_pct is None or not n_total or not events:
        return 0
    try:
        v = min(1.0, max(0.0, float(sec_pct)))
    except (TypeError, ValueError):
        return 0
    # P4: 顺手把当日全板块强度写成周期快照, 供历史回填按日期取当时真实值
    # (节流到同进程不超过 1 小时/条, fail-soft)。
    try:
        snapshot_board_strength()
    except Exception:
        pass
    n_total = int(n_total)
    cnt = 0
    for e in events:
        try:
            if int(e.get("idx", -1)) < n_total - recent:
                continue
        except (TypeError, ValueError):
            continue
        e.setdefault("feat", {})["sec_pct"] = v
        cnt += 1
    return cnt


def strength_history(sector: str, start_date: str = "2023-09-01", end_date: str = None) -> pd.DataFrame:
    """回填板块强度历史分位序列（按日）。

    从 wx_board_snap.json 读取历史快照，返回 DataFrame:
      columns = ['date', 'pct']  pct ∈ [0,1] (1=最强)

    若 sector 在快照中从未出现，返回空 DataFrame (非 None)，
    供调用方判定是 fail-open 还是 fail-close。

    参数:
        sector: 板块名 (如 "银行", "医药", "有色金属")
        start_date: 开始回溯日期 (默认 2023-09-01)
        end_date: 结束日期 (默认 None → 今日)
    返回: DataFrame 或空 DataFrame
    """
    from datetime import datetime
    from pathlib import Path

    import pandas as pd

    snap_path = Path(BOARD_SNAP_FILE)
    if not snap_path.exists():
        return pd.DataFrame(columns=["date", "pct"])

    try:
        with open(snap_path, encoding="utf-8") as f:
            snaps = json.load(f)
    except Exception:
        return pd.DataFrame(columns=["date", "pct"])

    if not isinstance(snaps, list):
        return pd.DataFrame(columns=["date", "pct"])

    # 统一时间戳 -> 日期字符串
    rows = []
    for s in snaps:
        if not isinstance(s, dict):
            continue
        ts_int = s.get("ts", 0)
        # 时间戳是秒级整数，转 datetime
        try:
            dt = datetime.fromtimestamp(ts_int) if ts_int > 1e9 else datetime.fromtimestamp(ts_int / 1e9)
        except Exception:
            continue
        d_str = dt.strftime("%Y-%m-%d")
        # 只保留在目标区间内的快照
        if start_date and d_str < start_date:
            continue
        if end_date:
            try:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                if dt > end_dt:
                    continue
            except Exception:
                pass
        # 抽取该板块的强度
        strengths = s.get("strengths") or {}
        pct = strengths.get(sector)
        if pct is not None:
            try:
                pct = float(pct)
            except Exception:
                pct = None
        else:
            pct = None
        rows.append({"date": d_str, "pct": pct})

    if not rows:
        return pd.DataFrame(columns=["date", "pct"])

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    # 对同一日多条记录去重，取最后一次 (最新快照)
    df = df.drop_duplicates(subset="date", keep="last")
    return df
