"""模拟盘状态层 (wyckoff/paper/_state.py) 加固回归测试。

Round-1 (docs/project_quality_plan.md):
- S1: 损坏账户文件 → 自动备份 + 重建新账户 + meta 标记; 落盘失败返回 False。
- S3: _weak_market_flag 数据异常时 fail-close (按弱市限仓), 不再 fail-open。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff.paper import _selection, _state


def _fresh_cur(monkeypatch):
    import wyckoff.paper as paper
    monkeypatch.setattr(paper, "_CUR", {"init_cash": 1_000_000.0})


# ── S1: 损坏账户文件 ──

def test_load_state_corrupt_rebuilds_and_backs_up(monkeypatch, tmp_path):
    """文件解析失败 → 备份损坏文件 (.corrupt-*) + 返回新账户 + meta 标记。"""
    import wyckoff.paper as paper
    _fresh_cur(monkeypatch)
    p = tmp_path / "wx_paper.json"
    p.write_text("{this is not json", encoding="utf-8")
    monkeypatch.setattr(_state, "PAPER_FILE", str(p))

    st = _state.load_state()

    assert st["meta"].get("rebuilt_after_corruption") is True
    assert not p.exists(), "损坏原文件应被移走备份"
    baks = list(tmp_path.glob("wx_paper.json.corrupt-*"))
    assert len(baks) == 1, "应恰好生成一份备份"
    assert "cash" in st and float(paper._CUR["init_cash"]) == st["cash"]


def test_load_state_missing_returns_fresh(monkeypatch, tmp_path):
    """文件不存在 → 全新默认账户 (非异常路径)。"""
    _fresh_cur(monkeypatch)
    p = tmp_path / "wx_paper.json"
    monkeypatch.setattr(_state, "PAPER_FILE", str(p))
    st = _state.load_state()
    assert st["meta"].get("rebuilt_after_corruption") is None
    assert st["positions"] == [] and st["cash"] == 1_000_000.0


# ── S1: 落盘失败可观测 ──

def test_save_state_failure_returns_false(monkeypatch, tmp_path):
    """写入异常 → 返回 False (调用方记录日志/计数)。"""
    p = tmp_path / "wx_paper.json"
    monkeypatch.setattr(_state, "PAPER_FILE", str(p))

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(_state.json, "dump", _boom)
    assert _state.save_state({"cash": 1.0, "positions": []}) is False


def test_save_load_roundtrip(monkeypatch, tmp_path):
    """正常落盘 → 读回一致 (回归保护)。"""
    _fresh_cur(monkeypatch)
    p = tmp_path / "wx_paper.json"
    monkeypatch.setattr(_state, "PAPER_FILE", str(p))
    st = _state._new_state()
    st["positions"] = [{"symbol": "600104", "qty": 100}]
    assert _state.save_state(st) is True
    loaded = _state.load_state()
    assert loaded["positions"] == st["positions"]
    assert loaded["cash"] == 1_000_000.0


# ── S3: 弱市判定 fail-close ──

def test_weak_market_flag_exception_fail_closes(monkeypatch):
    """数据/判定异常 → 返回 True (按弱市处理), 不 fail-open。"""
    import wyckoff.paper as paper
    monkeypatch.setattr(paper, "_CUR", {"weak_filter": True})

    def _boom():
        raise RuntimeError("index data unavailable")

    monkeypatch.setattr(paper, "_market_trend_ok", _boom)
    assert _selection._weak_market_flag() is True


def test_weak_market_flag_disabled_not_weak(monkeypatch):
    """弱市过滤关闭 → 不强 (不受限, 用户显式选择)。"""
    import wyckoff.paper as paper
    monkeypatch.setattr(paper, "_CUR", {"weak_filter": False})

    def _boom():
        raise RuntimeError("should not be called")

    monkeypatch.setattr(paper, "_market_trend_ok", _boom)
    assert _selection._weak_market_flag() is False


def test_weak_market_flag_trend_down_is_weak(monkeypatch):
    """指数未站上 MA20 → 弱市 (正常数据路径)。"""
    import wyckoff.paper as paper
    monkeypatch.setattr(paper, "_CUR", {"weak_filter": True})
    monkeypatch.setattr(paper, "_market_trend_ok", lambda: (False, "index below MA20"))
    assert _selection._weak_market_flag() is True
