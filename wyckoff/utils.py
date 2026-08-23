"""通用工具函数。"""


def normalize_symbol(code: str) -> str:
    """把用户输入规范化为 sina 风格代码, 如 600104 -> sh600104"""
    code = (code or "").strip().lower().replace(".sh", "").replace(".sz", "").replace(".bj", "")
    if len(code) == 6 and code.isdigit():
        if code.startswith(("6", "5")):
            return "sh" + code
        if code.startswith(("15", "16")):  # 深市 ETF/LOF
            return "sz" + code
        if code.startswith(("0", "2", "3")):
            return "sz" + code
        if code.startswith(("4", "8", "9")):
            return "bj" + code
    if len(code) == 8 and code[:2] in ("sh", "sz", "bj"):
        return code
    raise ValueError(f"无法识别的股票代码: {code}")
