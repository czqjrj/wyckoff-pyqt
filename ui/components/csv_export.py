"""统一的扫描/清单结果 CSV 导出 (此前 extra_windows ×7 + calibration_center
同构实现各自为政: 时间戳命名/编码/弹窗文案漂移)。"""
import csv
import os
from datetime import datetime

from PyQt6.QtWidgets import QMessageBox


def export_results_csv(parent, prefix, headers, rows,
                       complete_msg=None, filename=None) -> str | None:
    """把 rows (list[dict]) 按 headers (list[(key, 中文标题)]) 写出 CSV。

    落盘: {DATA_DIR}/{filename 或 wx_{prefix}_{YYYYmmdd_HHMM}.csv}
    (utf-8-sig, Excel 兼容)。成功后弹提示并返回路径;
    rows 为空时提示"无数据"并返回 None。
    """
    if not rows:
        QMessageBox.information(parent, "导出", "没有可导出的数据")
        return None
    from wyckoff.paths import DATA_DIR
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    path = os.path.join(DATA_DIR,
                        filename or f"wx_{prefix}_{ts}.csv")
    headers_out = [h for _k, h in headers]
    keys = [k for k, _h in headers]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers_out)
        for r in rows:
            if isinstance(r, dict):
                w.writerow([r.get(k, "") for k in keys])
            else:
                w.writerow(list(r))
    QMessageBox.information(
        parent, "导出完成", complete_msg or f"已导出:\n{path}")
    return path
