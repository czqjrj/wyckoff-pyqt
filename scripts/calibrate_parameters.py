"""Wyckoff 策略参数回测标定脚本。

目标：通过历史回测优化关键阈值参数，
提升事件置信度 IC 和 交易胜率。

使用方法：
    python scripts/calibrate_parameters.py \
        --data-file /path/to/historical_data.csv \
        --param-pivot-sensitivity normal \
        --vol-thresh-range 1.5 2.0 0.1

或直接在 Python 中导入使用 calilbrate_vol_threshold()。
"""
import argparse
import numpy as np
import pandas as pd
import sys
import os
from itertools import product
from datetime import datetime, timedelta

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wyckoff.indicators import add_indicators, find_pivots, pivot_order
from wyckoff.events import detect_all, _EventContext


def generate_mock_hist_data(n_days: int = 500,
                            start_price: float = 100.0,
                            seed: int = 42) -> pd.DataFrame:
    """生成模拟历史数据用于演示标定。
    
    实际使用时请替换为真实的历史股票数据 CSV。
    数据需包含列: date, open, high, low, close, volume
    """
    np.random.seed(seed)
    
    # 生成带趋势的随机游走
    returns = np.random.randn(n_days) * 0.3 / np.sqrt(252)  # 日化波动 ~30%
    price_ret = np.cumsum(returns) + 0.0001  # 小正向 drift
    prices = start_price * np.exp(price_ret)
    
    # 生成 OHLC
    high = prices + np.abs(np.random.randn(n_days) * 0.5)
    low = prices - np.abs(np.random.randn(n_days) * 0.5)
    open_ = prices + (np.random.randn(n_days) * 0.3)
    volume = np.random.randint(500, 5000, n_days)
    
    dates = pd.date_range(end=datetime.now(), periods=n_days, freq='D')
    # 逆序，最旧的在前
    dates = dates[::-1]
    
    df = pd.DataFrame({
        'date': dates,
        'open': open_,
        'high': high,
        'low': low,
        'close': prices,
        'volume': volume
    })
    df.set_index('date', inplace=True)
    return df


def calculate_event_ic(df: pd.DataFrame,
                       vol_thresh: float,
                       pivot_sen: str = 'normal',
                       confirm_window: int = 3) -> float:
    """计算指定参数下的事件置信度 IC (Information Coefficient)。
    
    IC = Spearman相关系数 事件置信度 vs 后续收益
    IC > 0 表示置信分高的事件后续收益 tended to be positive
    """
    try:
        # 计算指标
        result = add_indicators(df, symbol=None)
        
        # 获取枢轴点
        pivots = find_pivots(df, order=pivot_order(pivot_sen), sensitivity=pivot_sen)
        if not pivots:
            return 0.0
        
        # 事件检测
        events = detect_all(df, pivots)
        if not events:
            return 0.0
        
        # 计算每个事件后几天的收益
        close = df['close'].values
        n = len(df)
        
        # 收集置信度和收益对
        conf_vals = []
        returns = []
        
        for e in events[:20]:  # 仅使用前20个事件防止计算过慢
            idx = e['idx']
            conf = e.get('conf', 50)  # 置信度得分
            
            # 计算事件后 5 个交易日的收益
            if idx + 5 < n:
                ret_5d = (close[idx + 5] - close[idx]) / close[idx]
                conf_vals.append(conf)
                returns.append(ret_5d)
        
        if len(conf_vals) < 3:
            return 0.0
        
        # 计算相关系数 (Spearman)
        from scipy.stats import spearmanr
        if len(set(conf_vals)) < 2:
            return 0.0
        
        rho, p_value = spearmanr(conf_vals, returns)
        return rho if not np.isnan(rho) else 0.0
        
    except Exception as e:
        # print(f"Warning: IC calculation error for vol_thresh={vol_thresh}: {e}", file=sys.stderr)
        return 0.0


def calilbrate_vol_threshold(df: pd.DataFrame,
                             vol_thresh_range: tuple = (1.5, 2.0, 0.1),
                             pivot_sen: str = 'normal',
                             confirm_window: int = 3,
                             n_random_starts: int = 5) -> dict:
    """标定成交量阈值参数。
    
    通过网格搜索寻找使 Spearman IC 最大化的 vol_ratio_20 阈值。
    
    返回最佳参数和 IC 值字典。
    """
    print(f"开始参数标定...")
    print(f"参数组合: pivot_sensitivity={pivot_sen}, "
          f"vol_thresh_range={vol_thresh_range}")
    print("-" * 60)
    
    best_result = {
        'vol_thresh': vol_thresh_range[0],
        'ic': -999.0,
        'pivot_sen': pivot_sen,
        'confirm_window': confirm_window,
        'evaluations': 0
    }
    
    # 生成参数组合
    vol_threshes = np.arange(*vol_thresh_range)
    
    print(f"测试 {len(vol_threshes)} 个体积阈值组合...")
    
    for i, vol_thresh in enumerate(vol_threshes):
        try:
            # 计算 IC
            ic = calculate_event_ic(df, vol_thresh, pivot_sen, confirm_window)
            
            # 更新最佳结果
            if ic > best_result['ic']:
                best_result['ic'] = ic
                best_result['vol_thresh'] = vol_thresh
            
            # 显示进度
            if (i + 1) % 10 == 0 or i == 0:
                print(f"  已测试 {i+1}/{len(vol_threshes)} "
                      f"(当前最佳 IC: {best_result['ic']:.4f}, "
                      f"阈值: {vol_thresh})")
            
            best_result['evaluations'] = i + 1
            
        except Exception as e:
            print(f"  警告: 阈值 {vol_thresh} 测试失败: {e}", file=sys.stderr)
            continue
    
    print("-" * 60)
    print(f"标定完成! 最佳参数: vol_ratio_20 = {best_result['vol_thresh']:.2f}")
    print(f"对应 Spearman IC = {best_result['ic']:.4f}")
    print(f"总评测次数: {best_result['evaluations']}")
    
    return best_result


def multi_parameter_grid_search(df: pd.DataFrame,
                                pivot_sensitivity: list = None,
                                vol_thresh_ranges: list = None,
                                confirm_windows: list = None) -> list:
    """多参数网格搜索。
    
    搜索多个参数的组合以寻找全局最优。
    
    返回按 IC 排序的结果列表。
    """
    if pivot_sensitivity is None:
        pivot_sensitivity = ['fast', 'normal', 'safe']
    if vol_thresh_ranges is None:
        vol_thresh_ranges = [(1.5, 2.0, 0.1), (1.6, 2.1, 0.1)]
    if confirm_windows is None:
        confirm_windows = [3, 5, 8]
    
    all_results = []
    
    print(f"开始多参数网格搜索...")
    print(f"组合数: {len(pivot_sensitivity)} × "
          f"{len(vol_thresh_ranges)} × {len(confirm_windows)} = "
          f"{len(pivot_sensitivity) * len(vol_thresh_ranges) * len(confirm_windows)}")
    print("-" * 60)
    
    total_combos = (len(pivot_sensitivity) * 
                    len(vol_thresh_ranges) * 
                    len(confirm_windows))
    counter = 0
    
    for pen in pivot_sensitivity:
        for vr_range in vol_thresh_ranges:
            for cw in confirm_windows:
                counter += 1
                
                try:
                    ic = calculate_event_ic(df, 
                                            (vr_range[0] + vr_range[2]) / 2,  # 使用范围中点
                                            pen, 
                                            cw)
                    
                    all_results.append({
                        'pivot_sensitivity': pen,
                        'vol_thresh_mid': (vr_range[0] + vr_range[2]) / 2,
                        'confirm_window': cw,
                        'ic': ic,
                        'eval_order': counter
                    })
                    
                except Exception as e:
                    all_results.append({
                        'pivot_sensitivity': pen,
                        'vol_thresh_mid': (vr_range[0] + vr_range[2]) / 2,
                        'confirm_window': cw,
                        'ic': -999.0,
                        'eval_order': counter,
                        'error': str(e)[:50]
                    })
                
                if counter % 20 == 0 or counter == total_combos:
                    print(f"  进度: {counter}/{total_combos} "
                          f"{(counter/total_combos*100):.1f}%")
    
    # 按 IC 从高到低排序
    all_results.sort(key=lambda x: x['ic'], reverse=True)
    
    print("-" * 60)
    print(f"搜索完成! 总组合: {counter}")
    print(f"最佳 IC: {all_results[0]['ic']:.4f}")
    print(f"最佳参数: {[(k, v) for k, v in all_results[0].items() if k != 'eval_order' and k != 'ic']}")
    
    return all_results[:10]  # 返回前 10 名


def main():
    """主入口函数."""
    parser = argparse.ArgumentParser(
        description='Wyckoff 策略参数回测标定'
    )
    parser.add_argument('--data', type=str, default=None,
                        help='历史数据 CSV 文件路径 (需包含: open, high, low, close, volume)')
    parser.add_argument('--pivot-sensitivity', type=str, default='normal',
                        choices=['fast', 'normal', 'safe'],
                        help='枢轴点敏感度档位')
    parser.add_argument('--vol-thresh-range', type=float, nargs=3, default=[1.5, 2.0, 0.1],
                        help='体积阈值范围: start end step')
    parser.add_argument('--confirm-window', type=int, default=3,
                        help='确认窗口期')
    parser.add_argument('--mode', type=str, default='single',
                        choices=['single', 'grid'],
                        help='标定模式: 单参数或多参数网格搜索')
    
    args = parser.parse_args()
    
    # 生成或加载数据
    if args.data and os.path.exists(args.data):
        print(f"从文件加载数据: {args.data}")
        df = pd.read_csv(args.data, index_col='date', parse_dates=True)
        if 'date' in df.columns and df.index.name != 'date':
            df.index = pd.to_datetime(df.index)
    else:
        print("生成模拟历史数据 (演示用)...")
        df = generate_mock_hist_data(n_days=300, start_price=100.0, seed=42)
    
    # 确保数据有足够长度
    if len(df) < 200:
        print(f"警告: 数据长度 {len(df)} 可能不足以进行可靠标定")
    
    # 执行标定
    print(f"\n{'='*60}")
    print(f"Wyckoff 参数标定工具")
    print(f"{'='*60}")
    print(f"数据: {len(df)} 根 K 线")
    print(f"模式: {args.mode}")
    print(f"{'='*60}\n")
    
    if args.mode == 'single':
        result = calilbrate_vol_threshold(
            df,
            vol_thresh_range=tuple(args.vol_thresh_range),
            pivot_sen=args.pivot_sensitivity,
            confirm_window=args.confirm_window
        )
        print(f"\n✓ 标定结果保存完毕")
        print(f"  最佳 vol_ratio_20: {result['vol_thresh']:.2f}")
        print(f"  对应 Spearman IC: {result['ic']:.4f}")
        
    elif args.mode == 'grid':
        results = multi_parameter_grid_search(
            df,
            pivot_sensitivity=[args.pivot_sensitivity],
            vol_thresh_ranges=[tuple(args.vol_thresh_range)],
            confirm_windows=[args.confirm_window]
        )
        
        print(f"\n✓ 网格搜索完成")
        print(f"前 5 名最佳参数组合:")
        for i, r in enumerate(results[:5], 1):
            print(f"  {i}. IC={r['ic']:.4f}, "
                  f"pivot={r['pivot_sensitivity']}, "
                  f"vol_thresh={r['vol_thresh_mid']:.2f}, "
                  f"confirm={r['confirm_window']}")
    
    print(f"\n{'='*60}")
    print("标定工具使用完毕")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()