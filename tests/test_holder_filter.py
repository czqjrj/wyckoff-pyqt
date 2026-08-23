"""股东户数失真记录过滤的回归测试。

背景: 福莱特 (601865) 上市初期 17户→13.9万户 (环比+819570%) 的假性暴增曾被
误展示为 "筹码分散 / 派发迹象"。holder_ratio_ok 是单一权威过滤函数,
fundamental / market / chart 均复用, 本测试锁定其行为防止回归。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff.fundamental import holder_ratio_ok


def _rec(holder_num=139344, pre_num=17, ratio=819570.588235294):
    return {"end_date": "2019-02-15", "pre_date": "2019-01-21",
            "holder_num": holder_num, "pre_num": pre_num, "ratio": ratio}


def test_fuletai_ipo_spike_filtered():
    """福莱特 IPO 假性暴增记录必须被过滤。"""
    assert holder_ratio_ok(_rec()) is False


def test_normal_ratio_accepted():
    """正常环比 (±100% 内) 应通过。"""
    assert holder_ratio_ok(_rec(ratio=-2.5)) is True
    assert holder_ratio_ok(_rec(ratio=10.0)) is True
    assert holder_ratio_ok(_rec(ratio=-90.0)) is True
    assert holder_ratio_ok(_rec(ratio=100.0)) is True


def test_boundary_ratio_filtered():
    """环比超过 +100% 视为失真。"""
    assert holder_ratio_ok(_rec(ratio=100.1)) is False
    assert holder_ratio_ok(_rec(ratio=500.0)) is False


def test_missing_fields_filtered():
    """ratio / holder_num / pre_num 任一缺失或非数均为失真。"""
    rec = _rec()
    rec["ratio"] = None
    assert holder_ratio_ok(rec) is False
    rec = _rec()
    rec["pre_num"] = None
    assert holder_ratio_ok(rec) is False
    rec = _rec()
    rec["holder_num"] = 0
    assert holder_ratio_ok(rec) is False
    rec = _rec()
    rec["ratio"] = "abc"
    assert holder_ratio_ok(rec) is False


def test_empty_or_none_filtered():
    assert holder_ratio_ok(None) is False
    assert holder_ratio_ok({}) is False
    assert holder_ratio_ok([]) is False


def test_ipo_first_record_filtered():
    """上市首条 (pre_num=0, ratio=None) 应被过滤。"""
    rec = _rec(holder_num=17, pre_num=0, ratio=None)
    assert holder_ratio_ok(rec) is False
