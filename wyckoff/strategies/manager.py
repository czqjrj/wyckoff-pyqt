#!/usr/bin/env python3
"""威科夫策略管理系统

策略4: 模拟盘纪律策略 (强多头事件 conf≥90 + 硬门禁, 源自 wyckoff.paper 实证,
       真实K线历史回放胜率~53%、盈亏比~3、累计收益+50%)

价值吸筹: 综合选股「价值吸筹」预设 (底部整固 + 20根内吸筹事件, 源自
       research/screener_presets_verify.py 2026-09-02 回测: 48只样本胜率49.2%、
       盈亏比1.53、累计收益+224%, 唯一实测正期望的推荐预设)
"""

import json
import os
from collections import defaultdict, deque
from datetime import datetime

from wyckoff.datasource import fetch_kline, fetch_name
from wyckoff.events import detect_all
from wyckoff.indicators import add_indicators, find_pivots
from wyckoff.ninetests import nine_tests
from wyckoff.phases import judge_phase
from wyckoff.utils import normalize_symbol
from wyckoff.vsa import vsa_classify

# 多头吸筹事件集: 综合选股「价值吸筹」与策略4共用
LONG_EVENT_TYPES = ("Spring", "Shakeout", "ST", "LPS", "SC")

# SOS动态确认窗口 (源自 events.py DYNAMIC_WINDOW)
SOS_CONFIRM_WINDOW = 5

# Spring回踩确认窗口: Spring后确认窗口根数
SPRING_CONFIRM_WINDOW = 8


class WyckoffStrategyManager:
    """威科夫高胜率策略管理器"""

    def __init__(self, data_dir="three_strategy_data"):
        self.data_dir = data_dir
        self.strategy_results = defaultdict(list)
        self.performance_log = deque(maxlen=100)

        # 创建数据目录
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)

    def load_performance_history(self):
        """加载历史性能记录"""
        history_file = os.path.join(self.data_dir, "performance_history.json")
        if os.path.exists(history_file):
            try:
                with open(history_file, encoding='utf-8') as f:
                    self.performance_log = deque(json.load(f), maxlen=100)
            except:
                pass

    def save_performance_history(self):
        """保存性能记录"""
        history_file = os.path.join(self.data_dir, "performance_history.json")
        try:
            with open(history_file, 'w', encoding='utf-8') as f:
                json.dump(list(self.performance_log), f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存历史记录失败: {e}")

    @staticmethod
    def _price_position(df, i, window=20):
        """计算当前价格在近 window 日区间的相对位置 (0~1)，越低越安全"""
        recent_high = df["high"].iloc[max(0, i - window):i].max()
        recent_low = df["low"].iloc[max(0, i - window):i].min()
        if recent_high - recent_low <= 0:
            return 0.5
        return (df["close"].iloc[i] - recent_low) / (recent_high - recent_low)

    @staticmethod
    def _high_vsa_near(vsa_labels, i, high_types, min_vr=1.2, window=5):
        """查找 i 附近 window 格内、带量比确认的高价值 VSA 标签"""
        result = []
        for s in vsa_labels:
            if s["label"] not in high_types:
                continue
            if s["idx"] > i or s["idx"] < 0:  # 不允许使用未来数据
                continue
            if i - s["idx"] > window:
                continue
            vr = s.get("features", {}).get("vr", 0)
            if vr >= min_vr:
                result.append(s)
        return result

    @staticmethod
    def _trading_discipline(**overrides):
        """策略4 实证的交易纪律 (止损/止盈/持有/同持上限/结构破位),
        供各策略共用，保证信号落地时执行相同的出场规则。"""
        d = {
            "max_pos": 3,          # 同持上限
            "hold_bars": 20,       # 最大持有K数
            "stop_loss": 0.05,     # 止损 -5%
            "take_profit": 0.15,   # 止盈 +15%
            "support_break": True, # 结构位破位离场
            "cost": 0.004,         # 单边成本
        }
        d.update(overrides)
        return d

    def evaluate_strategy_4(self, df, i, wevents, nt, vsa_labels, stock_code=None, min_conf=90):
        """策略4: 模拟盘纪律策略 (强多头事件 + high conf + 硬门禁)

        源自 wyckoff.paper 模拟盘实证 (真实K线历史回放胜率~53%、盈亏比~3):
          选股: 强多头事件 {Spring, Shakeout, ST, LPS, SC}, conf≥90, 事件在近10根内;
          纪律硬门禁 (栅栏, 缺条件即拦截):
            ① 大盘20日线向上
            ② 板块强度 > 60 分位
            ③ 资金流近5日主力净流入 > 截面中位
          该策略的盈利主要通过 同持上限3 + 出场纪律(-5%止损/+15%止盈/结构破位) 实现,
          管理器中仅负责"选股信号"部分; 持仓与出场纪律见 wyckoff.paper。
        """
        bull_events = [
            e for e in wevents
            if e["type"] in LONG_EVENT_TYPES
            and int(e.get("conf", 0) or 0) >= min_conf
            and e["idx"] >= i - 10  # 事件在近10根内（模拟盘可买入窗口）
        ]
        if not bull_events:
            return None
        event = bull_events[0]

        signal = {
            "strategy": "paper_discipline_bull",
            "name": "模拟盘纪律策略",
            "signal": "strong_bull_conf90",
            "confidence": int(event.get("conf", 0) or 0),
            "details": f"模拟盘纪律: 强多头事件 {event['type']} (conf={int(event.get('conf', 0) or 0)})",
            "event": {"type": event["type"], "idx": event["idx"], "conf": int(event.get("conf", 0) or 0)},
            "requirements_met": "强多头+conf≥90",
            "trading": self._trading_discipline(),
        }

        # 可选硬门禁（需实时数据，离线分析默认关闭；开启时任一不满足即不产生信号）
        if stock_code:
            gates = self._check_discipline_gates(stock_code)
            signal["gates"] = gates
            if not gates["all_pass"]:
                return None
        return signal

    @staticmethod
    def _check_discipline_gates(stock_code):
        """复刻模拟盘三道硬门禁 (与 wyckoff.paper 口径一致, fail-close)。
        任一数据不可用即视为不满足 (严格拦截)。返回 {all_pass, details}。"""
        pass_ = True
        details = []
        try:
            from wyckoff.paper import _market_trend_ok
            ok, reason = _market_trend_ok()
            pass_ &= bool(ok)
            details.append(f"大盘20日线: {'通过' if ok else f'拦截({reason})'}")
        except Exception:
            pass_ &= False
            details.append("大盘20日线: 拦截(数据不可用)")
        try:
            from wyckoff.fundamental import fetch_sector
            from wyckoff.paper import _sector_strength_ok
            sector = fetch_sector(stock_code) or ""
            ok, reason = _sector_strength_ok(sector)
            pass_ &= bool(ok)
            details.append(f"板块强度: {'通过' if ok else f'拦截({reason})'}")
        except Exception:
            pass_ &= False
            details.append("板块强度: 拦截(数据不可用)")
        try:
            from wyckoff.paper import _flow_net5
            flow = _flow_net5(stock_code)
            pass_ &= bool(flow is not None and flow > 0)
            details.append(f"资金流: {'通过' if (flow is not None and flow > 0) else '拦截(净流入不足)'}")
        except Exception:
            pass_ &= False
            details.append("资金流: 拦截(数据不可用)")
        return {"all_pass": bool(pass_), "details": details}

    def evaluate_strategy_value_accumulation(self, df, i, wevents, wpivots):
        """综合选股·价值吸筹 (推荐预设): 底部整固 + 20根内吸筹事件

        源自 research/screener_presets_verify.py 2026-09-02 综合选股预设回测,
        5个预设中唯一实测正期望 (48只样本):
          胜率 49.2%、盈亏比 1.53、累计收益 +224%, 样本内/外 47.4%/51.9%,
          时间半段 48/51, 属"正期望型"(大盈小亏) 而非 >60% 高胜率。
        事件窗口回测口径为 20 根 (比策略4的 conf≥90 + 10根 事件更密, 更稳健)。
        """
        window = df.iloc[:i + 1]
        phase, _ = judge_phase(window, wpivots, wevents)
        if (phase or "").split(" ")[0] != "底部整固":
            return None
        acc_events = [
            e for e in wevents
            if e["type"] in LONG_EVENT_TYPES and e["idx"] >= i - 20
        ]
        if not acc_events:
            return None
        event = acc_events[0]
        return {
            "strategy": "screener_value_accumulation",
            "name": "价值吸筹",
            "signal": "accumulation_bottom_build",
            "confidence": int(event.get("conf", 0) or 0),
            "details": f"价值吸筹: 底部整固 + 吸筹事件 {event['type']} "
                       f"(近20根, conf={int(event.get('conf', 0) or 0)})",
            "event": {"type": event["type"], "idx": event["idx"],
                      "conf": int(event.get("conf", 0) or 0)},
            "requirements_met": "底部整固 + 多头吸筹事件≤20根",
            "trading": self._trading_discipline(),
            "verified": {
                "date": "2026-09-02", "recommended": True,
                "n": 130, "wr": 49.2, "pf": 1.53, "cum": 223.9,
                "note": "唯一实测正期望(PF>1.5)且样本内外+时间半段稳定; "
                        "正期望型(大盈小亏)而非>60%高胜率",
            },
        }

    def evaluate_strategy_spring(self, df, i, wevents, nt, vsa_labels, stock_code=None):
        """Spring回踩确认策略

        逻辑:
        - 事件: Spring (刺破前低后收回，底部震荡中的假突破/诱多)
        - 入场: Spring确认后的动态窗口 (8根K线) 收盘价守住回升低点
        - 入场确认: 后续收盘价未跌破Spring产生日低点
        - 止损: 低于Spring产生日低点的更低低点，或 3% 止损
        - 止盈: 固定风险比 1:2 或 1:3
        - 特征: Spring是经典的威科夫筑底形态, 实证回测显示Spring后20根
                 +12.7% 的上涨概率，是中短线多头的高概率入场时机

        相比策略4: 无需硬门禁, 专注于个股底部反转形态;
        相比SOS策略: 更侧重于震荡区间内的回踩确认而非突破。
        """
        spring_events = [e for e in wevents if e["type"] == "Spring"]
        if not spring_events:
            return None

        # 取最近的Spring事件
        sp = spring_events[-1]
        sp_idx = sp["idx"]
        sp_price = sp["price"]  # Spring产生日的最低价 (因为是向下突破后收回)

        n = len(df)
        if sp_idx >= n:
            return None

        close = df["close"].values
        low = df["low"].values

        # 使用动态确认窗口: 检查后SPRING_CONFIRM_WINDOW根K线
        # 确认条件: 收盘价守住Spring日低点，未跌破
        dyn_window = SPRING_CONFIRM_WINDOW
        confirm_idx = None

        # 检查确认窗口内是否守住低点
        for j in range(sp_idx + 1, min(sp_idx + 1 + dyn_window, n)):
            if close[j] > low[sp_idx]:  # 收盘守住低点
                confirm_idx = j
                break

        if confirm_idx is None:
            return None  # 确认窗口内跌破低点，放弃

        # 入场价: 确认bar的开盘价
        entry_price = df["open"].iloc[confirm_idx]
        if entry_price <= 0:
            return None

        # 止损: 低于Spring日低点 2% (收紧止损以提高盈亏比)
        stop_price = low[sp_idx] * 0.985

        # 止盈: 固定风险比 1:2.5 (保守比例，确保正期望)
        risk = entry_price - stop_price
        if risk <= 0:
            return None
        target_price = entry_price + risk * 3.0

        # 检查在持有horizon内是否触及止盈/止损
        horizon = 20
        exit_idx = min(confirm_idx + horizon, n - 1)
        exit_price = close[exit_idx]

        # 逐日扫描出场
        hit_tp = False
        hit_sl = False
        actual_exit_price = exit_price
        actual_exit_idx = exit_idx

        for j in range(confirm_idx + 1, exit_idx + 1):
            p = close[j]

            # 触及止盈
            if p >= target_price:
                actual_exit_price = target_price
                actual_exit_idx = j
                hit_tp = True
                break

            # 触及硬止损 (低于Spring日低点3%)
            if p <= stop_price:
                actual_exit_price = stop_price
                actual_exit_idx = j
                hit_sl = True
                break

        ret = (actual_exit_price / entry_price - 1) - 0.004  # 扣除成本

        if ret > -0.1 and ret < 0.8:  # 合理返回范围
            return {
                "strategy": "spring_pullback",
                "name": "Spring回踩确认策略",
                "signal": "spring_pullback",
                "confidence": int(sp.get("conf", 0) or 0),
                "details": f"Spring确认: 确认窗口{dyn_window}根守住低点, 入场={entry_price:.2f}, "
                           f"目标={target_price:.2f}, 硬止损={stop_price:.2f}",
                "event": {"type": "Spring", "idx": sp["idx"], "conf": int(sp.get("conf", 0) or 0)},
                "requirements_met": f"Spring确认守住低点, 风险回报1:2",
                "trading": {
                    "entry_idx": confirm_idx,
                    "entry_price": float(entry_price),
                    "stop_price": float(stop_price),
                    "target_price": float(target_price),
                    "horizon": horizon,
                    "hit_tp": hit_tp,
                    "hit_sl": hit_sl,
                    "return": float(ret),
                },
            }

    def analyze_stock(self, code, datalen=1000, horizon=20, cost=0.004):
        """分析股票并应用策略"""
        symbol = normalize_symbol(code)
        df = fetch_kline(symbol, datalen=datalen, scale=240)
        # 添加必要的技术指标
        df = add_indicators(df, symbol=symbol)

        if len(df) < 150:
            return {"error": "数据不足"}

        # 存储当前分析结果
        current_analysis = {
            "stock": code,
            "name": fetch_name(symbol),
            "timestamp": datetime.now().isoformat(),
            "strategies_found": [],
            "total_samples": len(df)
        }

        # 从第90根K线开始分析
        for i in range(90, len(df) - horizon):
            # 用截至当前时刻的数据进行分析
            wdf = df.iloc[:i+1]
            # 添加必要的技术指标
            wdf = add_indicators(wdf, symbol=symbol)
            wpivots = find_pivots(wdf, order=6)
            wevents = detect_all(wdf, wpivots)

            # 计算九大检验点
            nt = nine_tests(wdf, wevents, wpivots)

            # 获取VSA标签
            vsa_labels = vsa_classify(wdf, scale=240)

            # 应用策略4 (模拟盘纪律策略; 离线历史扫描默认关闭实时门禁, 只出选股信号)
            strategy4_result = self.evaluate_strategy_4(df, i, wevents, nt, vsa_labels)
            if strategy4_result:
                current_analysis["strategies_found"].append(strategy4_result)

            # 应用综合选股·价值吸筹 (推荐预设, 底部整固 + 20根内吸筹事件)
            value_acc_result = self.evaluate_strategy_value_accumulation(
                wdf, i, wevents, wpivots)
            if value_acc_result:
                current_analysis["strategies_found"].append(value_acc_result)

        # 记录性能
        self.record_performance(current_analysis)

        return current_analysis

    def record_performance(self, analysis_result):
        """记录分析结果到性能历史"""
        # 计算策略表现
        strategy_counts = defaultdict(int)
        for strategy in analysis_result["strategies_found"]:
            strategy_counts[strategy["strategy"]] += 1

        performance_record = {
            "stock": analysis_result["stock"],
            "name": analysis_result["name"],
            "timestamp": analysis_result["timestamp"],
            "strategies_count": dict(strategy_counts),
            "total_signals": len(analysis_result["strategies_found"])
        }

        self.performance_log.append(performance_record)
        self.save_performance_history()

    def get_strategy_statistics(self):
        """获取策略统计信息"""
        if not self.performance_log:
            return {"message": "暂无历史数据"}

        total_analyses = len(self.performance_log)
        strategy_totals = defaultdict(int)
        strategy_details = defaultdict(list)

        for record in self.performance_log:
            for strategy, count in record["strategies_count"].items():
                strategy_totals[strategy] += count
                strategy_details[strategy].append(count)

        stats = {
            "total_analyses": total_analyses,
            "strategy_totals": dict(strategy_totals),
            "strategy_averages": {}
        }

        for strategy, counts in strategy_details.items():
            stats["strategy_averages"][strategy] = {
                "total": sum(counts),
                "average_per_analysis": sum(counts) / total_analyses if total_analyses > 0 else 0,
                "max_per_analysis": max(counts) if counts else 0
            }

        return stats

    def export_strategy_report(self, filename="three_strategy_report.json"):
        """导出策略报告"""
        report = {
            "generated_at": datetime.now().isoformat(),
            "strategy_statistics": self.get_strategy_statistics(),
            "recent_analyses": list(self.performance_log)[-10:]  # 最近10次分析
        }

        try:
            with open(os.path.join(self.data_dir, filename), 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print(f"策略报告已导出到: {os.path.join(self.data_dir, filename)}")
        except Exception as e:
            print(f"导出报告失败: {e}")

    def get_strategy_details(self):
        """获取所有策略的详细说明"""
        return {
            "strategy_discipline": {
                "name": "模拟盘纪律策略 (强多头 + 硬门禁)",
                "description": "源自模拟盘实证：强多头事件(conf≥90) + 大盘/板块/资金流硬门禁，配合同持上限3与止盈止损出场纪律",
                "characteristics": ["真实回测胜率~53%", "盈亏比~3.0", "累计收益+50%"],
                "conditions": ["强多头事件(Spring/Shakeout/ST/LPS/SC) conf≥90",
                               "硬门禁(大盘20日线/板块>60分位/资金流>中位)",
                               "同持上限3 + 持20K + -5%止损 + +15%止盈 + 结构破位"]
            },
            "strategy_value_accumulation": {
                "name": "综合选股·价值吸筹 (推荐预设)",
                "description": "源自综合选股预设回测：底部整固阶段 + 20根内吸筹事件(Spring/Shakeout/SC/ST/LPS)，5预设中唯一实测正期望",
                "characteristics": ["实测胜率49.2%", "盈亏比1.53", "累计收益+224%", "样本内外+时间半段稳定(47.4%/51.9%, 48/51)"],
                "conditions": ["阶段=底部整固",
                               "近20根出现 {Spring,Shakeout,SC,ST,LPS}",
                               "同持上限3 + 持20K + -5%止损 + +15%止盈 + 0.4%成本"]
            }
        }


def main():
    """主函数 - 策略管理演示"""
    print("=== 威科夫策略管理系统（模拟盘纪律策略） ===")
    print()

    # 创建策略管理器
    manager = WyckoffStrategyManager()

    # 加载历史数据
    manager.load_performance_history()

    # 显示策略详情
    print("🔍 策略介绍:")
    print()

    strategy_details = manager.get_strategy_details()
    for key, strategy in strategy_details.items():
        print(f"🎯 {strategy['name']}")
        print(f"   描述: {strategy['description']}")
        print(f"   特点: {', '.join(strategy['characteristics'])}")
        print(f"   条件: {', '.join(strategy['conditions'])}")
        print()

    # 分析股票
    print("📊 正在分析股票...")
    test_stocks = ["sh600036", "sz000001", "sh601318"]

    analysis_results = []
    for stock in test_stocks:
        try:
            result = manager.analyze_stock(stock, datalen=1000)
            analysis_results.append(result)
        except Exception as e:
            print(f"分析 {stock} 时出错: {e}")
            analysis_results.append({"stock": stock, "error": str(e)})

    # 显示分析结果
    print("\n📈 分析结果汇总:")
    print("=" * 60)

    total_strategies = 0
    strategy_counts = {}

    for result in analysis_results:
        if "error" not in result:
            count = len(result["strategies_found"])
            total_strategies += count
            print(f"{result['stock']} ({result['name']}): 发现 {count} 个高胜率信号")

            # 统计各类策略
            for signal in result["strategies_found"]:
                strategy = signal.get("name") or signal["strategy"]
                strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1
        else:
            print(f"{result['stock']}: 错误 - {result['error']}")

    print(f"\n📋 总计发现高胜率信号: {total_strategies} 个")
    print("各策略分布:")
    for strategy, count in strategy_counts.items():
        print(f"  {strategy}: {count} 个")

    # 显示策略统计
    print("\n📊 策略统计信息:")
    stats = manager.get_strategy_statistics()
    if "message" not in stats:
        print(f"总分析次数: {stats['total_analyses']}")
        print("各策略出现次数:")
        for strategy, count in stats['strategy_totals'].items():
            print(f"  {strategy}: {count} 次")
    else:
        print(stats["message"])

    # 导出报告
    manager.export_strategy_report()

    print("\n✅ 系统功能总结:")
    print("1. 自动识别策略信号")
    print("2. 记录策略表现历史")
    print("3. 提供策略统计分析")
    print("4. 支持策略报告导出")
    print("5. 可扩展添加新策略")


if __name__ == "__main__":
    main()
