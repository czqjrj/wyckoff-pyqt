#!/usr/bin/env python3
"""威科夫四大高胜率策略管理系统

策略1: 威科夫7项通过 + 确认事件
策略2: 威科夫事件 + 高价值VSA标签
策略3: 多因子强化做强多头策略
策略4: 模拟盘纪律策略 (强多头事件 conf≥90 + 硬门禁, 源自 wyckoff.paper 实证,
       真实K线历史回放胜率~53%、盈亏比~3、累计收益+50%)
"""

import numpy as np
from collections import defaultdict, deque
import json
import os
from datetime import datetime

from wyckoff.ninetests import nine_tests
from wyckoff.events import detect_all
from wyckoff.indicators import find_pivots, add_indicators
from wyckoff.datasource import fetch_kline, fetch_name
from wyckoff.utils import normalize_symbol
from wyckoff.vsa import vsa_classify
from wyckoff.config import VSA_BULL, VSA_BEAR


class WyckoffStrategyManager:
    """威科夫三大高胜率策略管理器"""
    
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
                with open(history_file, 'r', encoding='utf-8') as f:
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

    def evaluate_strategy_1(self, df, i, wevents, nt, vsa_labels, params=None):
        """策略1: 威科夫7项以上通过 + 近期确认事件 + 量比确认 + 位置过滤

        params 可选（用于回测参数扫描/优化）：
            min_buy_passed: 威科夫通过项下限 (默认 7)
            max_position:   价格位置上限 (默认 0.9)
            conf_min:       确认事件置信度下限 (默认 70)
            event_window:   确认事件距当前的最大K数 (默认 20)
            min_vr:         VSA量比下限 (默认 1.1)
        """
        p = {
            "min_buy_passed": 7, "max_position": 0.9, "conf_min": 70,
            "event_window": 20, "min_vr": 1.1,
        }
        if params:
            p.update(params)
        if nt["buy_passed"] < p["min_buy_passed"]:
            return None
        if self._price_position(df, i) > p["max_position"]:
            return None
        confirmed_events = [
            e for e in wevents
            if e.get("confirmed") is True
            and e.get("conf", 0) > p["conf_min"]
            and e["idx"] >= i - p["event_window"]
        ]
        if not confirmed_events:
            return None
        if not self._high_vsa_near(vsa_labels, i, ["CHOC", "DEM", "SUP", "LPS", "Spring", "ST"], min_vr=p["min_vr"]):
            return None
        return {
            "strategy": "wyckoff_7plus_confirmed",
            "name": "威科夫7项通过+确认事件",
            "signal": "high_value_event",
            "confidence": 90,
            "details": f"威科夫7项通过 + 确认事件: {confirmed_events[0]['type']} (conf={confirmed_events[0].get('conf')})",
            "event": confirmed_events[0],
            "trading": self._trading_discipline()
        }

    def evaluate_strategy_2(self, df, i, wevents, nt, vsa_labels):
        """策略2: 威科夫事件 + 高价值VSA标签 (质量+量比+位置过滤)"""
        if nt["buy_passed"] < 4:
            return None
        high_events = [
            e for e in wevents
            if e["type"] in ["Spring", "Shakeout", "SOS", "JOC", "ST"]
            and e.get("conf", 0) > 70
            and e["idx"] >= i - 25
        ]
        if not high_events:
            return None
        high_vsa = self._high_vsa_near(
            vsa_labels, i, ["CHOC", "DEM", "SUP", "LPS", "Spring", "ST"], min_vr=1.2, window=8
        )
        if not high_vsa:
            return None
        if self._price_position(df, i) > 0.9:
            return None
        event_conf = max([e.get("conf", 0) for e in high_events])
        return {
            "strategy": "wyckoff_plus_vsa",
            "name": "威科夫事件+高价值VSA",
            "signal": "combined_signal",
            "confidence": min(90, event_conf),
            "details": f"威科夫高价值事件 + 高价值VSA: {high_events[0]['type']} (conf={event_conf}) + {high_vsa[0]['label']} (vr={high_vsa[0].get('features', {}).get('vr', 0):.1f})",
            "event": high_events[0],
            "vsa": high_vsa[0],
            "price_position": self._price_position(df, i),
            "trading": self._trading_discipline()
        }

    def evaluate_strategy_3(self, df, i, wevents, nt, vsa_labels, stock_code):
        """策略3: 多因子强化做强多头策略 (强信号+量比确认+趋势+位置+风控)"""
        strong_bull_events = ["Spring", "Shakeout", "ST", "LPS", "SC", "SOS", "JOC"]
        bull_events = [
            e for e in wevents
            if e["type"] in strong_bull_events
            and e.get("conf", 0) >= 85
            and e["idx"] >= i - 20
        ]
        if not bull_events:
            return None
        event = bull_events[0]
        supporting_vsa = self._high_vsa_near(
            vsa_labels, i, ["CHOC", "DEM", "SUP", "LPS", "Spring", "ETR"], min_vr=1.2, window=8
        )
        if not supporting_vsa:
            return None
        if "price_ma20" not in df.columns or df["close"].iloc[i] <= df["price_ma20"].iloc[i]:
            return None
        pos = self._price_position(df, i)
        if pos > 0.9:
            return None
        if "vol_ma20" in df.columns:
            if df["volume"].iloc[i] > df["vol_ma20"].iloc[i] * 3:
                return None
        return {
            "strategy": "multi_factor_bull",
            "name": "多因子强化做强多头",
            "signal": "strong_bull_with_conditions",
            "confidence": 95,
            "details": f"强多头事件: {event['type']} (conf={event.get('conf')}) + VSA: {supporting_vsa[0]['label']}",
            "event": event,
            "vsa_support": supporting_vsa[0],
            "price_position": pos,
            "requirements_met": "量比确认+趋势+位置+风控",
            "trading": self._trading_discipline()
        }

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
        LONG_EVENT_TYPES = ("Spring", "Shakeout", "ST", "LPS", "SC")
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
    
    def analyze_stock(self, code, datalen=1000, horizon=20, cost=0.004):
        """分析股票并应用三大策略"""
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
            
            # 应用策略1
            strategy1_result = self.evaluate_strategy_1(df, i, wevents, nt, vsa_labels)
            if strategy1_result:
                current_analysis["strategies_found"].append(strategy1_result)
            
            # 应用策略2
            strategy2_result = self.evaluate_strategy_2(df, i, wevents, nt, vsa_labels)
            if strategy2_result:
                current_analysis["strategies_found"].append(strategy2_result)
            
            # 应用策略3
            strategy3_result = self.evaluate_strategy_3(df, i, wevents, nt, vsa_labels, code)
            if strategy3_result:
                current_analysis["strategies_found"].append(strategy3_result)

            # 应用策略4 (模拟盘纪律策略; 离线历史扫描默认关闭实时门禁, 只出选股信号)
            strategy4_result = self.evaluate_strategy_4(df, i, wevents, nt, vsa_labels)
            if strategy4_result:
                current_analysis["strategies_found"].append(strategy4_result)
        
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
            "strategy_1": {
                "name": "威科夫7项通过 + 确认事件",
                "description": "威科夫九大检验点中7项以上通过，并有确认事件的高质量信号",
                "characteristics": ["高置信度", "事件确认", "精确过滤"],
                "conditions": ["威科夫7项通过", "事件确认", "位置过滤"]
            },
            "strategy_2": {
                "name": "威科夫事件 + 高价值VSA标签",
                "description": "威科夫事件与高价值VSA标签的组合验证",
                "characteristics": ["多维度验证", "量比确认", "价格位置过滤"],
                "conditions": ["威科夫事件", "高价值VSA标签", "位置过滤"]
            },
            "strategy_3": {
                "name": "多因子强化做强多头策略",
                "description": "基于强多头事件，结合市场环境条件的强化策略",
                "characteristics": ["强信号", "量比确认", "趋势+位置+风控"],
                "conditions": ["强多头事件", "量比确认", "趋势、位置、风控"]
            },
            "strategy_4": {
                "name": "模拟盘纪律策略 (强多头 + 硬门禁)",
                "description": "源自模拟盘实证：强多头事件(conf≥90) + 大盘/板块/资金流硬门禁，配合同持上限3与止盈止损出场纪律",
                "characteristics": ["真实回测胜率~53%", "盈亏比~3.0", "累计收益+50%"],
                "conditions": ["强多头事件(Spring/Shakeout/ST/LPS/SC) conf≥90",
                               "硬门禁(大盘20日线/板块>60分位/资金流>中位)",
                               "同持上限3 + 持20K + -5%止损 + +15%止盈 + 结构破位"]
            }
        }


def main():
    """主函数 - 四大策略管理演示"""
    print("=== 威科夫四大策略管理系统 ===")
    print()
    
    # 创建策略管理器
    manager = WyckoffStrategyManager()
    
    # 加载历史数据
    manager.load_performance_history()
    
    # 显示策略详情
    print("🔍 四大策略介绍:")
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
    print("1. 自动识别四大策略信号")
    print("2. 记录策略表现历史")
    print("3. 提供策略统计分析")
    print("4. 支持策略报告导出")
    print("5. 可扩展添加新策略")


if __name__ == "__main__":
    main()