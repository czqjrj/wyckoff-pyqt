"""A股自选股（独立 GUI 批量分析/批量记录）单元测试。"""

from wyckoff import ashare_watchlist as aw


def setup_function(_func):
    aw.clear_watchlist()


def test_normalize_code():
    assert aw.normalize_code("600519") == "600519"
    assert aw.normalize_code("sh600519") == "600519"
    assert aw.normalize_code("000001.SZ") == "000001"
    assert aw.normalize_code("bad") == ""


def test_parse_code_list():
    assert aw.parse_code_list("600519, 000001；sh600519\n300750") == [
        "600519",
        "000001",
        "300750",
    ]
    assert aw.parse_code_list("bad, , ,") == []


def test_add_remove_clear():
    items = aw.add_watchlist(["600519", "sh600519", "000001"])
    assert [r["code"] for r in items] == ["600519", "000001"]

    aw.add_watchlist([{"code": "000002.SZ", "name": "万科A"}])
    items = aw.list_watchlist()
    by_code = {r["code"]: r for r in items}
    assert by_code["000002"]["name"] == "万科A"

    aw.remove_watchlist(["600519"])
    items = aw.list_watchlist()
    assert [r["code"] for r in items] == ["000001", "000002"]

    aw.clear_watchlist()
    assert aw.list_watchlist() == []
