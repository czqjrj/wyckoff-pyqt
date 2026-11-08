"""A股独立 GUI 的自选股列表（批量分析 / 批量记录用）。

与旧 Wyckoff 的 `wyckoff_watchlist.json` 分开，避免影响原系统。
"""

from __future__ import annotations

import json
import os
import re
import time

from ._log import log_exc
from .paths import DATA_DIR

ASHARE_WATCHLIST_FILE = os.path.join(DATA_DIR, "wx_ashare_watchlist.json")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def normalize_code(value: str) -> str:
    """把 `600519` / `sh600519` / `600519.SH` 等归一化成 6 位代码。"""
    code = str(value or "").strip().lower().replace(".", "")
    code = re.sub(r"^(sh|sz|bj)", "", code)
    code = re.sub(r"^(sh|sz|bj)$", "", code)
    digits = re.sub(r"\D", "", code)
    return digits.zfill(6) if digits else ""


def parse_code_list(text: str) -> list[str]:
    """从 `600519, 000001; sh600519` 之类文本里解析代码。"""
    out: list[str] = []
    for part in re.split(r"[,，;；\s]+", str(text or "")):
        if not part:
            continue
        code = normalize_code(part)
        if code and code.isdigit() and code not in out:
            out.append(code)
    return out


def _load(path: str | None = None) -> dict:
    path = path or ASHARE_WATCHLIST_FILE
    if not os.path.exists(path):
        return {"version": 1, "watchlist": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        log_exc("读取A股自选股失败", e)
        return {"version": 1, "watchlist": []}
    if isinstance(data, list):
        data = {"version": 1, "watchlist": data}
    if not isinstance(data, dict):
        return {"version": 1, "watchlist": []}
    data.setdefault("version", 1)
    items = data.get("watchlist")
    data["watchlist"] = items if isinstance(items, list) else []
    return data


def _save(data: dict, path: str | None = None) -> None:
    path = path or ASHARE_WATCHLIST_FILE
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def list_watchlist(path: str | None = None) -> list[dict]:
    return list(_load(path).get("watchlist") or [])


def add_watchlist(codes, path: str | None = None) -> list[dict]:
    """添加自选股。`codes` 可以是代码字符串列表，也可以是 `{code, name}` 字典列表。"""
    data = _load(path)
    items = data.get("watchlist") or []
    by_code = {str(r.get("code")): r for r in items if r.get("code")}
    for code in codes or []:
        if isinstance(code, dict):
            c = normalize_code(code.get("code"))
            name = str(code.get("name") or "").strip()
        else:
            c = normalize_code(code)
            name = ""
        if not c:
            continue
        if c in by_code:
            if name and not by_code[c].get("name"):
                by_code[c]["name"] = name
            continue
        item = {"code": c, "name": name, "added_at": _now()}
        items.append(item)
        by_code[c] = item
    data["watchlist"] = items
    _save(data, path)
    return items


def remove_watchlist(codes, path: str | None = None) -> list[dict]:
    remove = {normalize_code(c) for c in (codes or [])}
    data = _load(path)
    items = [r for r in data.get("watchlist") or [] if str(r.get("code")) not in remove]
    data["watchlist"] = items
    _save(data, path)
    return items


def clear_watchlist(path: str | None = None) -> list[dict]:
    data = _load(path)
    data["watchlist"] = []
    _save(data, path)
    return []
