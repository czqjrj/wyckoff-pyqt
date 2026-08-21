# -*- coding: utf-8 -*-
"""AI 问股 (多轮对话解读层): 在 interpret.py "单次报告解读"之上的升级形态。

与既有模块的分工:
  - interpret.py = 翻译官: 一份报告 → 一段解读, 单向无状态;
  - falsify.py   = 反方: 唱反调复核阶段判断;
  - ai_chat.py   = 助理: 保留多轮对话上下文, 并把当前标的的历史信号实证
    (signal_accuracy 信号库) 注入系统提示, 追问"为什么看多/换个周期呢"
    时无需重新生成整份解读, 且模型"有据可答"。

防幻觉约束 (与 interpret/falsify 同一套工程标准):
  - 系统提示硬性要求: 只基于注入的报告与统计回答, 缺数据就明说,
    不编造价位/事件/胜率;
  - 复用 _is_degenerate / _clean_text 过滤退化输出, 失败不入历史;
  - 无 Key / 网络失败 → ask 返回 None 优雅降级, 不影响离线分析。
"""
from .config import event_dir, vsa_dir
from .interpret import _chat_text, _get, _is_degenerate, llm_client

# 对话窗口: system 提示 + 最近 MAX_TURNS 轮 (user+assistant), 控制 token 成本
MAX_TURNS = 12
MAX_CONTEXT_CHARS = 24000

_SYSTEM_TMPL = """你是威科夫量价分析助手。用户正在查看一只股票的分析报告, 会就报告内容多轮追问。
回答规则 (必须遵守):
1. 只依据下方"分析报告"与"历史信号实证"回答; 报告里没有的数据 (价位/事件/日期) 一律明说"报告中未提供", 严禁编造。
2. 引用历史胜率时注明样本数 n; n<10 时必须提示"样本较少仅供参考"。
3. A股单边做多市场: 偏空判断的建议只能落在 减仓/离场/回避/不追高, 严禁做空类操作建议。
4. 回答用简体中文, 直接给正文, 不要 markdown 标题和序号; 默认 200~500 字, 用户要求详实时再展开。
5. 用户追问时结合此前对话上下文连贯作答, 不要每次都从头复述整份报告。

【分析报告】
{report}

【历史信号实证】
{stats}"""


def symbol_signal_stats(code, horizon=20, max_lines=40):
    """从本机信号准确度库汇总该标的的历史实证 (按 类别×类型 聚合)。

    方向化口径与 signal_accuracy.signal_stats 一致:
    多头/中性事件 ret>0 记命中, 空头事件 ret<0 记命中 (VSA 用 vsa_dir)。
    无记录返回空串 (系统提示中显示为"暂无")。
    """
    try:
        from .signal_accuracy import load_signals
        records = load_signals()
    except Exception:
        return ""
    code = str(code or "").strip()
    if not code:
        return ""
    digits = "".join(ch for ch in code if ch.isdigit())
    agg = {}
    for r in records or []:
        rec_code = str(r.get("code", ""))
        rec_sym = str(r.get("symbol", ""))
        if rec_code != code and not (digits and rec_sym.endswith(digits)):
            continue
        ret = ((r.get("results") or {}).get(str(horizon)) or {}).get("ret")
        if not isinstance(ret, (int, float)):
            continue
        kind = r.get("kind") or "event"
        t = r.get("type") or "?"
        d = event_dir(t) if kind == "event" else vsa_dir(t)
        hit = (ret > 0) if d >= 0 else (ret < 0)
        a = agg.setdefault((kind, t), [0, 0, 0.0])
        a[0] += int(hit)
        a[1] += 1
        a[2] += float(ret)
    if not agg:
        return ""
    lines = [f"(20根K线方向化口径, 共 {sum(a[1] for a in agg.values())} 条已评估)"]
    for (kind, t), (w, n, rs) in sorted(agg.items(), key=lambda kv: -kv[1][1])[:max_lines]:
        lines.append(f"- {kind}/{t}: n={n}, 命中率={w / n * 100:.0f}%, "
                     f"平均涨跌={rs / n * 100:+.1f}%")
    return "\n".join(lines)


def build_system_context(report_text, stats_text):
    """组装系统提示: 角色 + 防幻觉规则 + 报告 + 该标的历史实证。"""
    report = (report_text or "").strip()
    if len(report) > MAX_CONTEXT_CHARS:
        report = report[:MAX_CONTEXT_CHARS] + "\n...(报告过长已截断)"
    return _SYSTEM_TMPL.format(report=report,
                               stats=(stats_text or "").strip() or "无 (本机信号库中暂无该标的的历史记录)")


class ChatSession:
    """单只标的的多轮对话会话 (无 Key / 未配置 → ok=False, ask 恒 None)。"""

    def __init__(self, settings, system_context=""):
        self.settings = settings
        self._client = llm_client(settings, require_enabled=False)
        self._model = _get(settings, "ai_model", "deepseek-chat")
        ctx = (system_context or "").strip()
        self.messages = []
        if self._client is not None and ctx:
            self.messages.append({"role": "system", "content": ctx})
        self.ok = bool(self.messages)

    def ask(self, question, max_tokens=2000):
        """追加提问并返回回答 str; 失败/退化返回 None (不污染历史)。"""
        q = (question or "").strip()
        if not q or not self.ok:
            return None
        ans = _chat_text(self._client, self._window() + [{"role": "user", "content": q}],
                         self._model, max_tokens=max_tokens)
        if _is_degenerate(ans):
            return None
        self.messages.append({"role": "user", "content": q})
        self.messages.append({"role": "assistant", "content": ans})
        return ans

    def _window(self):
        """system 全保留 + 最近 MAX_TURNS 轮, 防长对话 token 膨胀。"""
        sysm = [m for m in self.messages if m["role"] == "system"]
        rest = [m for m in self.messages if m["role"] != "system"]
        return sysm + rest[-MAX_TURNS * 2:]

    def reset(self):
        """清空对话但保留 system 上下文。"""
        self.messages = self.messages[:1]
