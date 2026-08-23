"""国家队持仓透视模块测试: 机构识别、报表期生成、持股格式化。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff.holdings import classify_holder, format_shares, report_dates


def test_classify_national_holders():
    cases = {
        "中央汇金资产管理有限责任公司": "中央汇金",
        "中国证券金融股份有限公司": "证金",
        "南方基金-农业银行-中证金融资产管理计划": "证金",
        "全国社会保障基金一一四组合": "社保",
        "基本养老保险基金八零八组合": "养老",
        "国家外汇管理局": "外管局",
        "梧桐树投资平台有限责任公司": "外管局",
    }
    for name, cat in cases.items():
        assert classify_holder(name) == cat, f"{name} -> {classify_holder(name)}"
    for name in ("上海汽车集团股份有限公司", "香港中央结算有限公司", ""):
        assert classify_holder(name) is None, name


def test_report_dates_desc():
    dates = report_dates(6)
    assert len(dates) == 6
    assert all(d.isdigit() and len(d) == 8 for d in dates)
    # 每期跨 3 个月, 依次向前
    for i in range(1, len(dates)):
        prev, cur = dates[i - 1], dates[i]
        y1, m1, y2, m2 = int(prev[:4]), int(prev[4:6]), int(cur[:4]), int(cur[4:6])
        gap = (y1 - y2) * 12 + (m1 - m2)
        assert gap == 3, f"{cur} -> {prev} 间隔 {gap} 个月"


def test_format_shares():
    assert format_shares(3.5e8) == "3.50亿股"
    assert format_shares(98580000) == "9,858.00万股"
    assert format_shares(12345) == "1.23万股"
    assert format_shares(None) == "-"
