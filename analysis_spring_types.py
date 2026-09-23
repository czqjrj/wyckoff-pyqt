# -*- coding: utf-8 -*-
"""Spring-only 策略跟踪·按事件类型拆解 (干净口径: 只看 paper_discipline_bull)。"""
import json, statistics, collections, sys
sys.stdout.reconfigure(encoding='utf-8')

from wyckoff.paper_strategy_accuracy import (
    load_signals, STRATEGY_CN, STRATEGY_ORDER,
)

recs = load_signals()
recs = [r for r in recs if r.get('strategy') == 'paper_discipline_bull']
print('Spring-only signals:', len(recs))
print('by status:', dict(collections.Counter(r.get('status') for r in recs)))
print('by date:', dict(sorted(collections.Counter(r['date'][:10] for r in recs).items())))

def rets(h):
    out = collections.defaultdict(list)
    for r in recs:
        rr = (r.get('results') or {}).get(str(h)) or {}
        if rr.get('ret') is None:
            continue
        out[r.get('event_type', '?')].append(rr['ret'])
    return out

print()
for h in (5, 10, 20):
    by = rets(h)
    print(f'== H{h} ==')
    for ev in sorted(by):
        rs = by[ev]
        hit = sum(1 for x in rs if x > 0) / len(rs)
        avg = statistics.mean(rs)
        print(f'  {ev[:14]:14s} n={len(rs):3d} hit={hit*100:4.0f}% avg={avg*100:+.2f}%')
    alls = [x for sub in by.values() for x in sub]
    if alls:
        hit = sum(1 for x in alls if x > 0) / len(alls)
        print(f'  --合计 n={len(alls)} hit={hit*100:.0f}% avg={statistics.mean(alls)*100:+.2f}%')
