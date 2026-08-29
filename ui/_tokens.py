"""Design Tokens: 单一真相源，供 theme.py / QSS / 组件统一引用。"""
from __future__ import annotations

# ────────────────── Spacing (8px 基准网格) ──────────────────
SPACING = {
    "0": 0,
    "1": 4,
    "2": 8,
    "3": 12,
    "4": 16,
    "5": 20,
    "6": 24,
    "8": 32,
    "10": 40,
    "12": 48,
}

# ────────────────── Border Radius ──────────────────
RADIUS = {
    "none": 0,
    "sm": 4,   # 按钮/输入框/标签
    "md": 8,   # 卡片/面板/下拉菜单
    "lg": 12,  # 对话框/悬浮面板/工具提示
    "full": 9999,  # 圆形/胶囊
}

# ────────────────── Type Scale (pt / line-height) ──────────────────
# 基准字号 UI_FONT_SIZE=12pt 下的标称值; 运行时由 theme.apply_type_scale()
# 按用户设置的界面字号等比缩放 (就地更新本 dict, 所有引用同步生效)。
# (font-size, line-height)
TYPE_SCALE = {
    "display": (20, 28),    # 品牌/大标题
    "h1": (17, 24),         # 页面标题/统计大数字
    "h2": (14, 20),         # 面板标题/股票名
    "body": (12, 18),       # 正文基准
    "body-sm": (11, 16),    # 次要正文/表单
    "caption": (10, 14),    # 说明文字/提示
    "mini": (9, 12),        # 徽标/脚注/元信息
    "mono": (12, 18),       # 等宽数字
    "mono-sm": (10, 14),
}

# ────────────────── Semantic Colors (由 theme.py 运行时注入) ──────────────────
# 这里只定义键名，实际十六进制值在 ThemeManager.set_theme() 时写入
SEMANTIC_KEYS = [
    # Brand
    "brand", "brand-hover", "brand-pressed",
    # Surface 层级
    "surface-0", "surface-1", "surface-2", "surface-3", "surface-4",
    # Border
    "border", "border-strong", "border-focus",
    # Text
    "text-primary", "text-secondary", "text-muted", "text-inverse", "text-disabled",
    # State / Feedback
    "success", "success-hover", "success-bg",
    "warning", "warning-hover", "warning-bg",
    "error", "error-hover", "error-bg",
    "info", "info-hover", "info-bg",
    # Accent (主品牌色)
    "accent", "accent-hover", "accent-pressed", "accent-bg",
    # Chart 专用 (保留原有键名兼容)
    "up", "down", "amber", "muted",
    "zone-acc", "zone-dist", "zone-neut",
    "grid", "zebra", "header", "sel", "btn-hover",
]

# ────────────────── Shadow / Elevation ──────────────────
SHADOW = {
    "0": "none",
    "1": "0 1px 2px rgba(0,0,0,0.05)",
    "2": "0 4px 8px rgba(0,0,0,0.08)",
    "3": "0 8px 24px rgba(0,0,0,0.12)",
    "4": "0 16px 48px rgba(0,0,0,0.16)",
}

# ────────────────── Transition ──────────────────
TRANSITION = {
    "fast": "120ms ease",
    "normal": "200ms ease",
    "slow": "300ms ease",
}

# ────────────────── Z-Index ──────────────────
Z_INDEX = {
    "dropdown": 100,
    "sticky": 200,
    "modal": 300,
    "popover": 400,
    "tooltip": 500,
    "toast": 600,
}

# ────────────────── Breakpoints (响应式参考) ──────────────────
BREAKPOINTS = {
    "sm": 640,
    "md": 1024,
    "lg": 1280,
    "xl": 1536,
}

# ────────────────── 导出聚合 ──────────────────
TOKENS = {
    "spacing": SPACING,
    "radius": RADIUS,
    "type": TYPE_SCALE,
    "semantic": SEMANTIC_KEYS,
    "shadow": SHADOW,
    "transition": TRANSITION,
    "zIndex": Z_INDEX,
    "breakpoints": BREAKPOINTS,
}


def css_var(name: str) -> str:
    """生成 CSS 变量引用: var(--name)"""
    return f"var(--{name})"


def spacing(key: str) -> int:
    return SPACING.get(key, 0)


def radius(key: str) -> int:
    return RADIUS.get(key, 0)


def type_scale(key: str) -> tuple[int, int]:
    return TYPE_SCALE.get(key, TYPE_SCALE["body"])
