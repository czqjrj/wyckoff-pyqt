#!/usr/bin/env python3
"""威科夫策略管理系统 (兼容门面)

重构后职责划分 (wyckoff/strategies/):
  - constants.py   常量与策略 key 单一来源 (零副作用)
  - evaluators.py  信号评估器 (纯函数, 无实例状态)
  - candidates.py  模拟盘候选插件表 + scan_individual 统一入口
  - cli.py         CLI 演示入口
  - manager.py     本文件: 兼容门面, 保留 WyckoffStrategyManager 类、
                   analyze_stock / 性能持久化 / 报告等实例能力, 供给历史 import
                   路径与 paper/UI/回测脚本继续使用。

门禁判定 (大盘20日线/板块强度/资金流) 统一收敛于 wyckoff.discipline 单一源,
本模块与评估器/候选模块均不再反向依赖 wyckoff.paper (解除依赖环)。

策略4: 模拟盘纪律策略 (强多头事件 conf≥90 + 硬门禁, 源自 wyckoff.paper 实证,
       真实K线历史回放胜率~53%、盈亏比~3、累计收益+50%)
价值吸筹: 综合选股「价值吸筹」预设 (底部整固 + 20根内吸筹事件, 源自
       research/screener_presets_verify.py 2026-09-02 回测: 48只样本胜率49.2%、
        盈亏比1.53、累计收益+224%, 唯一实测正期望的推荐预设)
long_buy: 威科夫完整做多买点 (可执行集合, 由低风险左侧到右侧加仓, 源自
       教学文档 + 30只×500根240分钟线实证: ST 85%/PF6.96, Spring 72%/PF4.32,
        二次测试 55.7%/PF2.04, 突破回踩 50%/PF1.72; SOS/中继类已剔除)
"""

import json
import os
from collections import defaultdict, deque
from datetime import datetime

from wyckoff.datasource import fetch_kline, fetch_name
from wyckoff.events import detect_all
from wyckoff.indicators import add_indicators, find_pivots
from wyckoff.ninetests import nine_tests
from wyckoff.strategies import candidates as _candidates
from wyckoff.strategies import evaluators as _evaluators
from wyckoff.strategies.candidates import STRATEGY_CN, STRATEGY_ORDER
from wyckoff.strategies.constants import (
    DISCIPLINE_EVENT_WINDOW,
    LB_ENTRY_MARGIN,
    LB_MAX,
    LONG_EVENT_TYPES,
    LONG_MIN_CONF,
    SOS_CONFIRM_WINDOW,
    SPRING_CONFIRM_WINDOW,
    STRATEGY_DISCIPLINE,
    STRATEGY_LONG_LEFT,
    STRATEGY_VALUE_ACC,
    VA_EXCLUDE_BJ,
    VA_EXCLUDE_ST,
    VA_MIN_CONF,
    VA_MIN_PRICE,
)
from wyckoff.utils import normalize_symbol
from wyckoff.vsa import vsa_classify

__all__ = [
    "WyckoffStrategyManager",
    "STRATEGY_DISCIPLINE", "STRATEGY_VALUE_ACC", "STRATEGY_LONG_LEFT",
    "STRATEGY_ORDER", "STRATEGY_CN",
    "LONG_EVENT_TYPES", "SOS_CONFIRM_WINDOW", "SPRING_CONFIRM_WINDOW",
    "VA_EXCLUDE_BJ", "VA_EXCLUDE_ST", "VA_MIN_PRICE", "VA_MIN_CONF",
    "LONG_MIN_CONF", "LB_ENTRY_MARGIN", "LB_MAX", "DISCIPLINE_EVENT_WINDOW",
]


class WyckoffStrategyManager:
    """威科夫高胜率策略管理器 (兼容门面)。

    信号评估 / 候选生成 / 插件注册已拆分至 evaluators.py 与 candidates.py
    (纯函数); 本类保留数据目录与性能历史等实例状态, 并提供历史调用入口。
    """

    def __init__(self, data_dir="three_strategy_data"):
        self.data_dir = data_dir
        self.strategy_results = defaultdict(list)
        self.performance_log = deque(maxlen=100)

        # 创建数据目录 (exist_ok: 多线程并行选股时首条调用并发实例化, 防竞态)
        if data_dir and not os.path.exists(data_dir):
            os.makedirs(data_dir, exist_ok=True)

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

    # ── 信号评估 (委托 evaluators, 保持历史方法签名) ──────────────
    def evaluate_strategy_4(self, df, i, wevents, nt, vsa_labels,
                            stock_code=None, min_conf=90):
        """策略4: 模拟盘纪律策略 (强多头事件 + high conf + 硬门禁)。"""
        return _evaluators.evaluate_strategy_4(
            df, i, wevents, nt, vsa_labels,
            stock_code=stock_code, min_conf=min_conf)

    def evaluate_strategy_value_accumulation(self, df, i, wevents, wpivots):
        """综合选股·价值吸筹 (底部整固 + 20根内吸筹事件)。"""
        return _evaluators.evaluate_strategy_value_accumulation(
            df, i, wevents, wpivots)

    def evaluate_strategy_spring(self, df, i, wevents, nt, vsa_labels,
                                 stock_code=None):
        """Spring回踩确认策略。"""
        return _evaluators.evaluate_strategy_spring(
            df, i, wevents, nt, vsa_labels, stock_code=stock_code)

    def evaluate_strategy_long_buy(self, code, datalen=500):
        """威科夫完整做多买点 (可执行集合): 由低风险左侧到右侧加仓。"""
        return _evaluators.evaluate_strategy_long_buy(code, datalen=datalen)

    # ── 候选生成 (委托 candidates, 保持历史方法签名) ──────────────
    def scan_individual(self, code, df=None, min_conf=90, gates_ok=None,
                        name="", event_types=None, strategies=None):
        """对单只股票产出模拟盘候选 (纪律→左侧买点→价值吸筹)。"""
        return _candidates.scan_individual(
            code, df=df, min_conf=min_conf, gates_ok=gates_ok,
            name=name, event_types=event_types, strategies=strategies)

    @staticmethod
    def _discipline_latest(evs, n, min_conf=90, event_types=None):
        """纪律口径: 最近 N 根内的最新强多头事件 (conf≥min_conf)。"""
        return _candidates.discipline_latest(
            evs, n, min_conf=min_conf, event_types=event_types)

    def _value_accum_candidate(self, code, df, evs, piv, name=""):
        """策略管理器·价值吸筹候选 (底部整固 + 20根内吸筹事件, conf 下限)。"""
        return _candidates.value_accum_candidate(code, df, evs, piv, name=name)

    def _left_buy_candidate(self, code, df, evs, piv, name=""):
        """威科夫完整做多买点·左侧起仓 (独立赛道, 自带入场/止损/目标/盈亏比)。"""
        return _candidates.left_buy_candidate(code, df, evs, piv, name=name)

    @staticmethod
    def _is_low_quality(code, price=None, name=None) -> bool:
        """判断标的是否属低质池: 北交所 / ST·退市 / 低价 (可选)。"""
        return _candidates.is_low_quality(code, price=price, name=name)

    @staticmethod
    def _trading_discipline(**overrides):
        """策略4 实证的交易纪律 (止损/止盈/持有/同持上限/结构破位)。"""
        return _evaluators.trading_discipline(**overrides)

    @staticmethod
    def _check_discipline_gates(stock_code):
        """复刻模拟盘三道硬门禁 (统一收敛于 wyckoff.discipline, fail-close)。
        任一数据不可用即视为不满足 (严格拦截)。返回 {all_pass, details}。"""
        return _evaluators.check_discipline_gates(stock_code)

    def analyze_stock(self, code, datalen=1000, horizon=20, cost=0.004):
        """分析股票并应用策略。

        性能: 技术指标一次性预处理 (add_indicators 全量只跑一次), 逐bar仅做
        枢轴/事件/九大检验/VSA 判别 (自身即"截至当前时刻"语义, 不可省略)。
        """
        symbol = normalize_symbol(code)
        df = fetch_kline(symbol, datalen=datalen, scale=240)
        # 添加必要的技术指标 (滚动值在任意前缀上与逐bar重算一致, 复用即可)
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

        # 应用威科夫完整做多买点 (整段历史识别 + 近端可执行买点; 需完整 df,
        # 故放在逐bar循环之后, 基于 cached 全量K线评估一次)
        try:
            long_buy_result = _evaluators.evaluate_strategy_long_buy(
                symbol, datalen=datalen)
            if long_buy_result and long_buy_result.get("signals"):
                current_analysis["strategies_found"].append(long_buy_result)
        except Exception:
            pass

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
            },
            "strategy_long_buy": {
                "name": "威科夫完整做多买点 (可执行集合)",
                "description": "教学文档框架实证筛选后的可执行买点: 底部吸筹左侧(ST/Spring/二次测试/末期回踩) 为主力, 突破后缩量回踩(BU/LPS) 仅作左侧已起仓后的加仓确认; 实证负期望的SOS/中继突破/中继回踩已剔除",
                "characteristics": ["左侧主力: ST 胜率85% PF6.96, Spring 胜率72% PF4.32, 二次测试 胜率55.7% PF2.04",
                                    "加仓确认: 突破回踩(BU/LPS) 胜率50% PF1.72",
                                    "右侧加仓纪律: 右买点须有45根内价位更低的左买点为前提",
                                    "30只×500根回测: 4类均正期望; SOS/中继类已停用"],
                "conditions": ["不在派发/下跌阶段买入",
                               "每买点带入场/止损/目标/仓位档位(左侧小仓紧止损, 右侧确认可加)",
                               "识别: wyckoff.buypoints / 扫描「威科夫买点」 / analyze_stock"],
            }
        }


def main():
    """主函数 (兼容入口) — 实际实现在 wyckoff.strategies.cli。"""
    from wyckoff.strategies.cli import main as _cli_main
    _cli_main()


if __name__ == "__main__":
    main()
