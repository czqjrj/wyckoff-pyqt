# -*- coding: utf-8 -*-
"""AI 反向证伪 (可选): 调用 DeepSeek / OpenAI 兼容接口, 以"唱反调"方式
检验当前阶段/结构判断是否成立。

借鉴 WyckoffPro FalsificationEngine 的核心理念: 让大模型专门扮演"找茬"角色,
尽力推翻当前判断, 而不是顺着说好话。输出供人复核, 不直接改结论。

- 未配置 API Key / 未启用 / 网络失败 → 全部优雅降级返回 None, 不影响离线分析。
- 内置调用冷却, 同一次分析内只发一次证伪请求, 控制成本。
"""
import json
import time

try:
    from openai import OpenAI
    _OPENAI_AVAILABLE = True
except Exception:  # pragma: no cover
    _OPENAI_AVAILABLE = False

# AI 请求超时 (秒): 端点卡住时在 60s 内失败, 避免后台线程长时间挂死
_AI_TIMEOUT = 60


def _get(s, key, default):
    try:
        return s.get(key, default)
    except AttributeError:
        return default


def llm_client(settings):
    """构造 OpenAI 兼容客户端; 不可用返回 None。"""
    if not _OPENAI_AVAILABLE:
        return None
    enabled = _get(settings, "ai_falsify_enabled", False)
    key = _get(settings, "ai_api_key", "")
    if not enabled or not key:
        return None
    try:
        return OpenAI(api_key=key,
                      base_url=_get(settings, "ai_api_base", "https://api.deepseek.com"),
                      timeout=_AI_TIMEOUT)
    except Exception:
        return None


def _chat_json(client, messages, model, max_tokens=1200, temperature=0.1, retries=2):
    """调用并解析 JSON; 失败返回 None。"""
    for attempt in range(retries):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages,
                max_tokens=max_tokens, temperature=temperature,
                timeout=_AI_TIMEOUT)
            content = resp.choices[0].message.content
            return _parse_json(content)
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


def _parse_json(text):
    text = (text or "").strip()
    if "```json" in text:
        start = text.find("```json") + 7
        end = text.rfind("```")
        text = text[start:end].strip()
    elif "```" in text:
        start = text.find("```") + 3
        end = text.rfind("```")
        text = text[start:end].strip()
    try:
        return json.loads(text)
    except Exception:
        s, e = text.find("{"), text.rfind("}")
        if 0 <= s < e:
            try:
                return json.loads(text[s:e + 1])
            except Exception:
                pass
    return None


def _kline_table(df, rows=14):
    seg = df.tail(rows)
    lines = []
    for _, r in seg.iterrows():
        lines.append(f"{r['day'].date()} O{r['open']:.2f} H{r['high']:.2f} "
                     f"L{r['low']:.2f} C{r['close']:.2f} V{r['volume']:.0f}")
    return "\n".join(lines)


def _events_str(events, df, n=8):
    from .config import W_PIVOT_LONG
    recent = [e for e in events if e["idx"] >= len(df) - W_PIVOT_LONG][-n:]
    if not recent:
        return "(近期无威科夫事件)"
    return " | ".join(f"{e['date'].date()} {e['type']}"
                      f"@{e['price']:.2f}" for e in recent)


def falsify_structure(df, events, phase_label=None, structure=None,
                      pnf_t=None, settings=None):
    """单次"唱反调"证伪请求。

    返回 None (不可用/失败) 或:
      result            FAILED(假设成立) / SUCCEEDED(被推翻) / PARTIAL
      confidence        证伪置信度 0-100
      violated          [条件...] 被违反的假设必要条件
      alternative       替代假设 {phase, reasoning, confidence}
      assessment        总结
      advice_gate       PASS / DOWNGRADE / BLOCK
      model             实际使用的模型
    """
    client = llm_client(settings)
    if client is None:
        return None
    model = _get(settings, "ai_model", "deepseek-chat")
    phase_txt = phase_label or (structure[2].splitlines()[0] if structure and len(structure) >= 3 else "未知")
    tr_txt = ""
    if pnf_t and pnf_t.get("tr_top") and pnf_t.get("tr_bottom"):
        tr_txt = (f"交易区间 {pnf_t['tr_bottom']:.2f} ~ {pnf_t['tr_top']:.2f}"
                  f", 方向 {pnf_t.get('direction', 'range')}")
    prompt = _FALSIFY_PROMPT.format(
        phase=phase_txt,
        events=_events_str(events, df),
        tr=tr_txt,
        kline=_kline_table(df),
    )
    data = _chat_json(client, [{"role": "user", "content": prompt}], model)
    if not data:
        return None
    result = data.get("falsification_result", "").upper()
    gate = "PASS"
    if result == "SUCCEEDED":
        gate = "BLOCK" if _has_critical(data) else "DOWNGRADE"
    return {
        "result": result if result in ("FAILED", "SUCCEEDED", "PARTIAL") else "PARTIAL",
        "confidence": data.get("confidence_in_falsification", 50),
        "violated": data.get("violated_conditions", []),
        "alternative": data.get("alternative_hypothesis", {}),
        "assessment": data.get("overall_assessment", ""),
        "advice_gate": gate,
        "model": model,
    }


def _has_critical(data):
    return any(v.get("severity") == "CRITICAL" for v in data.get("violated_conditions", []))


def fal_lines(fal):
    """AI 证伪结果 → 结论区文本行。"""
    if not fal:
        return ["  (未启用 AI 证伪: 设置中填写 DeepSeek API Key 后可用)"]
    r_cn = {"FAILED": "未推翻(假设成立)", "SUCCEEDED": "被推翻(假设存疑)",
            "PARTIAL": "部分存疑"}
    lines = [f"  判定: {r_cn.get(fal['result'], fal['result'])} "
             f"(证伪置信 {fal['confidence']}%)  门控: {fal['advice_gate']}"]
    for v in fal.get("violated", [])[:4]:
        lines.append(f"    ✗ {v.get('condition', '')} — "
                     f"期望 {v.get('expected', '?')}, 实际 {v.get('actual', '?')} "
                     f"[{v.get('severity', 'MINOR')}]")
    alt = fal.get("alternative") or {}
    if alt.get("phase"):
        lines.append(f"    替代假设: {alt['phase']} (置信 {alt.get('confidence', 0)}%) "
                     f"— {alt.get('reasoning', '')}")
    if fal.get("assessment"):
        lines.append(f"    总结: {fal['assessment']}")
    return lines


_FALSIFY_PROMPT = """你是专门被雇来"唱反调"的威科夫高级分析师。你的任务是竭尽全力
寻找证据来推翻下面这条结构判断, 而不是确认它。因为成功推翻错误判断而获得奖励。

# 当前结构判断 (需要你尝试推翻)
{phase}
威科夫近期事件: {events}
{tr}

# 最近K线 (日线 OHLCV)
{kline}

# A股市场约束 (必须遵守)
本工具面向 A股 (单边做多市场, 只能做多、不能做空)。你推翻/确认结构判断时,
只允许输出 阶段/结构 层面的判断 (如"吸筹/派发/趋势"等) 与量价证据;
凡涉及操作倾向的表述一律落在 减仓/离场/回避/不追高/不接飞刀 等动作上,
严禁出现 做空、放空、开空仓、空头可入场、裸卖空、空头回补 等做空指令或做空方案。
alternative_hypothesis 的 reasoning 与 overall_assessment 同理。

# 步骤
1. 列出该判断成立必须具备的条件 (至少5条)。
2. 逐条对照K线数据检验, 重点: 量价关系是否支持? 有没有"应该出现却没出现"?
   有没有"不该出现却出现"的? 时间/幅度是否合理?
3. 如果判断是错的, 最可能的真实结构是什么?

# 输出严格 JSON (不要包含额外文字)
{{
  "falsification_result": "FAILED 或 SUCCEEDED 或 PARTIAL",
  "confidence_in_falsification": 0-100,
  "violated_conditions": [
    {{"condition": "...", "expected": "...", "actual": "...", "severity": "CRITICAL/MAJOR/MINOR"}}
  ],
  "alternative_hypothesis": {{"phase": "...", "reasoning": "...", "confidence": 0-100}},
  "overall_assessment": "一段话总结"
}}"""
