"""模拟盘参数单一真源不变式 + 风控上限钳制 + 漂移告警测试。

背景: 引擎真源为 wyckoff.paper._params; config.DEFAULT_SETTINGS 与
settings_keys.DEFAULTS 长期靠手抄同步, 磁盘 wyckoff_settings.json 曾被运行中
客户端写回旧值静默覆盖调优参数 (maxpos3/止损5%/conf90/移动止盈关/VA开)。

本测试保证:
  1) 三个 Python 源里 paper_* 数值必须与引擎常量一致 (任一改值漂移即 CI 失败);
  2) apply_paper_params 对超校准上限的风控值强制钳制 (更严可、更松不行);
  3) load_settings 对过期校准版本的文件一次性回迁为引擎默认并落盘, 版本对齐后
     的用户主动调优只告警不覆盖 (既不静默弱化风控, 也不反复抹掉用户改值)。
"""
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── 常量名映射: 引擎真源 _params → 设置键 → config/settings_keys 应取值 ──
from wyckoff.paper import _params as P  # noqa: E402

# config.DEFAULT_SETTINGS 中与引擎 _params 同名同值的可校验键 (独立不变的
# 手抄源校验; 无对应引擎常量的除外: scan_interval/enable_chinext/star/sizing/推送)
AAA = {
    "paper_init_cash": P.INIT_CASH,
    "paper_max_pos": P.MAX_POSITIONS,
    "paper_hold_bars": P.HOLD_BARS,
    "paper_stop_loss": P.STOP_LOSS,
    "paper_take_profit": P.TAKE_PROFIT,
    "paper_cost": P.COST,
    "paper_min_conf": P.MIN_CONF,
    "paper_st_confirm": P.ST_CONFIRM,
    "paper_stop_cooldown": P.STOP_COOLDOWN,
    "paper_commission_rate": P.COMMISSION_RATE,
    "paper_min_commission": P.MIN_COMMISSION,
    "paper_stamp_tax_rate": P.STAMP_TAX_RATE,
    "paper_transfer_fee_rate": P.TRANSFER_FEE_RATE,
    "paper_limit_fill": P.LIMIT_FILL,
    "paper_trailing_stop": P.TRAILING_STOP,
    "paper_trail_back_pct": P.TRAIL_BACK_PCT,
    "paper_trail_activate_pct": P.TRAIL_ACTIVATE_PCT,
    "paper_trail_atr_mult": P.TRAIL_ATR_MULT,
    "paper_weak_filter": P.WEAK_FILTER,
    "paper_weak_max_pos": P.WEAK_MAX_POS,
    "paper_weak_index_code": P.WEAK_INDEX_CODE,
    "paper_va_weight": P.VA_WEIGHT,
    "paper_enable_va": P.ENABLE_VA,
    "paper_enable_long_left": P.ENABLE_LONG_LEFT,
    "paper_enable_event_vsa": P.ENABLE_EVENT_VSA,
    "paper_rebalance": P.REBALANCE,
    "paper_push_enabled": P.PUSH_ENABLED,
    "paper_push_method": P.PUSH_METHOD,
    "paper_max_drawdown": P.MAX_DRAWDOWN_PCT,
    "paper_max_risk_pct": P.MAX_RISK_PCT,
    "paper_max_sector_conc": P.MAX_SECTOR_CONCENTRATION,
    "paper_max_single_conc": P.MAX_SINGLE_CONCENTRATION,
    "paper_correlation_threshold": P.CORRELATION_THRESHOLD,
    "paper_vol_adjust_enabled": P.VOL_ADJUST_ENABLED,
    "paper_max_capital_usage": P.MAX_CAPITAL_USAGE,
    "paper_qlib_veto": P.QLIB_VETO_ENABLED,
    "paper_qlib_veto_hi": P.QLIB_VETO_HI,
}


def test_config_defaults_match_engine_source():
    from wyckoff.config import DEFAULT_SETTINGS
    for key, expected in AAA.items():
        assert DEFAULT_SETTINGS.get(key) == expected, (
            f"config.DEFAULT_SETTINGS[{key}] = {DEFAULT_SETTINGS.get(key)!r} "
            f"≠ 引擎真源 {expected!r} —— 应与 _params 一致")


def test_settings_keys_defaults_match_engine_source():
    """DEFAULTS 须覆盖 Paper 域全部键, 且取值 == 引擎真源。

    基准直接用 settings_keys._engine_paper_defaults() (单一真源映射),
    DEFAULTS 由它回填, 此处防的是"手抄字面量又被写回 / update 被删"。
    """
    from wyckoff.settings_keys import DEFAULTS, _engine_paper_defaults
    from wyckoff.settings_keys import Paper as SK_Paper

    engine = _engine_paper_defaults()
    members = {m.value for m in SK_Paper}
    missing = sorted(members - set(DEFAULTS))
    assert not missing, f"settings_keys.DEFAULTS 缺 Paper 域键: {missing}"
    for key, expected in engine.items():
        assert DEFAULTS.get(key) == expected, (
            f"settings_keys.DEFAULTS[{key}] = {DEFAULTS.get(key)!r} "
            f"≠ 引擎真源 {expected!r}")
    # 无引擎常量的键 (扫描间隔/板块权限/仓位方法) 与 config 登记一致
    from wyckoff.config import DEFAULT_SETTINGS as CFG
    for key in members - set(engine):
        assert DEFAULTS.get(key) == CFG.get(key), key


def test_risk_params_clamped_to_calibrated_ceiling():
    """风化风控值超出校准上限必须被钳制 (更严可、更松不行)。"""
    import wyckoff.paper as paper
    loose = {
        "paper_max_drawdown": 0.30,
        "paper_max_risk_pct": 0.05,
        "paper_max_sector_conc": 0.60,
        "paper_max_single_conc": 0.40,
        "paper_correlation_threshold": 0.90,
        "paper_max_capital_usage": 1.0,
    }
    c = paper.apply_paper_params(loose)
    assert c["max_drawdown"] == P.MAX_DRAWDOWN_PCT
    assert c["max_risk_pct"] == P.MAX_RISK_PCT
    assert c["max_sector_conc"] == P.MAX_SECTOR_CONCENTRATION
    assert c["max_single_conc"] == P.MAX_SINGLE_CONCENTRATION
    assert c["correlation_threshold"] == P.CORRELATION_THRESHOLD
    assert c["max_capital_usage"] == P.MAX_CAPITAL_USAGE


def test_risk_params_tighter_than_calibrated_preserved():
    """比校准值更严的用户设置应原样保留 (钳制只拦放宽, 不拦收紧)。"""
    import wyckoff.paper as paper
    tight = {
        "paper_max_drawdown": 0.08,
        "paper_max_risk_pct": 0.01,
        "paper_max_sector_conc": 0.25,
        "paper_max_single_conc": 0.15,
        "paper_correlation_threshold": 0.50,
        "paper_max_capital_usage": 0.80,
    }
    c = paper.apply_paper_params(tight)
    assert c["max_drawdown"] == 0.08
    assert c["max_risk_pct"] == 0.01
    assert c["max_sector_conc"] == 0.25
    assert c["max_single_conc"] == 0.15
    assert c["correlation_threshold"] == 0.50
    assert c["max_capital_usage"] == 0.80


def test_load_settings_migrates_legacy_paper_drift(tmp_path, monkeypatch, caplog):
    """过期校准版本 (旧客户端写回的旧值) 首启回迁为引擎默认并落盘。"""
    import wyckoff.storage as storage
    drifted = {
        "paper_max_pos": 3,
        "paper_stop_loss": 0.05,
        "paper_min_conf": 90,
        "paper_trailing_stop": False,
        "paper_enable_va": True,
    }
    f = tmp_path / "wyckoff_settings.json"
    f.write_text(json.dumps(drifted), encoding="utf-8")
    monkeypatch.setattr(storage, "SETTINGS_FILE", str(f))
    storage._PAPER_DRIFT_SEEN.clear()
    with caplog.at_level(logging.WARNING, logger="wyckoff.storage"):
        s = storage.load_settings()
    # 漂移值被回迁为校准默认
    assert s["paper_max_pos"] == P.MAX_POSITIONS
    assert s["paper_stop_loss"] == P.STOP_LOSS
    assert s["paper_min_conf"] == P.MIN_CONF
    assert s["paper_trailing_stop"] == P.TRAILING_STOP
    assert s["paper_enable_va"] == P.ENABLE_VA
    assert "回迁" in caplog.text
    # 回迁结果与版本戳已落盘, 二次加载不再回迁、不再告警
    on_disk = json.loads(f.read_text(encoding="utf-8"))
    assert on_disk["paper_max_pos"] == P.MAX_POSITIONS
    assert on_disk[storage._CALIBRATION_VERSION_KEY] == storage.PAPER_CALIBRATION_VERSION
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="wyckoff.storage"):
        storage.load_settings()
    assert "回迁" not in caplog.text


def test_load_settings_warns_on_post_migration_drift(tmp_path, monkeypatch, caplog):
    """版本已对齐后仍偏离 = 用户主动调优: 只告警, 不覆盖。"""
    import wyckoff.storage as storage
    drifted = {
        storage._CALIBRATION_VERSION_KEY: storage.PAPER_CALIBRATION_VERSION,
        "paper_max_pos": 3,
        "paper_stop_loss": 0.05,
        "paper_min_conf": 90,
    }
    f = tmp_path / "wyckoff_settings.json"
    f.write_text(json.dumps(drifted), encoding="utf-8")
    monkeypatch.setattr(storage, "SETTINGS_FILE", str(f))
    storage._PAPER_DRIFT_SEEN.clear()
    with caplog.at_level(logging.WARNING, logger="wyckoff.storage"):
        s = storage.load_settings()
    # 用户改值原样保留, 仅逐键告警
    assert s["paper_max_pos"] == 3
    assert s["paper_stop_loss"] == 0.05
    assert s["paper_min_conf"] == 90
    assert "模拟盘参数漂移" in caplog.text
    assert "paper_max_pos" in caplog.text
    assert "paper_stop_loss" in caplog.text
    assert "paper_min_conf" in caplog.text


def test_load_settings_migrates_v1_to_v2_tp_trail(tmp_path, monkeypatch, caplog):
    """v2 校准: 旧 v1 磁盘值 (止盈 15% / 追踪 8%) 一次性回迁为网格最优
    (30% / 6%), 并打版本戳; 二次加载不再回迁。"""
    import wyckoff.storage as storage
    v1 = {
        "_paper_calibration_version": 1,
        "paper_take_profit": 0.15,
        "paper_trail_back_pct": 0.08,
    }
    f = tmp_path / "wyckoff_settings.json"
    f.write_text(json.dumps(v1), encoding="utf-8")
    monkeypatch.setattr(storage, "SETTINGS_FILE", str(f))
    storage._PAPER_DRIFT_SEEN.clear()
    with caplog.at_level(logging.WARNING, logger="wyckoff.storage"):
        s = storage.load_settings()
    assert s["paper_take_profit"] == P.TAKE_PROFIT == 0.30
    assert s["paper_trail_back_pct"] == P.TRAIL_BACK_PCT == 0.06
    assert "回迁" in caplog.text
    on_disk = json.loads(f.read_text(encoding="utf-8"))
    assert on_disk[storage._CALIBRATION_VERSION_KEY] == storage.PAPER_CALIBRATION_VERSION
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="wyckoff.storage"):
        storage.load_settings()
    assert "回迁" not in caplog.text
