"""板块强度历史回填 (chain.backfill_board_strength_series) 专项测试。

网络与磁盘全部打桩: 合成板块日线验证周频快照生成 / 截面分位 / 合并幂等 /
fail-soft, 以及回填后 strength_at 能取到历史分位 (板块门禁不再空转)。
"""
import json

import pytest

from wyckoff import chain

TRADE_DAYS = [f"2024-01-{d:02d}" for d in (2, 3, 4, 5, 8, 9, 10, 11, 12,
                                           15, 16, 17, 18, 19)]
N_BOARDS = 35


def _mk_rows(seed, gain=0.0, flat=False):
    """合成板块日线: 每日 (day, open, close, volume)。

    强/弱板块单调涨/跌 (净流入占比饱和 ±1); 其余板块交替 ±0.1% 往复
    (去零分母、|占比|≈0.2 不与 ±1 并列), 保证最强=1.0 最弱=0.0 唯一。
    """
    rng = __import__("random").Random(seed)
    price = 50.0 + rng.uniform(0, 20)
    rows = []
    for i, d in enumerate(TRADE_DAYS):
        if flat:
            sign = 1 if i % 2 == 0 else -1
            close = price * (1 + sign * 0.001)
        else:
            close = price * (1 + gain)
        vol = 10000.0 + rng.random() * 1000
        rows.append((d, round(price, 2), round(close, 2), vol))
        price = close
    return rows


def _board_rows(name):
    if name == "强A":
        return _mk_rows(1, gain=0.02)
    if name == "弱A":
        return _mk_rows(2, gain=-0.02)
    return _mk_rows(100 + hash(name) % 1000, flat=True)


@pytest.fixture
def backfill_env(monkeypatch, tmp_path):
    """打桩网络/磁盘, 把所有输出重定向到 tmp 目录。"""
    captured = {"cache": {}, "fetches": 0}

    def fake_fetch(code, beg):
        captured["fetches"] += 1
        if code == "BK0000":
            return None
        return _board_rows(captured["code2name"][code])

    monkeypatch.setattr(chain, "_fetch_board_daily", fake_fetch)
    monkeypatch.setattr(chain, "BOARD_SNAP_FILE",
                        str(tmp_path / "wx_board_snap.json"))
    monkeypatch.setattr(chain, "_BACKFILL_CACHE_FILE",
                        str(tmp_path / "klines.json"))
    monkeypatch.setattr(
        chain, "_load_kline_cache",
        lambda: dict(captured["cache"]))
    monkeypatch.setattr(
        chain, "_save_kline_cache",
        lambda c: captured.__setitem__("cache", c))
    bmap = [("强A", "BK01"), ("弱A", "BK02")] + \
           [(f"平{i}", f"BK{30 + i}") for i in range(N_BOARDS - 2)]
    bmap.append(("坏板", "BK0000"))
    captured["code2name"] = {c: n for n, c in bmap}
    # _load_board_map 在函数体内 from .fundamental import, 打 fundamental 符号
    import wyckoff.fundamental as fu
    monkeypatch.setattr(fu, "_load_board_map", lambda: dict(bmap))
    captured["bmap"] = dict(bmap)
    captured["tmp"] = tmp_path
    return captured


def _file(env):
    p = env["tmp"] / "wx_board_snap.json"
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def test_backfill_writes_weekly_snapshot_and_strength_at(backfill_env):
    stats = chain.backfill_board_strength_series(start="2023-12-25")
    assert stats["ok"] is True
    assert stats["boards_fetched"] == N_BOARDS
    assert stats["boards_failed"] == 1                  # 坏板 fail-soft 跳过
    assert stats["weeks_written"] == 2                  # 仅收益(2024-01-08/15)两周

    snaps = _file(backfill_env)
    assert len(snaps) == 2
    assert set(snaps[0]) == {"ts", "strengths"}
    week2 = snaps[-1]
    # 最强板块 = 1.0, 最弱板块 = 0.0 (与实盘 1=最强 同语义)
    assert week2["strengths"]["强A"] == 1.0
    assert week2["strengths"]["弱A"] == 0.0
    assert week2["strengths"]["平0"] > 0.0

    # 回填后 strength_at 能取到历史分位 (门禁不再 fail-open 空转)
    pct = chain.strength_at("强A", ts="2024-01-18")
    assert pct == 1.0
    pct_weak = chain.strength_at("弱A", ts="2024-01-18")
    assert pct_weak == 0.0


def test_backfill_fail_open_when_no_data(backfill_env, monkeypatch):
    """网关断流 (全部板块抓取失败) 时返回 ok=False, 不写盘。"""
    monkeypatch.setattr(chain, "_fetch_board_daily", lambda code, beg: None)
    stats = chain.backfill_board_strength_series(start="2023-12-25")
    assert stats["ok"] is False
    assert "失败" in stats["error"]
    assert not (backfill_env["tmp"] / "wx_board_snap.json").exists()


def test_backfill_merge_idempotent(backfill_env):
    first_run_fetches = backfill_env["fetches"]
    first = chain.backfill_board_strength_series(start="2023-12-25")
    n1 = len(_file(backfill_env))
    assert first["boards_fetched"] == N_BOARDS      # 坏板被 fail-soft 排除
    assert backfill_env["fetches"] == first_run_fetches + N_BOARDS + 1

    second = chain.backfill_board_strength_series(start="2023-12-25")
    n2 = len(_file(backfill_env))
    assert first["snapshots_total"] == n1 == n2 == second["snapshots_total"]
    # 第二次全走缓存只有坏板重试一次
    assert backfill_env["fetches"] == first_run_fetches + N_BOARDS + 2


def test_backfill_write_false_does_not_persist(backfill_env):
    chain.backfill_board_strength_series(start="2023-12-25", write=False)
    assert not (backfill_env["tmp"] / "wx_board_snap.json").exists()


def test_backfill_merge_existing_snapshot_same_ts(backfill_env):
    """与既有实盘快照同 ts 不重复; 不同 ts 追加。"""
    p = backfill_env["tmp"] / "wx_board_snap.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump([{"ts": 1704240000, "strengths": {"强A": 0.9}}], f)  # 2024-01-03 附近
    chain.backfill_board_strength_series(start="2023-12-25")
    snaps = _file(backfill_env)
    assert len(snaps) == 3                       # 既有 1 + 回填 2
    # 既有快照保留且未被回填覆盖
    assert any(s["ts"] == 1704240000 and s["strengths"]["强A"] == 0.9
               for s in snaps)


def test_backfill_min_boards_guard(backfill_env):
    """有效板块数不足 _BACKFILL_MIN_BOARDS 的周不生成快照。"""
    old = chain._BACKFILL_MIN_BOARDS
    chain._BACKFILL_MIN_BOARDS = 1000
    try:
        stats = chain.backfill_board_strength_series(start="2023-12-25")
    finally:
        chain._BACKFILL_MIN_BOARDS = old
    assert stats["ok"] is False
    assert "无有效周快照" in stats["error"]
