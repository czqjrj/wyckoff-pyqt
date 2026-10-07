"""fund_history: 分层规则 / as-of 无前视 / 回填解析 / 门禁与仓位接线。

覆盖 (对应"基本面融化进项目"四接点):
  1) tier_of 单一权威分层规则 (A/B/C/D/无数据);
  2) asof/tier_at 公告日无前视 (报告期≠得知日, 按报告期取数即泄漏);
  3) backfill 行解析与合并 (含公告日缺失 → 法定披露截止日回退);
  4) 回放门禁 fund_gate_filter + _make_order 分层仓位权重;
  5) 准确率报告 tier_stats 分层表;
  6) 实盘 run_cycle: D 层硬门禁拦截 + 权重折算下单;
  7) CLI fundamental_trend 报告。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from wyckoff import fund_history as fh


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    """每个测试独立财报缓存文件 + 干净进程缓存, 结束后不污染共享数据目录。"""
    path = str(tmp_path / "wyckoff_fund_history.json")
    monkeypatch.setattr(fh, "FUND_HISTORY_FILE", path)
    fh.reset_cache()
    yield path
    fh.reset_cache()


def _write_hist(rows_by_code):
    with open(fh.FUND_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(rows_by_code, f, ensure_ascii=False)
    fh.reset_cache()
    return fh.load_history()


# ── 1. 分层规则 ──────────────────────────────────────────────
@pytest.mark.parametrize("kw,expect", [
    (dict(np_yoy=25.0, rev_yoy=5.0), "A"),
    (dict(np_yoy=15.0, rev_yoy=0.0), "A"),       # 门槛含端点
    (dict(np_yoy=5.0, rev_yoy=2.0), "B"),        # 温和增长
    (dict(np_yoy=5.0, rev_yoy=-15.0), "C"),      # 营收萎缩
    (dict(np_yoy=-5.0), "C"),                    # 轻度负增长
    (dict(np_yoy=-40.0, rev_yoy=5.0), "D"),      # 净利断崖
    (dict(eps=0.0), "D"),                        # 报告期亏损
    (dict(eps=-0.3), "D"),
    (dict(np_yoy=-10.0, pe=50.0), "D"),          # 负增长+高估值 (价值陷阱)
    (dict(np_yoy=-10.0, pe=20.0), "C"),          # 负增长+估值合理
    (dict(np_yoy=-5.0, rev_yoy=-30.0), "D"),     # 收入利润双杀
    (dict(pe=-5.0), "D"),                        # PE 亏损
    (dict(rev_yoy=8.0), "B"),                    # 只有营收数据
    (dict(), ""),                                # 全缺 fail-open
])
def test_tier_of_rules(kw, expect):
    assert fh.tier_of(**kw) == expect


def test_gate_weight_fail_open():
    assert fh.gate_ok("") and fh.gate_ok("A") and fh.gate_ok("B") and fh.gate_ok("C")
    assert not fh.gate_ok("D")
    assert fh.weight("") == 1.0
    assert fh.weight("A") == 1.0
    assert fh.weight("B") == 0.8
    assert fh.weight("C") == 0.5
    assert fh.weight("D") == 0.25


# ── 2. as-of 无前视 ──────────────────────────────────────────
def test_asof_no_lookahead(tmp_path):
    _write_hist({
        "600104": [
            {"d": "2025-12-31", "n": "2026-04-02", "rev": 4.6, "np": 300.0, "eps": 0.9},
            {"d": "2026-03-31", "n": "2026-04-30", "rev": 1.0, "np": 10.0, "eps": 0.2},
            {"d": "2026-06-30", "n": "2026-08-29", "rev": -0.3, "np": -50.0, "eps": -0.1},
        ],
    })
    # 公告日前: 看不到任何报告 (按报告期取数就会提前看到 2025-12-31 → 泄漏)
    assert fh.asof("600104", "2026-04-01") is None
    assert fh.tier_at("600104", "2026-04-01") == ""
    # 公告日当天 (含端点)
    assert fh.asof("600104", "2026-04-02")["d"] == "2025-12-31"
    assert fh.tier_at("600104", "2026-04-02") == "A"
    # 中段: 只见已公告的最近一份
    assert fh.asof("sh600104", "2026-06-01")["d"] == "2026-03-31"
    # 最新已公告: D 层 (2026H1 亏损断崖)
    assert fh.asof("600104", "2026-10-07")["d"] == "2026-06-30"
    assert fh.tier_at("600104", "2026-10-07") == "D"
    # 无该股数据 fail-open
    assert fh.tier_at("600000", "2026-10-07") == ""


def test_asof_rejects_bad_inputs():
    _write_hist({"600104": [{"d": "2026-06-30", "n": "2026-08-29", "np": 1.0}]})
    assert fh.asof("600104", None) is None
    assert fh.asof("600104", "") is None
    assert fh.asof("abc", "2026-10-07") is None
    assert fh.asof(None, "2026-10-07") is None


# ── 3. 回填解析/合并 ─────────────────────────────────────────
def test_parse_row_notice_fallback():
    code, row = fh._parse_row({
        "SECURITY_CODE": "600000", "REPORTDATE": "2025-12-31 00:00:00",
        "NOTICE_DATE": None, "SJLTZ": "12.3", "YSTZ": "—", "BASIC_EPS": "1.1",
    })
    assert code == "600000"
    assert row["d"] == "2025-12-31"
    assert row["n"] == "2026-04-30"      # 年报法定披露截止日 (不早于报告期)
    assert row["np"] == 12.3
    assert row["rev"] is None            # "—" → None 不当 0
    assert row["eps"] == 1.1
    # 半年报 / 三季报截止日
    assert fh._notice_fallback("2025-06-30") == "2025-08-31"
    assert fh._notice_fallback("2025-09-30") == "2025-10-31"
    assert fh._parse_row({"SECURITY_CODE": "x", "REPORTDATE": ""}) == (None, None)


def test_backfill_merges_and_failsoft(monkeypatch):
    _write_hist({"600000": [{"d": "2025-06-30", "n": "2025-08-14", "np": 1.0}]})
    calls = []

    def fake_fetch(period, page):
        calls.append((period, page))
        if period == "2025-09-30":
            return None, 0          # 网络失败 → 该期跳过不抛
        return ([{
            "SECURITY_CODE": "600000",
            "REPORTDATE": "2025-09-30 00:00:00",
            "NOTICE_DATE": "2025-10-25 00:00:00",
            "SJLTZ": 8.0, "YSTZ": 5.0, "BASIC_EPS": 0.5,
        }, {
            "SECURITY_CODE": "600104",
            "REPORTDATE": "2025-09-30 00:00:00",
            "NOTICE_DATE": "2025-10-30 00:00:00",
            "SJLTZ": -60.0, "YSTZ": -2.0, "BASIC_EPS": -0.05,
        }], 2)

    monkeypatch.setattr(fh, "_fetch_period", fake_fetch)
    n = fh.backfill(periods=["2025-09-30", "2025-12-31"], save=True)
    assert n == 2
    hist = fh.load_history()
    # 合并: 600000 两期共存且按公告日升序; 600104 新增
    assert [r["d"] for r in hist["600000"]] == ["2025-06-30", "2025-09-30"]
    assert hist["600104"][0]["np"] == -60.0
    # 失败期只请求第 1 页即跳过, 成功期总条数 2 < 500 也只请求第 1 页
    assert sorted(calls) == [("2025-09-30", 1), ("2025-12-31", 1)]
    # 落盘后进程缓存重建仍可读
    with open(fh.FUND_HISTORY_FILE, encoding="utf-8") as f:
        assert "600104" in json.load(f)


def test_backfill_periods_exclude_unannounced():
    ps = fh._periods(2025)
    # 已过披露截止日的报告期才回填
    assert "2025-12-31" in ps          # 年报截止次年 04-30, 已过
    assert "2026-03-31" in ps          # 一季报截止 04-30, 已过 (今日 2026-10-07)
    assert "2026-06-30" in ps          # 半年报截止 08-31, 已过
    # 未出完的报告期不抓 (按期重跑即可补)
    assert "2026-09-30" not in ps      # 三季报截止 10-31, 未到
    assert "2026-12-31" not in ps      # 2026 年报截止 2027-04-30, 未到
    # start_year 之前的报告期不含
    assert "2024-12-31" not in ps


# ── 4. 回放门禁 + 仓位权重 ───────────────────────────────────
def test_replay_fund_gate_filter(monkeypatch):
    from scripts import paper_replay_bt as pbt

    tiers = {"sh600000": "A", "sh600104": "D", "sh600002": ""}
    monkeypatch.setattr(pbt, "_fund_tier", lambda code, day: tiers.get(code, ""))
    cands = [{"code": c} for c in ("sh600000", "sh600104", "sh600002")]
    # flags 全关: 原样返回零开销 (不附 fund_tier)
    out = pbt.fund_gate_filter(cands, "2026-10-07")
    assert len(out) == 3 and "fund_tier" not in out[0]
    # 仅权重: 附层级不过滤 (D 层由权重 0.25 处理)
    out = pbt.fund_gate_filter(cands, "2026-10-07", weight=True)
    assert [c["fund_tier"] for c in out] == ["A", "D", ""]
    # 硬门禁: D 剔除, "" fail-open 放行
    out = pbt.fund_gate_filter(cands, "2026-10-07", gate=True, weight=True)
    assert [c["code"] for c in out] == ["sh600000", "sh600002"]


def test_make_order_fund_weight(monkeypatch):
    import wyckoff.paper as paper

    monkeypatch.setattr(paper, "_CUR", dict(paper._CUR))
    paper._CUR["max_pos"] = 5
    base = paper._make_order("sh600000", "", "Spring", 100, 10.0, 0, 1_000_000)
    half = paper._make_order("sh600000", "", "Spring", 100, 10.0, 0, 1_000_000,
                             fund_weight=0.5)
    quarter = paper._make_order("sh600000", "", "Spring", 100, 10.0, 0, 1_000_000,
                                fund_weight=0.25)
    assert base and half and quarter
    assert 0.45 < half["qty"] / base["qty"] < 0.55
    assert 0.20 < quarter["qty"] / base["qty"] < 0.30
    # None = 旧行为不变
    same = paper._make_order("sh600000", "", "Spring", 100, 10.0, 0, 1_000_000,
                             fund_weight=None)
    assert same["qty"] == base["qty"]


# ── 5. 准确率报告分层 ────────────────────────────────────────
def test_tier_stats_stratifies_by_asof_tier():
    _write_hist({
        "600104": [
            {"d": "2025-12-31", "n": "2026-04-02", "rev": 5.0, "np": 40.0, "eps": 1.0},
            {"d": "2026-06-30", "n": "2026-08-29", "rev": -1.0, "np": -50.0, "eps": -0.1},
        ],
    })
    from wyckoff import paper_strategy_accuracy as psa

    recs = [
        # A 层 (2026-05-01 已见 2025 年报, 2026H1 未公告) 命中
        {"code": "600104", "symbol": "sh600104", "date": "2026-05-01",
         "results": {"20": {"ret": 0.05}}},
        # D 层 (2026-09-15 已见 2026H1 亏损) 未命中
        {"code": "600104", "symbol": "sh600104", "date": "2026-09-15",
         "results": {"20": {"ret": -0.02}}},
        # 无财报 → "-" fail-open 分组
        {"code": "600000", "symbol": "sh600000", "date": "2026-09-15",
         "results": {"20": {"ret": 0.01}}},
        # 未评估 (pending) 不入表
        {"code": "600104", "date": "2026-09-20", "results": {}},
    ]
    out = psa.tier_stats(recs)
    assert out["A"]["n"] == 1 and out["A"]["hit"] == 1.0 and out["A"]["avg"] == 0.05
    assert out["D"]["n"] == 1 and out["D"]["hit"] == 0.0
    assert out["-"]["n"] == 1 and out["-"]["hit"] == 1.0
    assert out["B"]["n"] == 0 and out["C"]["n"] == 0
    assert out["A"]["ci_lo"] is not None  # Wilson 区间随表给出


def test_review_report_renders_tier_table(tmp_path):
    _write_hist({"600104": [{"d": "2026-06-30", "n": "2026-08-29",
                             "rev": -1.0, "np": -50.0, "eps": -0.1}]})
    from wyckoff import paper_strategy_accuracy as psa

    rep = {
        "_generated_at": "2026-10-07 00:00:00",
        "_summary": {"total": 1, "evaluated": 1, "small_sample": True,
                     "min_conclusion_n": 50},
        "_tiers": psa.tier_stats([
            {"code": "600104", "date": "2026-10-01",
             "results": {"20": {"ret": -0.1}}},
        ]),
    }
    md = psa._render_report(rep)
    assert "按基本面分层" in md
    assert "公告日 as-of" in md
    assert "| D |" in md


# ── 6. 实盘 run_cycle 接线 ───────────────────────────────────
def _cand(code, conf=100, price=10.0, day="2026-10-07"):
    return {"code": code, "name": "", "type": "Spring", "conf": conf,
            "entry_price": price, "last": price, "sector": "", "strategy": "",
            "day": day, "trigger": "above"}


def _prep_cycle(monkeypatch):
    import wyckoff.paper as paper

    st = paper.load_state()
    st.update({"positions": [], "pending": [], "closed": [], "conditions": [],
               "cash": 1_000_000.0, "candidates": [], "equity_hist": [],
               "scan_count": 0, "weak": False})
    monkeypatch.setattr(paper, "load_state", lambda *a, **k: st)
    monkeypatch.setattr(paper, "save_state", lambda s: None)
    monkeypatch.setattr(paper, "_weak_market_flag", lambda: False)
    monkeypatch.setattr(paper, "_backfill_position_protection", lambda s: None)
    monkeypatch.setattr(paper, "_apply_auto_conditions", lambda *a, **k: 0)
    monkeypatch.setattr(paper, "_risk_blocks_entry", lambda *a, **k: False)
    monkeypatch.setattr("wyckoff.datasource.fetch_kline", lambda *a, **k: None)
    monkeypatch.setattr("wyckoff.indicators.add_indicators", lambda df, **k: df)
    return st


def test_run_cycle_fund_gate_blocks_d(monkeypatch):
    from wyckoff import paper
    from wyckoff.settings_keys import S

    _write_hist({
        "600104": [{"d": "2026-06-30", "n": "2026-08-29",
                    "rev": -1.0, "np": -50.0, "eps": -0.1}],
        "600000": [{"d": "2026-06-30", "n": "2026-08-29",
                    "rev": 10.0, "np": 40.0, "eps": 1.0}],
    })
    st = _prep_cycle(monkeypatch)
    paper.run_cycle(settings={S.Paper.FUND_GATE: True,
                              S.Paper.FUND_TIER_WEIGHT: False,
                              S.Paper.LIMIT_FILL: False},
                    candidates=[_cand("sh600104"), _cand("sh600000")],
                    anytime=True)
    bought = [p["symbol"] for p in st["positions"]]
    assert "sh600000" in bought          # A 层放行
    assert "sh600104" not in bought      # D 层被基本面硬门禁拦截
    paper.apply_paper_params(None)       # 还原 _CUR 默认


def test_run_cycle_fund_weight_scales_qty(monkeypatch):
    from wyckoff import paper
    from wyckoff.settings_keys import S

    _write_hist({
        "600104": [{"d": "2026-06-30", "n": "2026-08-29",
                    "rev": -1.0, "np": -50.0, "eps": -0.1}],
    })
    # 对照组: 权重关
    st1 = _prep_cycle(monkeypatch)
    paper.run_cycle(settings={S.Paper.FUND_GATE: False,
                              S.Paper.FUND_TIER_WEIGHT: False,
                              S.Paper.LIMIT_FILL: False},
                    candidates=[_cand("sh600104")], anytime=True)
    q_base = st1["positions"][0]["qty"]
    # 实验组: D 层权重 0.25 (门禁关时仍允许小仓试探)
    st2 = _prep_cycle(monkeypatch)
    paper.run_cycle(settings={S.Paper.FUND_GATE: False,
                              S.Paper.FUND_TIER_WEIGHT: True,
                              S.Paper.LIMIT_FILL: False},
                    candidates=[_cand("sh600104")], anytime=True)
    q_scaled = st2["positions"][0]["qty"]
    assert 0.20 < q_scaled / q_base < 0.30
    paper.apply_paper_params(None)


# ── 7. CLI 趋势报告 ──────────────────────────────────────────
def test_fundamental_trend_report(capsys):
    _write_hist({
        "600104": [
            {"d": "2025-12-31", "n": "2026-04-02", "rev": 4.6, "np": 300.0,
             "eps": 0.9, "roe": 3.4, "gm": 10.1},
            {"d": "2026-06-30", "n": "2026-08-29", "rev": -0.3, "np": -14.4,
             "eps": 0.45, "roe": 1.7, "gm": 11.7},
        ],
    })
    from wyckoff import fundamental_trend as ft

    rep = ft.build_report("600104", fetch=False, live=False)
    assert rep["code"] == "600104"
    assert rep["tier"] == "C" and rep["weight"] == 0.5 and rep["gate_ok"] is True
    assert "负增长" in rep["verdict"]["np"]
    assert "营收同比" in rep["verdict"]["rev"]
    # main --json 退出码 0 且输出可解析
    rc = ft.main(["600104", "--json", "--no-fetch", "--no-live"])
    out = capsys.readouterr().out
    assert rc == 0 and json.loads(out)["tier"] == "C"
    # 文本模式含分层与判读
    rc = ft.main(["600104", "--no-fetch", "--no-live"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "基本面趋势报告" in out and "按公告日" in out and "趋势判读" in out


def test_fundamental_trend_missing_data_exit1():
    from wyckoff import fundamental_trend as ft

    rc = ft.main(["900001", "--json", "--no-fetch", "--no-live"])
    assert rc == 1


def test_trend_verdict_consecutive_negative():
    from wyckoff import fundamental_trend as ft

    rows = [
        {"d": "2026-03-31", "n": "2026-04-30", "rev": 2.0, "np": 5.0, "roe": 2.0},
        {"d": "2026-06-30", "n": "2026-08-29", "rev": -1.0, "np": -10.0, "roe": 1.5},
    ]
    v = ft.trend_verdict(rows, tier="C")
    assert "连续 1 期负增长" in v["np"]
    assert "下行" in v["rev"]
    assert "C" in v["conclusion"]
