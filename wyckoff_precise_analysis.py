#!/usr/bin/env python3
"""威科夫项目高胜率策略精确定位"""

from collections import defaultdict

import numpy as np

from wyckoff.datasource import fetch_kline
from wyckoff.events import detect_all
from wyckoff.indicators import add_indicators, find_pivots
from wyckoff.ninetests import nine_tests
from wyckoff.utils import normalize_symbol
from wyckoff.vsa import vsa_classify


def precise_strategy_analysis(code, datalen=1000, horizon=20, cost=0.004):
    """精确策略分析"""

    symbol = normalize_symbol(code)
    df = fetch_kline(symbol, datalen=datalen, scale=240)
    # 添加必要的技术指标
    df = add_indicators(df, symbol=symbol)

    if len(df) < 150:
        return {"error": "数据不足"}

    # 存储不同策略的回测结果
    strategy_results = defaultdict(list)

    # 从第90根K线开始回测（避免前期数据不足）
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

        # 计算收益
        end_idx = min(i + horizon, len(df) - 1)
        close = df["close"].values

        # 计算持有收益（费后）
        if end_idx < len(close):
            ret = (close[end_idx] / close[i + 1] - 1) - cost
        else:
            continue

        # 策略1: 威科夫7项通过 + 确认事件
        if nt["buy_passed"] >= 7:
            # 找到最近的确认事件
            confirmed_events = [e for e in wevents if e.get("confirmed") is True and e["idx"] >= i-20]
            if confirmed_events:
                strategy_results["wyckoff_7plus_confirmed"].append(ret)

        # 策略2: 高胜率VSA标签
        high_value_vsa = ["CHOC", "DEM", "SUP", "LPS", "ST", "Spring"]
        high_vsa = [s for s in vsa_labels if s["label"] in high_value_vsa]
        if high_vsa:
            strategy_results["high_value_vsa"].append(ret)

        # 策略3: 威科夫事件 + VSA组合
        if nt["buy_passed"] >= 7:
            # 检查是否有高价值事件
            high_events = [e for e in wevents if e["type"] in ["Spring", "Shakeout", "SOS", "JOC", "ST"]]
            if high_events:
                strategy_results["wyckoff_plus_vsa"].append(ret)

        # 策略4: 多重确认信号
        if nt["buy_passed"] >= 6:
            confirmed_events = [e for e in wevents if e.get("confirmed") is True and e["idx"] >= i-10]
            if confirmed_events:
                high_vsa = [s for s in vsa_labels if s["label"] in ["CHOC", "DEM", "SUP"]]
                if high_vsa:
                    strategy_results["multi_confirmation"].append(ret)

    # 计算各策略胜率
    results = {}

    for strategy_name, returns in strategy_results.items():
        if len(returns) >= 5:  # 至少5个样本才计算
            win_rate = (np.array(returns) > 0).mean() * 100
            avg_return = np.mean(returns) * 100
            results[strategy_name] = {
                "count": len(returns),
                "win_rate": win_rate,
                "avg_return": avg_return
            }

    return {
        "total_samples": len(df),
        "strategies": results
    }


def format_precise_results(result, stock_code):
    """格式化精确结果"""
    print(f"=== 精确高胜率策略分析 - {stock_code} ===")
    print("数据周期: 1000根K线")
    print("回测周期: 20天")
    print("交易成本: 0.4%")
    print()

    if "error" in result:
        print(f"错误: {result['error']}")
        return

    if not result["strategies"]:
        print("没有足够的样本数据进行分析")
        return

    print("高胜率策略表现:")
    print("策略名称              | 样本数 | 胜率(%) | 平均收益(%)")
    print("-" * 55)

    for strategy_name, stats in result["strategies"].items():
        if stats:
            # 策略名称美化显示
            display_name = strategy_name.replace("_", " ").title()
            print(f"{display_name:<20} | {stats['count']:6d} | {stats['win_rate']:7.1f} | {stats['avg_return']:10.2f}")


if __name__ == "__main__":
    # 测试股票
    stock_code = "sh600036"

    print("正在执行精确策略分析...")
    result = precise_strategy_analysis(stock_code, datalen=1000)
    format_precise_results(result, stock_code)

    print()
    print("=== 策略说明 ===")
    print("1. wyckoff_7plus_confirmed: 威科夫7项以上通过 + 确认事件")
    print("2. high_value_vsa: 高胜率VSA标签信号")
    print("3. wyckoff_plus_vsa: 威科夫事件 + 高价值VSA标签")
    print("4. multi_confirmation: 多重确认信号组合")
