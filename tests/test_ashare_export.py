"""报告导出 PDF 单元测试 (offscreen, 不触网)。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from wyckoff.ashare_export import write_pdf


def test_write_pdf(tmp_path):
    path = str(tmp_path / "report.pdf")
    text = (
        "# A股市场状态报告\n\n"
        "## 结论\n\n"
        "| 指数 | 状态分 | 状态 |\n|---|---:|---|\n"
        "| 上证指数 | 28.5 | 谨慎 |\n\n"
        "> 估值只作中期锚，不预测短期涨跌。\n"
    )
    out = write_pdf(text, path, title="测试报告")
    assert out == path
    assert os.path.exists(path)
    assert os.path.getsize(path) > 100
    with open(path, "rb") as f:
        assert f.read(4) == b"%PDF"
