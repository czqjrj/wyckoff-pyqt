"""新闻情绪分析: 抓取个股公告/资讯/互动问答, 量化对个股的情绪影响,
并用 量价反应 与 威科夫事件 做二次验证 (effort-vs-result / 新闻-事件共振)。

数据源分级 (市场级与个股级严格分离):
  个股级 → 东财公告 np-anotice-stock + 东财个股资讯 np-listapi
           + 互动易问答 irm.cninfo.com.cn, 进入个股融合权重
  市场级 → 新浪滚动快讯 feed.mix.sina.com.cn + 新浪7x24快讯 zhibo.sina.com.cn,
           仅作大盘环境参考 (market_env)

打分: 关键词分级加权 (强事件 2.0 / 中 1.0~1.5 / 弱 0.5) × 时效衰减
(48h 半衰期) × 来源权重 × 公告类型系数 (例行公告降权)。长词优先消耗避免
子串重复计分; 否定前缀反转语义 ("终止重大资产重组"→利空), 条件前缀弱化
确定性 ("拟收购" 权重×0.55); 情绪分按累计证据强度缩放 (单弱词不再满格)。

验证层 (提升威科夫分析准确性):
  1) 价格反应验证 apply_price_validation: 利好后放量下跌=市场证伪(借利好出货)
     → 大幅降权; 同向运动/缩量整理=市场确认 → 小幅加权。威科夫 effort-vs-result
     原则的直接应用 —— 让"市场用真金白银投的票"修正关键词打分。
  2) 事件共振 event_resonance: 新闻方向与近期威科夫事件同向加分、背离减分;
     Spring/Shakeout 伴随利空=复合人恐吓筹码特征 (反向确认),
     UTAD/LPSY 伴随利好吹票=诱多嫌疑 (额外减分)。
  3) 前瞻日历 fetch_forward_calendar: 未来解禁/业绩预告/定期报告披露窗口,
     融合层对临近窗口的多头信号降置信 (事前排雷优于事后解释)。
"""
import difflib
import re
import time
from datetime import datetime, timedelta
from threading import Lock

import numpy as np
import pandas as pd

from ._shared import http_session


_NEWS_CACHE = {}
_NEWS_CACHE_TTL = 1800
_MARKET_NEWS_CACHE = {}
_NEWS_LOCK = Lock()

# ── 分级加权关键词: (词, 权重) ──
# 强事件 (±2.0): 直接改变基本面预期, 如立案/退市/重组
KEYWORDS_BULL = [
    ("业绩预增", 2.0), ("预增", 2.0), ("扭亏", 2.0), ("要约收购", 2.0),
    ("重大资产重组", 2.0), ("借壳", 2.0),
    ("中标", 1.5), ("回购", 1.5), ("增持", 1.5), ("注资", 1.5),
    ("并购", 1.5), ("重组", 1.5), ("签订合同", 1.5), ("业绩大增", 1.5),
    ("合作", 1.0), ("签约", 1.0), ("订单", 1.0), ("扩产", 1.0),
    ("专利", 1.0), ("技术突破", 1.0), ("政策利好", 1.0), ("定增", 1.0),
    ("纳入", 1.0), ("业绩预盈", 1.0), ("摘帽", 1.5),
    ("分红", 0.5), ("送转", 0.5), ("高送转", 1.0), ("补贴", 0.5),
    ("减税", 0.5), ("试点", 0.5), ("突破", 0.5), ("创新高", 1.0),
    ("独家", 0.5), ("龙头", 0.5), ("产能", 0.5), ("获批", 1.5),
]

KEYWORDS_BEAR = [
    ("立案调查", 2.0), ("立案", 2.0), ("退市", 2.0), ("*st", 2.0),
    ("＊st", 2.0),
    ("终止上市", 2.0), ("预亏", 2.0), ("违约", 2.0), ("商誉减值", 2.0),
    ("强制退市", 2.0), ("留置", 2.0),
    ("大股东减持", 1.5), ("高管减持", 1.5), ("减持", 1.5), ("处罚", 1.5),
    ("违规", 1.5), ("终止", 1.5), ("限售解禁", 1.5), ("解禁", 1.5),
    ("警示函", 1.5), ("监管函", 1.5), ("业绩大幅下滑", 1.5),
    ("st板块", 1.5), ("st股", 1.5), ("被st", 1.5), ("戴帽", 1.5),
    ("披星戴帽", 1.5),
    ("质押", 1.0), ("亏损", 1.0), ("下调", 1.0), ("风险提示", 1.0),
    ("关注函", 1.0), ("问询函", 1.0), ("诉讼", 1.0), ("仲裁", 1.0),
    ("资产减值", 1.0), ("解约", 1.0), ("业绩预减", 1.0), ("冻结", 1.0),
    ("流通盘扩大", 0.5), ("延期回复", 0.5),
]

# 长词优先排序, 匹配后从文本中消耗, 防止 "大股东减持" 同时命中 "减持" 等子串重复计分
_ALL_KEYWORDS = sorted(
    [(k, w, 1) for k, w in KEYWORDS_BULL] + [(k, w, -1) for k, w in KEYWORDS_BEAR],
    key=lambda x: -len(x[0]))

# ── 否定/条件前缀 (语义反转与确定性打折) ──
# 否定前缀: 出现在关键词前的动词使语义整体反转 (对多空词统一适用):
#   "终止重大资产重组"→利空; "取消减持计划"→利好。匹配时连带消耗前缀字符,
#   防止前缀本身的关键词 (终止/取消等) 再重复计分。
NEGATION_FLIP = ("终止", "取消", "撤回", "暂停", "停止", "失败", "告吹",
                 "搁置", "否决", "未获通过", "未通过", "不再")
# 条件/筹划前缀: 事项尚未落地, 确定性打折 (权重 × _CONDITIONAL_FACTOR):
#   "拟收购"/"筹划重大资产重组" ≠ 已完成事项。
CONDITIONAL_PREFIXES = ("拟", "筹划", "谋求", "商讨", "接洽", "传闻",
                        "预计", "或将", "有意")
_CONDITIONAL_FACTOR = 0.55

# 例行公告模式: 程序性/例行披露, 无增量信息 → 来源权重 × ROUTINE_ANN_FACTOR。
# 刻意不包含 业绩预告/问询函回复/中标/重组进展 等高信息量类型 (由关键词主导)。
ROUTINE_ANN_PAT = re.compile(
    r"(董事会|监事会|股东大会|职工代表大会).{0,14}决议"
    r"|股东大会.{0,8}通知"
    r"|会议纪要|会议记录"
    r"|审计报告|内部控制(审计|评价|规范)|独立董事.{0,8}(意见|事前认可)"
    r"|募集资金.{0,10}(存放|使用|置换)"
    r"|会计政策变更|会计差错更正"
    r"|英文版|摘要|补充公告|召开(临时)?(董事会|监事会|股东大会)"
)
ROUTINE_ANN_FACTOR = 0.45

# 来源权威度: 交易所公告 > 媒体资讯 > 互动问答
SOURCE_WEIGHT = {"eastmoney_ann": 1.3, "em_stock_news": 1.0, "irm_qa": 0.85}

# 时效半衰期 (小时): 48h 前的新闻权重减半
_HALF_LIFE_H = 48.0

SECTOR_KEYWORDS = {
    "AI": ["人工智能", "大模型", "算力", "gpu", "芯片", "半导体", "算力租赁"],
    "新能源": ["光伏", "锂电", "储能", "新能源", "电池", "充电桩"],
    "医药": ["创新药", "cxo", "医疗器械", "集采", "医保", "glp-1"],
    "军工": ["军工", "国防", "航天", "卫星", "低空经济"],
    "消费": ["白酒", "食品饮料", "家电", "纺织", "零售"],
    "金融": ["银行", "保险", "券商", "信托", "资管"],
    "地产": ["房地产", "建筑", "水泥", "建材", "城投"],
}


def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_routine_ann(title: str, source: str = "") -> bool:
    """例行公告判定 (仅对交易所公告源生效): 决议/通知/纪要类程序性披露。"""
    if source and source != "eastmoney_ann":
        return False
    return bool(ROUTINE_ANN_PAT.search(str(title or "")))


def _score_text(text: str) -> float:
    """分级加权关键词打分: -1~1, 方向×强度双编码。

    长词优先并按字符区间消耗已匹配片段 (防止子串重复计分);
    否定前缀反转语义并连带消耗前缀; 条件前缀权重打折;
    最终分按累计证据强度缩放 ((bull+bear)/2 饱和): 单个弱词只产生小分值,
    多条同向/强关键词才接近满格 —— 比旧口径 (单词即 ±1) 抗噪得多。
    """
    low = text.lower()
    used = []
    bull = bear = 0.0

    def _overlap(a, b):
        return max(0, min(a[1], b[1]) - max(a[0], b[0]))

    for k, w, pol in _ALL_KEYWORDS:
        for m in re.finditer(re.escape(k), low):
            s0, s1 = m.span()
            if any(_overlap((s0, s1), u) for u in used):
                continue
            ww, pp, span0 = float(w), pol, s0
            pre = low[max(0, s0 - 4):s0]
            flip = next((f for f in NEGATION_FLIP if pre.endswith(f)), None)
            if flip is not None:
                pp = -pol                      # 语义反转
                span0 = s0 - len(flip)         # 连带消耗否定前缀
            elif any(pre.endswith(c) for c in CONDITIONAL_PREFIXES):
                ww = w * _CONDITIONAL_FACTOR   # 筹划中: 确定性打折
            used.append((span0, s1))
            if pp > 0:
                bull += ww
            else:
                bear += ww
    total = bull + bear
    if total == 0:
        return 0.0
    direction = (bull - bear) / total
    intensity = min(1.0, total / 2.0)          # 证据强度饱和 (2.0 权重即满格)
    return round(max(-1.0, min(1.0, direction * intensity)), 4)


def _extract_sector(text: str) -> list[str]:
    text = text.lower()
    return [sec for sec, kws in SECTOR_KEYWORDS.items() if any(k in text for k in kws)]


# ─────────────────── 近似去重 ───────────────────

_TITLE_NORM_PAT = re.compile(r"[\W\s_]+", re.UNICODE)


def _norm_title(t: str) -> str:
    return _TITLE_NORM_PAT.sub("", str(t or "")).lower()


def dedup_news(news: list[dict], window_h: float = 36.0, ratio: float = 0.62) -> list[dict]:
    """近似去重: 同一事件的公告+多源资讯报道只保留权威度最高的一条。

    规则: 时间窗 window_h 内, 标题规范化后相似度 ≥ ratio 或互相包含 → 合并。
    保留 SOURCE_WEIGHT 更高者 (同权保留更早者), 被合并条的正文互补进代表条,
    并累计 dup_count (展示时可标 "x源报道")。"""
    kept: list[dict] = []
    for n in sorted(news, key=lambda x: (
            -SOURCE_WEIGHT.get(x.get("source"), 1.0), x.get("datetime"))):
        t = _norm_title(n.get("title", ""))
        dup = None
        for m in kept:
            try:
                gap = abs((n["datetime"] - m["datetime"]).total_seconds())
            except Exception:
                continue
            if gap > window_h * 3600.0:
                continue
            mt = _norm_title(m.get("title", ""))
            if not t or not mt:
                continue
            if t in mt or mt in t or \
                    difflib.SequenceMatcher(None, t, mt).ratio() >= ratio:
                dup = m
                break
        if dup is None:
            kept.append(dict(n))
        else:
            dup["dup_count"] = int(dup.get("dup_count", 1)) + 1
            if not dup.get("content") and n.get("content"):
                dup["content"] = n["content"]
    # 恢复时间倒序 (调用方按新到旧消费)
    kept.sort(key=lambda x: x["datetime"], reverse=True)
    return kept


class NewsSentiment:
    """统一新闻情绪接口: 个股级 (公告+资讯+互动易) 与市场级 (滚动+7x24快讯) 分离,
    外加价格反应验证 / 事件共振 / 前瞻日历三个威科夫验证层。"""

    def __init__(self):
        self.session = http_session()

    # ─────────────────── 个股级数据源 ─────────────────--

    def fetch_eastmoney_announcements(self, symbol: str, days: int = 7) -> list[dict]:
        """东方财富个股公告 (np-anotice-stock 端点, 支持沪深)"""
        code = symbol[-6:]
        url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
        params = {
            "sr": "-1", "page_size": "50", "page_index": "1",
            "ann_type": "A", "client_source": "web",
            "stock_list": code,
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
        try:
            r = self.session.get(url, params=params, headers=headers, timeout=10)
            items = ((r.json().get("data") or {}).get("list")) or []
        except Exception:
            return []
        out = []
        cutoff = pd.Timestamp.now().normalize() - timedelta(days=days)
        for it in items:
            pub_date = it.get("notice_date") or it.get("display_time")
            if not pub_date:
                continue
            try:
                dt = pd.to_datetime(pub_date)
            except Exception:
                continue
            if dt < cutoff:
                continue
            art_code = it.get("art_code") or ""
            out.append({
                "source": "eastmoney_ann",
                "title": _clean_html(it.get("title", "")),
                "content": "",
                "datetime": dt,
                "art_code": art_code,
                "url": f"https://data.eastmoney.com/notices/detail/{art_code}.html" if art_code else "",
            })
        return out

    def fetch_ann_content(self, art_code: str, max_chars: int = 600) -> str:
        """东财公告正文 (np-cnotice-stock 内容端点)。失败返回空串。

        只对标题打分会漏掉正文里的关键信息 (如问询函回复中的具体说明),
        对最近几条公告抓正文可显著提升打分覆盖面。"""
        if not art_code:
            return ""
        url = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
        params = {"art_code": art_code, "client_source": "web", "page_index": "1"}
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
        try:
            r = self.session.get(url, params=params, headers=headers, timeout=6)
            data = (r.json().get("data") or {})
            txt = data.get("notice_content") or ""
        except Exception:
            return ""
        return _clean_html(txt)[:max_chars]

    def fetch_em_stock_news(self, symbol: str, days: int = 7) -> list[dict]:
        """东方财富个股资讯流 (np-listapi, 按股票代码过滤的真实个股新闻)"""
        code = symbol[-6:]
        mkt = "1" if symbol.lower().startswith("sh") else "0"
        url = "https://np-listapi.eastmoney.com/comm/web/getListInfo"
        params = {
            "client": "web", "mTypeAndCode": f"{mkt}.{code}",
            "type": "1", "pageSize": "50", "req_trace": "1",
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://so.eastmoney.com/"}
        try:
            r = self.session.get(url, params=params, headers=headers, timeout=10)
            items = ((r.json().get("data") or {}).get("list")) or []
        except Exception:
            return []
        out = []
        cutoff = pd.Timestamp.now() - timedelta(days=days)
        for it in items:
            title = _clean_html(it.get("Art_Title", ""))
            show_time = it.get("Art_ShowTime", "")
            if not title or not show_time:
                continue
            try:
                dt = pd.to_datetime(show_time)
            except Exception:
                continue
            if dt < cutoff:
                continue
            out.append({
                "source": "em_stock_news",
                "title": title,
                "content": "",
                "datetime": dt,
                "url": it.get("Art_Url", ""),
            })
        return out

    def fetch_irm_qa(self, symbol: str, days: int = 30, limit: int = 20) -> list[dict]:
        """互动易投资者问答 (irm.cninfo.com.cn, 深沪合并入口) — 个股级 best-effort。

        公司在问答中的官方回复常先于公告确认/否认经营传闻, 对验证技术信号背后
        的基本面动因很有价值; 接口无鉴权但字段偶有变动, 任何异常静默降级为空。"""
        code = symbol[-6:]
        url = "https://irm.cninfo.com.cn/newircs/index/search"
        payload = {"pageNum": "1", "pageSize": str(limit), "searchTypes": "1,11",
                   "market": "", "industry": "", "stockCode": "", "keyWord": code,
                   "highLight": "false", "type": "1"}
        headers = {"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest",
                   "Referer": "https://irm.cninfo.com.cn/interactiveAnnouncement.html"}
        try:
            r = self.session.post(url, data=payload, headers=headers, timeout=10)
            results = ((r.json().get("data") or {}).get("results")) or []
        except Exception:
            return []
        out = []
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
        for it in results:
            question = _clean_html(str(it.get("mainContent") or ""))    # 投资者提问
            answer = _clean_html(str(it.get("attachContent") or ""))    # 公司回复
            text = answer or question
            if not text:
                continue
            pub = str(it.get("pubDate") or it.get("updateDate") or "").strip()
            try:
                if pub.isdigit():
                    v = int(pub)
                    dt = pd.to_datetime(v, unit="ms" if len(pub) >= 13 else "s")
                else:
                    dt = pd.to_datetime(pub)
            except Exception:
                continue
            if getattr(dt, "tzinfo", None) is not None:
                dt = dt.tz_localize(None)
            if dt < cutoff:
                continue
            company = str(it.get("companyShortName") or it.get("stockShortName") or "")
            title = f"{company}互动易: {(question or text)[:60]}".strip()
            out.append({"source": "irm_qa", "title": title,
                        "content": text[:600],
                        "datetime": dt, "url": "https://irm.cninfo.com.cn/"})
        return out

    def _fill_ann_contents(self, news: list[dict], max_fetch: int = 6) -> None:
        """给最近的东财公告补正文 (best-effort, 上限 max_fetch 控制延迟)。"""
        fetched = 0
        for n in news:  # news 已按时间倒序
            if fetched >= max_fetch:
                break
            if n.get("source") != "eastmoney_ann" or n.get("content"):
                continue
            content = self.fetch_ann_content(n.get("art_code") or "")
            if content:
                n["content"] = content
            fetched += 1

    # ─────────────────── 市场级数据源 ─────────────────--

    def fetch_market_news(self, days: int = 2, num: int = 50) -> list[dict]:
        """新浪全市场滚动快讯 (k 参数被服务端忽略, 只能作大盘环境参考,
        不能当个股新闻用 — 此处显式归入市场级)。"""
        url = "https://feed.mix.sina.com.cn/api/roll/get"
        params = {
            "pageid": "153",
            "lid": "2509",  # 股票新闻频道
            "k": "",
            "num": str(num),
            "page": "1",
            "r": "0.123456",
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}
        try:
            r = self.session.get(url, params=params, headers=headers, timeout=10)
            data = r.json()
            items = data.get("result", {}).get("data", []) or []
        except Exception:
            return []
        out = []
        cutoff = datetime.now() - timedelta(days=days)
        for it in items:
            try:
                dt = datetime.fromtimestamp(int(it.get("ctime", 0)))
            except Exception:
                continue
            if dt < cutoff:
                continue
            title = _clean_html(it.get("title", ""))
            if not title:
                continue
            out.append({
                "source": "sina_market",
                "title": title,
                "content": title,
                "datetime": dt,
                "url": it.get("url", ""),
            })
        return out

    def fetch_sina_live(self, days: int = 2, num: int = 60) -> list[dict]:
        """新浪 7x24 全球实时财经快讯 (zhibo.sina.com.cn) — 市场级补充源。

        相比滚动频道时效更高 (分钟级), 含宏观/行业突发; 仅作 market_env 参考。"""
        url = "https://zhibo.sina.com.cn/api/zhibo/feed"
        params = {"page": "1", "page_size": str(num), "zhibo_id": "152",
                  "tag_id": "0", "dire": "f", "dpc": "1"}
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}
        try:
            r = self.session.get(url, params=params, headers=headers, timeout=10)
            feed = (((r.json().get("result") or {}).get("data") or {})
                    .get("feed") or {}).get("list") or []
        except Exception:
            return []
        out = []
        cutoff = datetime.now() - timedelta(days=days)
        for it in feed:
            try:
                ct = it.get("create_time")
                dt = (pd.to_datetime(ct).to_pydatetime()
                      if not str(ct).isdigit() else datetime.fromtimestamp(int(ct)))
            except Exception:
                continue
            if dt < cutoff:
                continue
            text = _clean_html(str(it.get("rich_text") or ""))
            if len(text) < 6:
                continue
            out.append({"source": "sina_live", "title": text[:120],
                        "content": text, "datetime": dt,
                        "url": str(it.get("docurl") or "")})
        return out

    # ─────────────────── 分析 ───────────────────

    @staticmethod
    def _aggregate(news: list[dict], top_k: int = 5) -> dict:
        """通用聚合: 加权平均情绪分 + 重点事件/风险标记 + 逐条明细。

        每条权重 = 时效衰减 × 来源权威度 × 例行公告系数;
        逐条 score/weight 存入 result["items"] (供价格验证/K线标注复用)。"""
        now = pd.Timestamp.now()
        total_w = total_s = 0.0
        bull_cnt = bear_cnt = 0
        events = []
        sectors_set = set()
        items = []
        for n in news:
            age_h = max(0.0, (now - n["datetime"]).total_seconds() / 3600.0)
            decay = 0.5 ** (age_h / _HALF_LIFE_H)
            src_w = SOURCE_WEIGHT.get(n["source"], 1.0)
            routine = is_routine_ann(n.get("title", ""), n.get("source", ""))
            if routine:
                src_w *= ROUTINE_ANN_FACTOR
            s = _score_text(f"{n['title']} {n['content']}")
            w = decay * src_w
            total_w += w
            total_s += s * w
            if s > 0.15:
                bull_cnt += 1
                events.append({**n, "score": round(s, 3), "weight": round(w, 3)})
            elif s < -0.15:
                bear_cnt += 1
                events.append({**n, "score": round(s, 3), "weight": round(w, 3)})
            sectors_set.update(_extract_sector(f"{n['title']} {n['content']}"))
            items.append({**n, "score": round(s, 3), "weight": round(w, 3),
                          "routine": routine})

        score = max(-1.0, min(1.0, total_s / total_w)) if total_w > 0 else 0.0
        key_events = sorted([e for e in events if e["score"] > 0],
                            key=lambda x: -abs(x["score"]) * x["weight"])[:top_k]
        risk_flags = sorted([e for e in events if e["score"] < 0],
                            key=lambda x: -abs(x["score"]) * x["weight"])[:top_k]
        news_sorted = sorted(news, key=lambda x: x["datetime"], reverse=True)
        items.sort(key=lambda x: x["datetime"], reverse=True)
        return {
            "score": round(score, 3),
            "count": len(news),
            "bull_count": bull_cnt,
            "bear_count": bear_cnt,
            "key_events": key_events,
            "risk_flags": risk_flags,
            "sectors": sorted(sectors_set),
            "latest": news_sorted[0] if news_sorted else None,
            "items": items[:80],
        }

    def analyze(self, symbol: str, days: int = 7) -> dict:
        """个股新闻情绪: 仅聚合个股级来源 (公告+个股资讯+互动易), 不混入市场杂讯。

        流程: 三源并发式顺序抓取 → 最近公告补正文 → 近似去重 → 加权聚合。
        结果含 items 明细, 供 apply_price_validation / K线新闻标注复用。"""
        key = (symbol, days)
        now = time.time()
        with _NEWS_LOCK:
            cached = _NEWS_CACHE.get(key)
            if cached and now - cached[0] < _NEWS_CACHE_TTL:
                return cached[1]

        news = self.fetch_eastmoney_announcements(symbol, days)
        ann_count = len(news)
        news.extend(self.fetch_em_stock_news(symbol, days))
        qa = self.fetch_irm_qa(symbol, days=max(days, 14))
        news.extend(qa)
        try:
            self._fill_ann_contents(news)
        except Exception:
            pass
        news = dedup_news(news)
        result = self._aggregate(news)
        result["ann_count"] = ann_count
        result["news_count"] = sum(1 for n in news
                                   if n.get("source") != "eastmoney_ann")

        with _NEWS_LOCK:
            _NEWS_CACHE[key] = (now, result)
        return result

    def analyze_market(self, days: int = 2) -> dict:
        """市场级快讯情绪 (滚动频道 + 7x24直播 双源合并, 供大盘环境参考)。"""
        with _NEWS_LOCK:
            cached = _MARKET_NEWS_CACHE.get(days)
            if cached and time.time() - cached[0] < _NEWS_CACHE_TTL:
                return cached[1]
        news = self.fetch_market_news(days=days)
        news.extend(self.fetch_sina_live(days=days))
        news = dedup_news(news)
        result = self._aggregate(news)
        with _NEWS_LOCK:
            _MARKET_NEWS_CACHE[days] = (time.time(), result)
        return result


# ═══════════════════ 验证层 1: 价格反应验证 (effort vs result) ═══════════════════

_REACT_WINDOW = 3        # 发布后观察 K 线根数
_REACT_CONFIRM = 0.005   # 顺新闻方向涨跌 ≥0.5% → 市场确认
_REACT_REJECT = 0.010    # 逆方向 ≥1% 且放量 → 市场证伪
_REACT_VOL_REJECT = 1.15  # 证伪要求放量 (≥1.15×量MA20, 排除大盘噪音阴跌)
_W_CONFIRM = 1.15        # 确认条目加权系数
_W_REJECT = 0.40         # 证伪条目降权系数


def apply_price_validation(news_result: dict, df=None) -> dict:
    """威科夫 effort-vs-result: 用新闻发布后的量价反应验证新闻成色。

    对每条 |score|>=0.15 的新闻, 取发布前收盘为基准, 观察其后最多
    _REACT_WINDOW 根 K 线:
      confirmed  顺向运动 ≥0.5% (或逆向前缩量企稳) → 权重 ×1.15
      rejected   逆向运动 ≥1% 且放量 (利好+放量下跌=借利好派发, 吸收失败)
                 → 权重 ×0.40
      pending    发布太近无后续K线 / 数据不足 → 权重不变
    纯函数: 返回浅拷贝+新 items, 绝不修改缓存中的原对象。
    重算 score / key_events / risk_flags (按调整后权重), 新增:
      raw_score    验证前聚合分
      validation   {"confirmed","rejected","pending","note"}
    """
    if not isinstance(news_result, dict) or df is None or len(df) < 12:
        return news_result
    items_in = news_result.get("items") or []
    if not items_in:
        return news_result
    try:
        days = pd.to_datetime(df["day"])
        close = df["close"].astype(float).values
        vol = df["volume"].astype(float).values
        vol_ma = pd.Series(vol).rolling(20, min_periods=5).mean().bfill().values
    except Exception:
        return news_result
    n = len(close)

    items = [dict(it) for it in items_in]
    n_conf = n_rej = n_pend = 0
    for it in items:
        s = it.get("score", 0.0)
        w0 = float(it.get("weight", 1.0)) or 1.0
        it["_w"] = w0
        if abs(s) < 0.15:
            continue
        try:
            dt = pd.Timestamp(it["datetime"])
            idx = int(days.searchsorted(dt))
        except Exception:
            it["validation"] = "pending"
            n_pend += 1
            continue
        j_end = min(idx + _REACT_WINDOW, n - 1)
        base_i = idx - 1 if idx > 0 else idx
        if idx <= 0 or idx >= n or j_end <= idx or close[base_i] <= 0:
            it["validation"] = "pending"
            n_pend += 1
            continue
        ret = close[j_end] / close[base_i] - 1.0
        seg_vol = vol[idx:j_end + 1]
        ref = vol_ma[idx - 1] if np.isfinite(vol_ma[idx - 1]) and vol_ma[idx - 1] > 0 \
            else float(np.mean(seg_vol)) if len(seg_vol) else 0.0
        vr = float(np.mean(seg_vol)) / float(ref) if ref and ref > 0 else 1.0
        pos = s > 0
        move = ret if pos else -ret           # 顺新闻方向的净运动
        if move >= _REACT_CONFIRM:
            verdict, wf = "confirmed", _W_CONFIRM
            n_conf += 1
        elif move >= 0 and vr <= 0.9:
            # 缩量整理: 无抛压承接良好, 视为温和确认 (威科夫: 回调无供给)
            verdict, wf = "confirmed", 1.0
            n_conf += 1
        elif move <= -_REACT_REJECT and vr >= _REACT_VOL_REJECT:
            verdict, wf = "rejected", _W_REJECT
            n_rej += 1
        else:
            verdict, wf = "pending", 1.0       # 方向不明: 保持原权重
            n_pend += 1
        it["validation"] = verdict
        it["_w"] = w0 * wf

    tw = sum(float(it.get("_w", 1.0)) for it in items)
    ts = sum(float(it.get("score", 0.0)) * float(it.get("_w", 1.0)) for it in items)
    new_score = max(-1.0, min(1.0, ts / tw)) if tw > 0 else 0.0

    def _rank(lst, positive):
        pool = [it for it in items
                if (it["score"] > 0.15) == positive and abs(it.get("score", 0.0)) > 0.15]
        return sorted(pool, key=lambda x: -abs(x["score"]) * x.get("_w", 1.0))[:5]

    note_parts = []
    if n_conf:
        note_parts.append(f"{n_conf}条市场确认")
    if n_rej:
        note_parts.append(f"{n_rej}条被证伪降权")
    if n_pend:
        note_parts.append(f"{n_pend}条待观察")
    out = dict(news_result)
    out.update({
        "raw_score": news_result.get("score"),
        "score": round(new_score, 3),
        "key_events": _rank(items, True),
        "risk_flags": _rank(items, False),
        "items": items,
        "validation": {"confirmed": n_conf, "rejected": n_rej, "pending": n_pend,
                       "note": "·".join(note_parts)},
    })
    return out


# ═══════════════════ 验证层 2: 新闻 × 威科夫事件共振 ═══════════════════

from .config import W_RECENT, event_dir  # noqa: E402  (config 无反向依赖)

_RES_MAX_BONUS = 25.0
TRAP_ACCUM_EVENTS = ("Spring", "Shakeout")    # 底部陷阱类: 伴随利空=恐吓筹码
TRAP_DISTR_EVENTS = ("UTAD", "LPSY", "UT")    # 顶部陷阱类: 伴随利好=诱多吹票


def event_resonance(news_sentiment: dict, events: list, max_idx: int,
                    recent_window: int = None) -> tuple[float, str]:
    """新闻方向 × 近期威科夫事件方向的共振/背离校验。

    返回 (bonus ∈ [-25,+25], note):
      同向共振 (SOS/LPS+利好, SOW/UTAD+利空) → 正 bonus, 因果互证加成;
      经典陷阱组合按复合人行为学反向解读:
        Spring/Shakeout 伴随利空 → 吓筹洗盘特征, 给小幅正分而非惩罚;
        UTAD/LPSY/UT 伴随利好 → 诱多出货嫌疑, 额外负分;
      其余背离 → 负 bonus, 提示基本面叙事与技术结构矛盾。
    """
    ns = (news_sentiment or {}).get("score", 0.0)
    if abs(ns) < 0.15 or not events:
        return 0.0, ""
    if recent_window is None:
        recent_window = W_RECENT
    ndir = 1.0 if ns > 0 else -1.0
    net = 0.0
    types = []
    for e in events:
        dist = max_idx - e.get("idx", 0)
        if dist < 0 or dist > recent_window:
            continue
        d = event_dir(e.get("type", ""))
        if d == 0:
            continue
        conf = (e.get("conf", 50) or 50) / 100.0
        net += d * conf * (1.0 - dist / recent_window) ** 1.5
        types.append((e.get("type", ""), dist))
    if not types or abs(net) < 0.3:
        return 0.0, ""
    tdir = 1.0 if net > 0 else -1.0
    strength = min(1.0, abs(ns))
    names = "、".join(sorted({t for t, _d in types})[:3])
    if tdir == ndir:
        bonus = _RES_MAX_BONUS * strength * min(1.0, abs(net) / 2.0)
        return round(bonus, 1), f"与事件({names})同向共振"
    recent_types = {t for t, dist in types if dist <= 15}
    if ndir < 0 and any(t in TRAP_ACCUM_EVENTS for t in recent_types):
        return 8.0, "Spring伴随利空·恐吓筹码特征"
    if ndir > 0 and any(t in TRAP_DISTR_EVENTS for t in recent_types):
        return -14.0, "利好伴随派发事件·诱多嫌疑"
    return round(-_RES_MAX_BONUS * strength * 0.6, 1), "新闻与近期事件背离"


# ═══════════════════ 验证层 3: 前瞻事件日历 (解禁/业绩预告/财报披露) ═══════════════════

_CAL_TTL = 6 * 3600
_CAL_CACHE = {}          # code -> (ts, calendar)
_RAW_CAL_CACHE = {}      # kind -> (ts, rows)   全市场原始表 (拉一次全池共用)
_CAL_LOCK = Lock()


def _raw_calendar_rows(kind: str):
    """flow_extra 的全市场原始表, 进程内 TTL 缓存 (避免逐股重复拉 akshare)。"""
    with _CAL_LOCK:
        cached = _RAW_CAL_CACHE.get(kind)
        if cached and time.time() - cached[0] < _CAL_TTL:
            return cached[1]
    rows = []
    try:
        from . import flow_extra as _fx
        if kind == "restricted":
            rows = _fx.fetch_restricted(days=45, min_ratio=0.5)
        elif kind == "yjyg":
            rows = _fx.fetch_yjyg()
    except Exception:
        rows = []
    with _CAL_LOCK:
        _RAW_CAL_CACHE[kind] = (time.time(), rows)
    return rows


def _fetch_report_appoint(code: str) -> list[dict]:
    """东财数据中心: 定期报告预约披露时间 (RPT_PUBLIC_BS_APPOIN, best-effort)。"""
    session = http_session()
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_PUBLIC_BS_APPOIN",
        "columns": "SECURITY_CODE,REPORT_TYPE,APPOINT_PUBLISH_DATE,PUBLISH_DATE",
        "filter": f'(SECURITY_CODE="{code}")',
        "pageSize": "16", "pageNumber": "1",
        "sortColumns": "APPOINT_PUBLISH_DATE", "sortTypes": "-1",
        "source": "WEB", "client": "WEB",
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
    try:
        r = session.get(url, params=params, headers=headers, timeout=8)
        rows = ((r.json().get("result") or {}).get("data")) or []
    except Exception:
        return []
    out = []
    for row in rows:
        appoint = row.get("APPOINT_PUBLISH_DATE") or row.get("PUBLISH_DATE")
        if not appoint:
            continue
        out.append({"report_type": str(row.get("REPORT_TYPE") or "定期报告"),
                    "date": str(appoint)[:10]})
    return out


def fetch_forward_calendar(symbol: str, days_ahead: int = 30) -> dict:
    """未来 N 日前瞻事件日历: 解禁 / 业绩预告 / 定期报告预约披露 (个股过滤)。

    返回 {"items": [{kind,date,detail,tone}], "risk_days": int|None,
          "risk_label": str}
      tone: -1 偏空风险 (解禁/预亏预减), +1 潜在催化 (预增扭亏), 0 程序性节点;
      risk_days/risk_label: 最近一个偏空风险事件的剩余天数与描述
      (供融合层对临近窗口的多头信号降置信 —— 事前排雷)。
    akshare 缺失/网络失败时逐项静默降级, 至少返回空结构。"""
    code = symbol[-6:]
    with _CAL_LOCK:
        cached = _CAL_CACHE.get(code)
        if cached and time.time() - cached[0] < _CAL_TTL:
            return cached[1]

    today = datetime.now().date()
    items = []

    def _gap_days(dstr):
        try:
            return (pd.to_datetime(dstr).date() - today).days
        except Exception:
            return None

    for r in _raw_calendar_rows("restricted"):
        if str(r.get("code")) != code:
            continue
        gap = _gap_days(r.get("date", ""))
        if gap is None or not (0 <= gap <= days_ahead):
            continue
        ratio = float(r.get("ratio") or 0.0)
        items.append({"kind": "解禁", "date": str(r.get("date", ""))[:10],
                      "detail": f"解禁约{ratio:.1f}%流通市值 ({r.get('type') or '限售股'})",
                      "tone": -1, "gap": gap})
    for r in _raw_calendar_rows("yjyg"):
        if str(r.get("code")) != code:
            continue
        gap = _gap_days(r.get("date", ""))
        if gap is None or not (-30 <= gap <= days_ahead):  # 近30日内已发布的预告仍有定价影响
            continue
        ampl = r.get("ampl")
        tone = 0
        if ampl is not None:
            tone = 1 if ampl > 0 else -1 if ampl < 0 else 0
        kind_txt = str(r.get("kind") or "业绩预告")
        detail = f"{ampl:+.0%}" if ampl is not None else str(r.get("msg") or "")
        items.append({"kind": kind_txt, "date": str(r.get("date", ""))[:10],
                      "detail": detail, "tone": tone, "gap": max(gap, 0)})
    for r in _fetch_report_appoint(code):
        gap = _gap_days(r.get("date", ""))
        if gap is None or not (0 <= gap <= days_ahead):
            continue
        items.append({"kind": "财报披露", "date": str(r["date"]),
                      "detail": r.get("report_type", ""), "tone": 0, "gap": gap})

    items.sort(key=lambda x: (x["gap"], x["date"]))
    risk_days = None
    risk_label = ""
    for it in items:
        if it["tone"] < 0:
            risk_days, risk_label = it["gap"], f"{it['kind']}·{it['detail']}"
            break
        if it["tone"] == 0 and it["kind"] == "财报披露":
            risk_days, risk_label = it["gap"], f"定期报告披露 ({it['detail']})"
            break
    cal = {"items": [{k: v for k, v in it.items() if k != "gap"} for it in items[:12]],
           "risk_days": risk_days, "risk_label": risk_label}
    with _CAL_LOCK:
        _CAL_CACHE[code] = (time.time(), cal)
    return cal


# ─────────────────── 便捷函数 ───────────────────

_default_sentiment = NewsSentiment()


def fetch_news_sentiment(symbol: str, days: int = 7) -> dict:
    """便捷函数: 获取并分析个股新闻情绪 (仅个股级来源)"""
    return _default_sentiment.analyze(symbol, days)


def fetch_market_news_sentiment(days: int = 2) -> dict:
    """便捷函数: 市场级快讯情绪 (供 market_env 参考)"""
    return _default_sentiment.analyze_market(days)
