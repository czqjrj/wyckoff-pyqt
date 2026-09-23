# -*- coding: utf-8 -*-
"""一次性: 信信号级统计 (Spring-only 与左侧买点, 按事件类型/置信度)."""
import json, statistics, collections
import wyckoff.paper_strategy_accuracy as m

def _pct(v):
    return f"{v*100:.0f}%" if v is not None else "-"

def tab(recs, label):
    byh = {5: [], 10: [], 20: []}
    byev = collections.defaultdict(lambda: {5: [], 10: [], 20: []})
    for r in recs:
        res = r.get('results') or {}
        ev = r.get('event_type') or '?'
        for h in byh:
            rh = (res.get(str(h)) or {})
            ret = rh.get('ret')
            if ret is None:
                continue
            byh[h].append(ret)
            byev[ev][h].append(ret)
    print(f'==== {label} n_signals={len(recs)} ====')
    for h in (5, 10, 20):
        rs = byh[h]
        if not rs:
            continue
        hit = sum(1 for x in rs if x > 0) / len(rs)
        print(f'  H{h} n={len(rs):3d} hit={hit*100:4.0f}% avg={statistics.mean(rs)*100:+.2f}%')
    print('  -- by event_type (H5/H10/H20: n/hit/avg) --')
    for ev in sorted(byev):
        parts = []
        for h in (5, 10, 20):
            rs = byev[ev][h]
            if not rs:
                continue
            hit = sum(1 for x in rs if x > 0) / len(rs)
            parts.append(f'H{h}:n={len(rs)}/{hit*100:.0f}%/{statistics.mean(rs)*100:+.2f}%')
        if parts:
            print(f'    {ev[:30]:30s}  ' + '  '.join(parts))

recs = m.load_signals()
key = {'paper_discipline_bull': 'Spring-only', 'long_buy_left': '左侧买点'}

for strat in sorted({r.get('strategy') for r in recs}):
    grp = [r for r in recs if r.get('strategy') == strat]
    tab(grp, f'{key.get(strat, strat)} ({strat})')
