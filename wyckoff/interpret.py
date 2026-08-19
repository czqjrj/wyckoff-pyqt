# -*- coding: utf-8 -*-
"""AI 报告解读 (可选): 把完整的威科夫分析报告发给 DeepSeek / OpenAI 兼容接口,
让大模型从"普通投资者能看懂"的视角解读报告含义, 输出通俗化的要点解读。

- 未配置 API Key / 未启用 / 网络失败 → 全部优雅降级返回 None, 不影响离线分析。
- 与 AI 证伪 (falsify.py) 相互独立: 证伪是"唱反调"复核阶段判断, 解读是
  "翻译"整份报告给用户看。二者共用同一套 API Key/Base/Model 设置。
"""
import re
import time

try:
    from openai import OpenAI
    _OPENAI_AVAILABLE = True
except Exception:  # pragma: no cover
    _OPENAI_AVAILABLE = False

# AI 请求超时 (秒): 端点卡住时在 60s 内失败, 而不是默认 ~600s 挂死后台线程
_AI_TIMEOUT = 60


def _get(s, key, default):
    try:
        return s.get(key, default)
    except AttributeError:
        return default


def llm_client(settings, require_enabled=True):
    """构造 OpenAI 兼容客户端; 不可用返回 None。

    require_enabled=True (默认): 需 `ai_interpret_enabled` 开关打开 (用于分析
    流水线自动解读, 防止未确认就消耗 token)。
    require_enabled=False: 仅需 API Key 即可 (用于用户显式点击的『生成 AI 解读』,
    只要有 Key 就能用, 不再被自动解读开关误卡)。
    """
    if not _OPENAI_AVAILABLE:
        return None
    key = _get(settings, "ai_api_key", "")
    if not key:
        return None
    if require_enabled and not _get(settings, "ai_interpret_enabled", False):
        return None
    try:
        return OpenAI(api_key=key,
                      base_url=_get(settings, "ai_api_base", "https://api.deepseek.com"),
                      timeout=_AI_TIMEOUT)
    except Exception:
        return None


def _clean_text(text):
    """去掉 markdown 代码围栏等包裹, 只保留正文。"""
    text = (text or "").strip()
    if text.startswith("```"):
        end = text.rfind("```")
        if end > 3:
            inner = text[3:end].lstrip()
            first = inner.split("\n", 1)[0].strip()
            if first in ("text", "txt", "markdown", "plaintext"):
                inner = inner.split("\n", 1)[1] if "\n" in inner else ""
            return inner.strip()
    return text


def _is_degenerate(text):
    """判定空响应 / 模板回显: 过短、几乎无中文、或只有连续【】标签行 → 视为失败。"""
    text = (text or "").strip()
    if len(text) < 20:
        return True
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if cjk < 10:
        return True
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines and all(ln.strip().startswith("【") and ln.strip().endswith("】")
                     for ln in lines):
        return True
    return False


# ── 操作自洽检测: 揪出"做多方向价位颠倒"的解读 (A股只能做多) ──
_ENTRY_RE = re.compile(
    r"(?:回踩|低吸|试探|入场|介入|轻仓|建仓|买点|激进者)[^。；;\n]{0,10}?"
    r"([0-9]+(?:\.[0-9]{1,2})?)")
_STOP_RE = re.compile(r"止损[^0-9]{0,8}?([0-9]+(?:\.[0-9]{1,2})?)")


def _contradictory_plan(text):
    """判定解读里出现做多方向自相矛盾的价位: 止损价高于入场价。

    仅当同一文本中 入场类动作后出现价格 且 止损后出现价格, 且 止损 > 入场 时判定矛盾。
    这是 A股单边做多市场的硬伤 (多头操作必须 止损 < 入场 < 目标)。
    检测不到 (缺任一数字) 一律返回 False, 只兜底不误伤。
    """
    text = text or ""
    if not text:
        return False
    m_e = _ENTRY_RE.search(text)
    m_s = _STOP_RE.search(text)
    if not m_e or not m_s:
        return False
    try:
        return float(m_s.group(1)) > float(m_e.group(1))
    except (TypeError, ValueError):
        return False


def _chat_text(client, messages, model, max_tokens=2500, temperature=0.5, retries=2):
    """调用并返回纯文本回答; 失败返回 None。"""
    for attempt in range(retries):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages,
                max_tokens=max_tokens, temperature=temperature,
                timeout=_AI_TIMEOUT)
            content = resp.choices[0].message.content or ""
            return _clean_text(content)
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


def interpret_report(report_text, settings, max_chars=30000, min_len=300):
    """把分析报告发给大模型解读, 返回解读文本 str; 不可用/失败返回 None。

    报告超长时截断 (仅截输入, 保证成本可控); 返回结果供界面以 'AI解读' 节展示。
    模型若只回一句话 (如 <min_len 字), 会用"太简短"提示再追问一次, 尽量拿足
    600~900 字的详实解读; 两次都失败/过短则返回较长的一次。
    若解读里出现做多方向自相矛盾的价位 (如 止损高于入场、目标低于入场),
    会用"操作自洽"提示追问一次, 再取不矛盾的那稿。
    """
    client = llm_client(settings)
    if client is None:
        return None
    model = _get(settings, "ai_model", "deepseek-chat")
    report = (report_text or "").strip()
    if len(report) > max_chars:
        report = report[:max_chars] + "\n...(报告过长已截断)"
    prompt = _INTERPRET_PROMPT.format(report=report)
    result = _chat_text(client, [{"role": "user", "content": prompt}], model)
    if _is_degenerate(result):
        return None
    if _char_len(result) < min_len:
        nudge = ("你刚才的解读只有寥寥几句, 太简短了。用户期望一篇详实的解读, "
                 "请严格按上面第 1~5 点的要求, 重新输出完整、自然连贯的段落 "
                 "(全篇 600~900 字), 直接给正文, 不要标题/序号/markdown。")
        messages = [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": result},
                    {"role": "user", "content": nudge}]
        retried = _chat_text(client, messages, model)
        if retried and not _is_degenerate(retried) and _char_len(retried) > _char_len(result):
            result = retried
    elif _contradictory_plan(result):
        nudge = ("你上面操作参考里的价位自相矛盾: 这是 A股单边做多市场, 任何多头操作都必须是 "
                 "'止损价 < 入场价 < 目标价' (止损设在入场价下方、目标在入场价上方)。请检查并"
                 "纠正: 要么以报告'交易计划'一节给出的 现价/止损/目标 为准复述, 要么只做定性描述,"
                 " 不要自己另造一套互相矛盾的价位。重新输出全文, 直接给正文。")
        messages = [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": result},
                    {"role": "user", "content": nudge}]
        retried = _chat_text(client, messages, model)
        if retried and not _is_degenerate(retried):
            if not _contradictory_plan(retried):
                result = retried
            elif _char_len(retried) > _char_len(result):
                result = retried
    return result


def interpret_prompt(prompt, settings, min_len=150, max_tokens=1800, temperature=0.5,
                     require_enabled=True):
    """用自定义 prompt 调用大模型, 返回解读文本 str 或 None (未配置/失败/过短)。

    供各类工具窗口 (国家队持仓透视 / ETF三因子监测 等) 的"AI解读"复用。
    模型若只回一句话, 会追问一次拿更详实的回答。
    require_enabled=False: 用户显式点击生成时, 只要有 API Key 即可调用
    (不依赖 ai_interpret_enabled 自动解读开关)。
    """
    client = llm_client(settings, require_enabled=require_enabled)
    if client is None:
        return None
    model = _get(settings, "ai_model", "deepseek-chat")
    result = _chat_text(client, [{"role": "user", "content": prompt}], model,
                        max_tokens=max_tokens, temperature=temperature)
    if _is_degenerate(result):
        return None
    if _char_len(result) < min_len:
        nudge = ("解读太简短了。请严格按上面要求重新输出完整、自然连贯的段落, "
                 "直接给正文, 不要标题/序号/markdown。")
        messages = [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": result},
                    {"role": "user", "content": nudge}]
        retried = _chat_text(client, messages, model, max_tokens=max_tokens,
                             temperature=temperature)
        if retried and not _is_degenerate(retried) and _char_len(retried) > _char_len(result):
            result = retried
    return result


def _char_len(text):
    """正文有效字数 (去掉空白, 中英文均计)。"""
    return len("".join((text or "").split()))


def interpret_tag(label, settings, context=None, min_len=120, max_tokens=1200,
                  temperature=0.5, require_enabled=True):
    """对单个 VSA/Wyckoff 标签做 AI 解读, 面向 A股单边做多市场。

    label:   标签名 (SC/BC/SPR/UT/Spring/UTAD/SOS/JOC 等, vsa.py + events.py 全集);
    context: 可选的当前分析语境文本 (股票 + 最近K线 + 该标签近期出现), 由界面提供,
             缺省仅基于静态解释;
    settings: 与 interpret_report 相同的 AI 配置 (未启用/无 Key 时优雅返回 None)。
    require_enabled=False: 用户显式点击时只要有 Key 即可调用。

    解读硬性约束: A股只能做多不能做空 → 偏空信号的建议只落在
    减仓/离场/回避/不追高/不接飞刀 等动作, 严禁出现做空、开空仓、放空、空头回补等指令。
    """
    from .vsa_explain import explain_lines, VSA_EXPLAIN, EVENT_EXPLAIN
    if label not in VSA_EXPLAIN and label not in EVENT_EXPLAIN:
        return None
    base = "\n".join(explain_lines(label))
    ctx = (context or "").strip()
    prompt = _TAG_INTERPRET_PROMPT.format(
        label=label, base=base,
        context=ctx if ctx else "无 (仅基于该信号的静态解释, 未提供本次 K 线语境)")
    return interpret_prompt(prompt, settings, min_len=min_len,
                            max_tokens=max_tokens, temperature=temperature,
                            require_enabled=require_enabled)


_TAG_INTERPRET_PROMPT = """你是一名资深的威科夫(Wyckoff)分析师, 请针对股票分析工具在 K 线图上标注的
单个量价信号标签, 给普通 A股投资者写一段通俗解读。

# 铁律
A股是单边做多市场, 只能做多、不能做空 (无融券做空渠道)。因此:
- 任何偏空信号 (UT/BC/UPT/TRU/SUP/ETF/UTAD/BC/ER 等) 的建议必须落在
  "减仓/离场/回避/不追高/不接飞刀/暂停买入" 这些可执行动作上, 严禁出现
  做空、开空仓、放空、空头可入场、空头回补、裸卖空 等任何做空指令;
- 偏多信号 (SC/NS/SPR/Spring/TRD/TEST/ST/SOS/JOC/DEM/LPS 等) 落到
  "低吸/分批介入/持有/加仓" 等动作, 并强调关键止损位。

# 标签与静态解释
标签: {label}
{base}

# 当前K线语境 (来自本次分析)
{context}

# 解读要求
- 第一句直接说: 该标签此刻的含义 + 偏多/偏空/中性 + 在 A股单边市场下的操作倾向。
- 然后说明它出现的意义、需要观察的确认/证伪信号, 以及和当前 K线/阶段的关系。
- 结尾给出一条明确的 A股可执行操作参考 (入场/持有/减仓/观望 + 关键价位或止损思路),
  并注明"仅供参考, 不构成投资建议"。
- 若给出多头入场与止损, 必须满足 止损价 < 入场价 < 目标价, 数字只能来自语境中的
  真实价位, 语境没给的用"前低/前高/区间上沿/0.382回撤"等定性指代, 严禁编造。
- 自然连贯的段落, 120~300 字, 不要标题、序号、markdown 标记或【】标签。
- 不编造语境里不存在的数字; 语境未提供的内容只定性描述。
"""


_INTERPRET_PROMPT = """你是一名资深的威科夫(Wyckoff)分析师, 负责把专业分析报告解读成
普通投资者能看懂的通俗中文解读。请基于下面这份完整的威科夫分析报告写一篇详实的解读。

# A股铁律
A股是单边做多市场, 只能做多、不能做空。偏空信号的建议只能落在
减仓/离场/回避/不追高/不接飞刀 等动作上, 严禁出现 做空、开空仓、放空、空头回补
等任何做空指令。

# 解读必须覆盖以下内容 (按此顺序写成自然段落)
1. 当前阶段与多空倾向: 第一段先一句话说结论 (阶段 + 偏多/偏空/中性)。
2. 核心依据: 具体引用报告中的威科夫事件 (Spring/ST/JOC/SOS/SC/UTAD/BC 等)、
   阶段、量价关系、供需比、VSA 信号、均线、资金流, 尽量带上具体数字,
   说明支撑多空判断的关键证据。
3. 目标与空间: 结合目标价、P&F 点数图目标、支撑/阻力、交易区间, 说明上行/下行
   空间有多大, 是否已到位。
4. 风险与疑虑: 逐条说明最值得注意的风险点及其原因, 包括资金背离、反面证据积分、
   阶段存疑、稳健性脆弱、AI 证伪结论、仓位提示、数据质量等。
5. 操作参考: 以报告"交易计划"一节中的 方向/现价/止损/目标1/目标2/盈亏比/仓位参考
   为唯一基准, 用通俗话复述即可, 不要另造一套价位。若报告方向是"观望", 主基调必须是
   观望, 至多说明"满足什么触发条件后才考虑" (如放量突破某价位、缩量回踩某支撑不破),
   不得给出无条件的具体入场; 若报告给出多头计划, 复述其 入场/止损/目标/盈亏比 并
   强调止损纪律。
   若报告方向是"空头/减仓": 这只代表持仓者的减仓/离场指引 (A股不能做空,
   报告不会也绝不能给出做空交易计划), 解读必须落在 逢高减仓/控制仓位/离场/回避追高
   等动作上, 复述其上空确认位与下方回踩支撑参考即可; 严禁把 止损/目标/盈亏比 套在
   该方向上当作可执行空单, 严禁出现做空指令, 也不得自行编造该方向的 止损/目标/盈亏比
   数字来讨论"盈亏比不足"。最后注明"仅供参考, 不构成投资建议"。

# 硬性自洽要求 (输出前必须自查)
- 全文所有价格必须来自报告原文 (交易计划、支撑/阻力、目标价、前高/前低、区间上下沿等),
  严禁自行编造数字; 报告没给的价位用"前低/前高/区间上沿/0.382回撤"等定性指代。
- A股只能做多: 任何多头操作都必须满足 止损价 < 入场价 < 目标价; 绝不允许出现
  "止损高于入场"或"目标低于入场"这类倒置数字。
- "空头/减仓"方向下不得输出任何以 止损/目标/盈亏比 表述的开仓/空单方案。
- 结论基调与操作建议必须一致: 偏多→低吸/持有, 偏空→减仓/回避, 观望→不给具体入场,
  减仓→逢高了结/控制仓位。
- 同一段内的价格、盈亏比、仓位逻辑必须互相自洽, 不得前后矛盾。

# 要求
- 写成自然连贯的段落 (不要输出任何标题、序号、markdown 标记或【】标签)。
- 全篇 600~900 字, 具体有据, 不空泛, 不重复报告原文。
- 口语化但保持专业准确, 把威科夫术语用通俗话解释清楚。
- 观点中立客观, 标注不确定性。
- 直接输出正文, 不要开场白或结束语。

# 分析报告
{report}
"""
