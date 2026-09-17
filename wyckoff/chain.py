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
- backfill_board_strength_series(): 用板块历史日线(东财)回填历史周频快照,
  使 strength_at() 在回测区间内也取得到可信分位 (板块门禁免于空转)。
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


# ── P4.2: 历史回填 (板块强度无偏采样补库) ──
# 板块门禁在历史回测(2023-06~2026-08)中近乎空转, 因 wx_board_snap.json 仅自
# 2026-08 起有快照; strength_at 对更早信号取不到分位 → fail-open。本节回填:
#   数据源: 东财 push2his 板块日线 (secid=90.BKxxxx, 历史日线 OHLCV, 板块映射
#           _load_board_map 与实盘抓取名同源, f127二级行业可精确命中)。
#   强度代理: 板块级近 _BACKFILL_WIN 根量价净流入占比 Σ(C-O)*V / Σ|C-O|*V
#             (与回测个股资金流门禁同构), 逐日对全部板块求截面分位 —— 即实盘
#             "当日主力净流入排名"的因果代理, 零前视。
#   落盘: 每周最后一个交易日 1 条快照 {ts, strengths}, 与实盘快照按 ts 合并。
#   抓取策略: 东财对高频连续请求会 IP 级临时限流 (RemoteDisconnected), 故按
#   节流串行抓取 + 原始日线磁盘缓存 (中断可续), 失败板 fail-soft 跳过。
_BACKFILL_WIN = 5          # 净流入代理回看窗口 (根)
_BACKFILL_MIN_BOARDS = 30  # 当日有效板块数低于此不生成该周快照 (同 _rank_to_pct)
_BACKFILL_THROTTLE = 0.6   # 相邻板请求最小间隔 (秒); 东财对持续 >4req/s 会间歇断连
_BACKFILL_FAIL_GAP = 3.0   # 单板失败后额外退避 (秒), 降低被断连概率
_BACKFILL_MAX_TRY = 3      # 单板重试次数
_BACKFILL_CACHE_FILE = os.path.join(DATA_DIR, "wyckoff_board_klines.json")

_BF_LAST_REQ = {"ts": 0.0}


def _fetch_board_daily(board_code, beg):
    """板块BK码 → 历史日线 [(day, open, close, volume), ...] 或 None (失败)。

    节流: 相邻请求至少 _BACKFILL_THROTTLE 秒; 失败退避 _BACKFILL_FAIL_GAP 后
    重试 (最多 _BACKFILL_MAX_TRY 次)。cache_fail=False: 不写东财负缓存, 限流
    恢复后不会被残留的失败记录短路。
    """
    for attempt in range(_BACKFILL_MAX_TRY):
        with _LOCK:
            wait = _BF_LAST_REQ["ts"] - (time.time() - _BACKFILL_THROTTLE)
            if wait > 0:
                time.sleep(wait)
            _BF_LAST_REQ["ts"] = time.time()
        r = _get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                 {"secid": f"90.{board_code}", "klt": "101", "fqt": "1",
                  "beg": beg, "end": "20500101", "lmt": "100000",
                  "fields1": "f1,f2,f3,f4,f5,f6",
                  "fields2": "f51,f52,f53,f54,f55,f56"},
                 {"User-Agent": "Mozilla/5.0",
                  "Referer": "https://quote.eastmoney.com/"},
                 retries=1, cache_fail=False)
        rows = []
        if r is not None:
            try:
                kl = ((r.json().get("data") or {}).get("klines")) or []
            except (ValueError, KeyError):
                kl = []
            for line in kl:
                f = line.split(",")
                if len(f) < 6:
                    continue
                rows.append((f[0], float(f[1]), float(f[2]), float(f[5])))
        if rows:
            return rows
        if attempt < _BACKFILL_MAX_TRY - 1:
            time.sleep(_BACKFILL_FAIL_GAP + attempt * 2)
    return None


def _load_kline_cache():
    try:
        with open(_BACKFILL_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_kline_cache(cache):
    try:
        os.makedirs(os.path.dirname(_BACKFILL_CACHE_FILE), exist_ok=True)
        atomic_write_json(_BACKFILL_CACHE_FILE, cache)
    except Exception:
        pass


def backfill_board_strength_series(start="2020-01-01", max_boards=None,
                                   progress_cb=None, write=True):
    """回填 wx_board_snap.json 的历史板块强度周频快照 (与实盘快照合并)。

    - 逐板块拉东财历史日线 (beg 留 30 天热身后延), 按 _BACKFILL_WIN 根量价净
      流入占比代理强度, 逐日求跨板块截面分位 (1=最强, 语义同实盘净流入排名);
    - 每周取最后一个交易日落 1 条 {ts, strengths}, 与现有快照按 ts 合并去重,
      经 _SNAP_MAX 保留最近窗后原子写回 BOARD_SNAP_FILE (write=False 时只计算);
    - 原始日线缓存在 _BACKFILL_CACHE_FILE, 中断后重跑只补抓失败板 (幂等);
    - 东财限流/板块失败按 fail-soft 跳过, 计数在返回 stats 暴露。

    返回 dict: ok / boards_fetched / boards_failed / weeks_written /
    snapshots_total / first_ts / last_ts / error。
    """
    from .fundamental import _load_board_map

    bmap = _load_board_map()
    if not bmap:
        return {"ok": False, "error": "板块映射表为空"}
    items = sorted(bmap.items())
    if max_boards:
        items = items[:max_boards]

    beg = (pd.Timestamp(start) - pd.Timedelta(days=30)).strftime("%Y%m%d")
    cache = _load_kline_cache()
    fresh_key = f"raw|{beg}"
    cached_ok = cache.get("_meta", {}).get("beg") == beg

    fetched = failed = 0
    per_board = {}
    n_total = len(items)
    last_saved = 0
    try:
        for i, (name, code) in enumerate(items):
            rec = cache.get(code) if cached_ok else None
            if rec is None:
                rec = _fetch_board_daily(code, beg)
                if rec is not None:
                    cache[code] = rec
                    fetched += 1
                else:
                    failed += 1
            else:
                fetched += 1  # 命中磁盘缓存, 免重抓
            if rec is not None:
                per_board[name] = rec
            # 增量落盘: 被杀进程/断网中断也能从缓存续跑
            if fetched - last_saved >= 20:
                _save_kline_cache(cache)
                last_saved = fetched
                if progress_cb:
                    progress_cb(i + 1, n_total, fetched)
    except BaseException:
        _save_kline_cache(cache)  # 中断兜底, 保留已抓部分
        raise
    if cache.get("_meta", {}).get("beg") != beg:
        cache["_meta"] = {"beg": beg, "fetched_at": int(time.time())}
    _save_kline_cache(cache)
    if not per_board:
        return {"ok": False, "error": "板块日线全部拉取失败",
                "boards_fetched": 0, "boards_failed": failed,
                "weeks_written": 0}

    ratios = []
    for name, rows in per_board.items():
        df = pd.DataFrame(rows, columns=["day", "open", "close", "volume"])
        df["day"] = pd.to_datetime(df["day"])
        df = df.set_index("day").sort_index()
        mv = (df["close"] - df["open"]) * df["volume"]
        av = (df["close"] - df["open"]).abs() * df["volume"]
        num = mv.rolling(_BACKFILL_WIN, min_periods=_BACKFILL_WIN).sum()
        den = av.rolling(_BACKFILL_WIN, min_periods=_BACKFILL_WIN).sum()
        ratios.append((num / den.mask(den.eq(0))).clip(-1.0, 1.0).rename(name))
    wide = pd.concat(ratios, axis=1)          # 交易日 × 板块
    # 截面分位, 高=强; 与实盘 _rank_to_pct 同语义 (最强=1.0, 最弱=0.0)。
    # pandas 3.x 的 rank(pct=True) 分母是 n (最小=1/n), 故手动 (rank-1)/(cnt-1)。
    cnt = wide.notna().sum(axis=1).clip(lower=2)
    pct = (wide.rank(axis=1, method="average") - 1).div(cnt - 1, axis=0)

    snaps = []
    for wk, grp in pct.groupby(pct.index.to_period("W")):
        sub = grp.dropna(how="all")
        if not len(sub):
            continue
        row = sub.iloc[-1]                    # 该周最后一个交易日
        if int(row.notna().sum()) < _BACKFILL_MIN_BOARDS:
            continue
        strengths = {k: round(float(v), 4) for k, v in row.items()
                     if pd.notna(v)}
        snaps.append({"ts": int(pd.Timestamp(sub.index[-1]).timestamp()),
                      "strengths": strengths})
    if not snaps:
        return {"ok": False, "error": "无有效周快照生成", "boards_fetched": fetched,
                "boards_failed": failed, "weeks_written": 0}

    existing = {s["ts"]: s for s in _load_snaps()}
    for s in snaps:
        existing.setdefault(s["ts"], s)
    merged = sorted(existing.values(), key=lambda s: s["ts"])
    if write:
        os.makedirs(os.path.dirname(BOARD_SNAP_FILE), exist_ok=True)
        atomic_write_json(BOARD_SNAP_FILE, merged[-_SNAP_MAX:])

    return {"ok": True, "boards_fetched": fetched, "boards_failed": failed,
            "weeks_written": len(snaps), "snapshots_total": len(merged),
            "first_ts": snaps[0]["ts"], "last_ts": snaps[-1]["ts"]}


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



