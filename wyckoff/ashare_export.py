"""A股报告导出: Markdown → PDF (PyQt6 QTextDocument)。

核心引擎不依赖 PyQt; 这里把 PyQt6 延迟到函数内部导入, 只有真正导出 PDF 时才加载。
"""

from __future__ import annotations

import os


def write_pdf(markdown_text: str, path: str, title: str = "") -> str:
    """把 Markdown 报告渲染成 PDF。

    不依赖系统字体嵌入配置; 用 Qt 的 QTextDocument + QPdfWriter。
    需要可用的 QApplication (无显示环境可设 QT_QPA_PLATFORM=offscreen)。
    """
    from PyQt6.QtCore import QMarginsF
    from PyQt6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument
    from PyQt6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    doc = QTextDocument()
    css = (
        "body { font-family: 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif; "
        "font-size: 10pt; line-height: 1.5; color: #222; }"
        "h1 { font-size: 18pt; } h2 { font-size: 13pt; margin-top: 14pt; }"
        "h3 { font-size: 11pt; } table { border-collapse: collapse; }"
        "th, td { border: 1px solid #999; padding: 3px 6px; }"
        "blockquote { color: #555; }"
    )
    doc.setDefaultStyleSheet(css)
    doc.setMarkdown(markdown_text)
    if title:
        doc.setMetaInformation(QTextDocument.MetaInformation.DocumentTitle, title)

    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    writer = QPdfWriter(path)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(12, 12, 12, 12), QPageLayout.Unit.Millimeter)
    doc.print(writer)
    return path
