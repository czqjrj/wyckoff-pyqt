"""阈值注册表 (calib_registry) 的登记正确性、回退逻辑与消费方一致性的测试。"""
from datetime import date, timedelta

import pytest

from wyckoff import calib_registry as cr


def test_lookup_hits_expected_adjs():
    """数值区间桶: 命中桶返回登记值, 未命中返回 default。"""
    assert cr.bucket_value(cr.get("sc_env_gate"), -0.20) == 8
    assert cr.bucket_value(cr.get("sc_env_gate"), -0.12) == 0  # 中间段不加不减
    assert cr.bucket_value(cr.get("sc_env_gate"), 0.0) == -15
    assert cr.bucket_value(cr.get("bc_env_gate"), 0.20) == 8
    assert cr.bucket_value(cr.get("bc_env_gate"), 0.10) == 0
    assert cr.bucket_value(cr.get("bc_env_gate"), 0.0) == -12
    assert cr.bucket_value(cr.get("bc_uptrend"), 1.0) == -5
    assert cr.bucket_value(cr.get("bc_uptrend"), 0.0) == 0
    assert cr.bucket_value(cr.get("sow_vol_gate"), 3.0) == 8
    assert cr.bucket_value(cr.get("sow_vol_gate"), 1.8) == 2
    assert cr.bucket_value(cr.get("sow_vol_gate"), 1.0) == -8


def test_boll_cap_gate():
    """门控桶: 高位关断 (0), 低位放行 (1)。"""
    assert cr.bucket_value(cr.get("sos_joc_boll_cap"), 0.9) == 0
    assert cr.bucket_value(cr.get("sos_joc_boll_cap"), 0.5) == 1


def test_weak_event_half_type_bucket():
    """类型桶匹配事件类型; 未登记类型回退 default=1.0 (原行为不半权)。"""
    assert cr.bucket_value(cr.get("weak_event_half"), "SOS") == 0.5
    assert cr.bucket_value(cr.get("weak_event_half"), "JOC") == 0.5
    assert cr.bucket_value(cr.get("weak_event_half"), "Spring") == 1.0


def test_expired_entry_falls_back_default():
    """过期条目整体回退 default (不区分命中与否)。"""
    key = "sow_vol_gate"
    entry = cr.REGISTRY[key]
    as_of = date.fromisoformat(entry.as_of)
    far_future = as_of + timedelta(days=entry.max_age_days + 1)
    assert cr.bucket_value(entry, 3.0, today=far_future) == entry.default


def test_undersampled_bucket_falls_back_default():
    """命中桶样本量 < min_n → 回退 default (视为无调查结论)。"""
    stale = cr.CalibEntry(
        key="demo",
        as_of="2026-09-15",
        feature="x",
        min_n=1000,
        buckets=(cr.Bucket(label="足量", value=5, lo=0, hi=0.5, n=50),),
        default=-1.0,
    )
    assert cr.bucket_value(stale, 0.2) == -1.0
    assert cr.bucket_value(stale, 0.9) == -1.0


def test_registry_is_fresh_on_landing_date():
    """每个真实条目在调查落地日都生效, 且唯一键注册。"""
    for entry in cr.REGISTRY.values():
        assert entry.buckets, entry.key
        assert entry.key == cr.get(entry.key).key
        # 落地当天 (as_of) 不应视为过期 → 命中桶能取到登记值
        today = date.fromisoformat(entry.as_of)
        for b in entry.buckets:
            if b.lo is None and b.hi is None:
                key = b.label
            else:
                key = b.lo if b.lo is not None else b.hi
                if key is None:
                    key = 0.0
            result = cr.resolve(entry, key, today=today)
            assert result is not None, (entry.key, b.label)


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        cr.get("no_such_key")
