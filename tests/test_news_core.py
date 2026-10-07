"""新闻情绪 (wyckoff/news.py) 纯函数骨架单测 (T2: 最薄模块补测)。

覆盖: HTML 清洗 / 例行公告判定 / 分级关键词打分 (否定·条件·强度饱和) /
板块抽取 / 近似去重 / 价格反应验证 (effort-vs-result) / 事件共振。
全部离线: 不触碰任何网络抓取路径。
"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import pytest

from wyckoff import news as nw

# ── 清洗与例行公告 ──

def test_clean_html_strips_tags_and_entities():
    """标签、HTML 实体、多余空白一律清掉。"""
    assert nw._clean_html("<p>重大资产重组&nbsp;落地</p>") == "重大资产重组 落地"
    assert nw._clean_html("  a\n\tb  ") == "a b"
    assert nw._clean_html("无标签<b>加粗</b>") == "无标签加粗"


def test_is_routine_ann_gated_by_source():
    """例行公告只对交易所公告源生效; 其余源一律非例行。"""
    title = "关于召开2025年年度股东大会的通知"
    assert nw.is_routine_ann(title) is True
    assert nw.is_routine_ann(title, "eastmoney_ann") is True
    assert nw.is_routine_ann(title, "em_stock_news") is False, "资讯源不降权"
    assert nw.is_routine_ann(title, "irm_qa") is False
    assert nw.is_routine_ann("签订重大合同", "eastmoney_ann") is False


# ── 关键词打分 ──

def test_score_text_bounds_and_direction():
    """分数域 [-1,1]; 利好为正、利空为负、无关键词为 0。"""
    assert nw._score_text("中性标题没有关键词") == 0.0
    assert nw._score_text("") == 0.0
    bull = nw._score_text("业绩预增")
    bear = nw._score_text("立案调查")
    assert 0.0 < bull <= 1.0
    assert -1.0 <= bear < 0.0


def test_score_text_intensity_saturation():
    """单个弱词只产生小分值; 强词才接近满格 (强度缩放)。"""
    weak = nw._score_text("分红")
    strong = nw._score_text("业绩预增")
    assert abs(weak) < 0.5, f"弱词不应满格, 实际 {weak}"
    assert abs(strong) > abs(weak)


def test_score_text_negation_flips_semantics():
    """否定前缀反转方向, 且连带消耗前缀不重复计分。"""
    assert nw._score_text("重大资产重组") > 0
    assert nw._score_text("终止重大资产重组") < 0, "终止+利好词 → 利空"
    assert nw._score_text("大股东减持") < 0
    assert nw._score_text("取消减持计划") > 0, "取消+利空词 → 利好"


def test_score_text_conditional_prefix_discounts():
    """筹划中的事项 (拟/筹划) 权重打折, 分值弱于已落地事项。"""
    done = abs(nw._score_text("并购"))
    pending = abs(nw._score_text("拟并购"))
    assert 0 < pending < done, f"拟并购 {pending} 应小于 并购 {done}"


# ── 板块与去重 ──

def test_extract_sector():
    """按行业关键词表抽取板块 (大小写不敏感)。"""
    assert "AI" in nw._extract_sector("公司发布大模型与算力芯片")
    assert "新能源" in nw._extract_sector("签订锂电储能订单")
    assert nw._extract_sector("普通公司新闻") == []


def _item(title, source="em_stock_news", hours=0, content=""):
    return {"title": title, "source": source, "content": content,
            "datetime": pd.Timestamp("2023-06-01 09:00") + timedelta(hours=hours)}


def test_dedup_news_keeps_authoritative_source():
    """同题多源: 保留权威度最高一条, dup_count 累加, 正文互补。"""
    ann = _item("某公司与华为签署战略合作协议", source="eastmoney_ann", hours=1)
    news = [_item("某公司与华为签署战略合作协议", hours=0, content="资讯正文"),
            ann]
    out = nw.dedup_news(news)
    assert len(out) == 1, "同题应合并为一条"
    kept = out[0]
    assert kept["source"] == "eastmoney_ann", "权威源保留"
    assert kept["dup_count"] == 2
    assert kept["content"] == "资讯正文", "代表条正文为空时互补进被合并条"


def test_dedup_news_keeps_distinct_items():
    """标题不同 / 超出时间窗 / 空标题 的条目不得误合并。"""
    a = _item("某公司中标10亿元合同", hours=0)
    b = _item("某公司股东减持计划公告", hours=1)
    assert len(nw.dedup_news([a, b])) == 2

    far = _item("某公司与华为签署战略合作协议", hours=100)
    near = _item("某公司与华为签署战略合作协议", hours=0)
    assert len(nw.dedup_news([near, far])) == 2, "超出 36h 窗口不合并"

    empty = _item("", hours=0)
    other = _item("", hours=1)
    assert len(nw.dedup_news([empty, other])) == 2, "空标题不合并"


def test_dedup_news_sorted_newest_first():
    """输出按时间倒序 (调用方按新到旧消费)。"""
    old = _item("某公司中标10亿元合同", hours=-24)
    new = _item("某公司股东减持计划公告", hours=0)
    out = nw.dedup_news([old, new])
    assert [n["title"] for n in out] == ["某公司股东减持计划公告", "某公司中标10亿元合同"]


# ── 价格反应验证 (effort-vs-result) ──

def _mk_df(closes, vols=None, start="2023-01-01"):
    n = len(closes)
    return pd.DataFrame({
        "day": pd.date_range(start, periods=n, freq="D"),
        "open": list(closes),
        "high": [c * 1.001 for c in closes],
        "low": [c * 0.999 for c in closes],
        "close": list(closes),
        "volume": list(vols) if vols else [1e6] * n,
    })


def test_price_validation_pass_through_without_df():
    """df 缺失 / 根数不足 / 无 items → 原对象直接返回 (不抛、不改)。"""
    res = {"score": 0.5, "items": [{"score": 0.5, "weight": 1.0}]}
    assert nw.apply_price_validation(res, None) is res
    short = _mk_df([100.0] * 11)
    assert nw.apply_price_validation(res, short) is res
    no_items = {"score": 0.5}
    assert nw.apply_price_validation(no_items, _mk_df([100.0] * 30)) is no_items
    empty = {"score": 0.5, "items": []}
    assert nw.apply_price_validation(empty, _mk_df([100.0] * 30))["items"] == []


def test_price_validation_confirmed_boosts_weight():
    """利好后顺向上涨 ≥0.5% → confirmed, 权重 ×1.15, 原对象不被改写。"""
    closes = [100.0] * 30
    closes[13] = 102.0          # 发布后 3 根内 +2%
    df = _mk_df(closes)
    item = {"datetime": pd.Timestamp("2023-01-11"), "score": 0.5, "weight": 1.0}
    res = {"score": 0.5, "items": [item]}

    out = nw.apply_price_validation(res, df)

    assert out["items"][0]["validation"] == "confirmed"
    assert out["items"][0]["_w"] == pytest.approx(nw._W_CONFIRM)
    assert out["validation"]["confirmed"] == 1
    assert out["raw_score"] == 0.5
    assert "_w" not in res["items"][0], "纯函数: 不得修改入参 items"
    assert "validation" not in res["items"][0]


def test_price_validation_rejected_with_volume():
    """利好 + 放量逆向 ≥1% → rejected, 权重 ×0.40 (借利好出货)。"""
    closes = [100.0] * 30
    closes[10:14] = [99.5, 99.0, 98.5, 98.0]
    vols = [1e6] * 30
    for i in range(10, 14):
        vols[i] = 3e6            # 放量下跌
    df = _mk_df(closes, vols)
    item = {"datetime": pd.Timestamp("2023-01-11"), "score": 0.5, "weight": 1.0}
    out = nw.apply_price_validation({"score": 0.5, "items": [item]}, df)

    assert out["items"][0]["validation"] == "rejected"
    assert out["items"][0]["_w"] == pytest.approx(1.0 * nw._W_REJECT)
    assert out["validation"]["rejected"] == 1


def test_price_validation_pending_when_no_followthrough():
    """方向不明 / 发布太近 → pending, 权重不变。"""
    closes = [100.0] * 30
    df = _mk_df(closes)
    item = {"datetime": pd.Timestamp("2023-01-11"), "score": 0.5, "weight": 1.0}
    out = nw.apply_price_validation({"score": 0.5, "items": [item]}, df)
    assert out["items"][0]["validation"] == "pending"
    assert out["items"][0]["_w"] == 1.0
    assert out["validation"]["pending"] == 1

    late = {"datetime": pd.Timestamp("2023-01-31 23:00"), "score": 0.5, "weight": 1.0}
    out2 = nw.apply_price_validation({"score": 0.5, "items": [late]}, df)
    assert out2["items"][0]["validation"] == "pending", "发布在末根之后无观察窗"


# ── 事件共振 ──

def test_event_resonance_no_signal_when_weak_or_no_events():
    """|news|<0.15 或无事件 → 0 加成、空说明。"""
    assert nw.event_resonance({"score": 0.1}, [{"type": "LPS", "idx": 10}], 10) == (0.0, "")
    assert nw.event_resonance({"score": 0.5}, [], 10) == (0.0, "")
    assert nw.event_resonance({}, [{"type": "LPS", "idx": 10}], 10) == (0.0, "")


def test_event_resonance_same_direction_bonus():
    """利好 × 多头事件 (LPS) 同向 → 正加成与共振说明。"""
    bonus, note = nw.event_resonance(
        {"score": 0.5}, [{"type": "LPS", "idx": 100, "conf": 90}], max_idx=100)
    assert bonus > 0
    assert "同向共振" in note and "LPS" in note
    assert bonus <= nw._RES_MAX_BONUS


def test_event_resonance_spring_bearish_is_terror_pattern():
    """Spring 伴随利空 → 反向解读为恐吓筹码, 给固定小加分。"""
    bonus, note = nw.event_resonance(
        {"score": -0.5}, [{"type": "Spring", "idx": 100, "conf": 90}], max_idx=100)
    assert bonus == 8.0
    assert "恐吓筹码" in note


def test_event_resonance_utad_bullish_is_trap():
    """UTAD 伴随利好 → 诱多嫌疑, 固定减分。"""
    bonus, note = nw.event_resonance(
        {"score": 0.5}, [{"type": "UTAD", "idx": 100, "conf": 90}], max_idx=100)
    assert bonus == -14.0
    assert "诱多" in note


def test_event_resonance_ignores_events_outside_window():
    """超出近期窗口的事件不参与共振判定。"""
    bonus, note = nw.event_resonance(
        {"score": 0.5},
        [{"type": "LPS", "idx": 0, "conf": 90}],
        max_idx=1000, recent_window=120)
    assert bonus == 0.0 and note == ""
