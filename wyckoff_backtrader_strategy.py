#!/usr/bin/env python3
"""
威科夫策略回测 - 使用 backtrader 框架
基于 Strategy 2: 威科夫事件 + 高价值VSA标签
"""

import backtrader as bt
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

from wyckoff.datasource import fetch_kline
from wyckoff.indicators import find_pivots, add_indicators
from wyckoff.events import detect_all
from wyckoff.ninetests import nine_tests
from wyckoff.vsa import vsa_classify


class WyckoffStrategy(bt.Strategy):
    """基于威科夫事件 + 高价值VSA的回测策略"""
    
    params = (
        ('horizon', 20),         # 持有期天数
        ('cost_pct', 0.004),     # 交易成本 0.4%
        ('confidence_thresh', 85),  # 置信度阈值
    )
    
    def __init__(self):
        self.trades = []
        self.order = None
        self.buy_signal_count = 0
        self.sell_signal_count = 0
        
    def next(self):
        """每根K线执行一次"""
        # 只在有仓位时处理出场，无仓位时处理入场
        if self.position:
            self._check_exit()
        else:
            self._check_entry()
    
    def _check_entry(self):
        """检查入场信号"""
        i = len(self.data) - 1
        if i < 90:
            return
        
        # 使用截至当前K线的数据进行分析
        df = self.data0.get_df().iloc[:i+1].copy()
        df = add_indicators(df, symbol=self.data._symbol)
        
        if len(df) < 150:
            return
        
        wpivots = find_pivots(df, order=6)
        wevents = detect_all(df, wpivots)
        nt = nine_tests(df, wevents, wpivots)
        vsa_labels = vsa_classify(df, scale=240)
        
        # 策略2: 威科夫事件 + 高价值VSA标签
        if nt["buy_passed"] >= 7:
            high_events = [e for e in wevents if e["type"] in ["Spring", "Shakeout", "SOS", "JOC", "ST"] and e.get("conf", 0) > 80]
            if high_events:
                high_value_vsa = ["CHOC", "DEM", "SUP", "LPS", "ST", "Spring"]
                high_vsa = [s for s in vsa_labels if s["label"] in high_value_vsa and s.get("features", {}).get("vr", 0) >= 1.5]
                if high_vsa:
                    event_conf = max([e.get("conf", 0) for e in high_events])
                    vsa_conf = max([s.get("conf", 0) for s in high_vsa])
                    combined_conf = min(95, (event_conf * 0.6 + vsa_conf * 0.4))
                    
                    if combined_conf >= self.p.confidence_thresh:
                        # 入场：次日开盘价
                        entry_price = self.data.open[0]
                        self.buy(
                            price=entry_price,
                            exectype=bt.Order.Market
                        )
                        self.buy_signal_count += 1
                        self.log(f"买入信号: 置信度={combined_conf:.1f}, 事件={high_events[0]['type']}, VSA={high_vsa[0]['label']}")
    
    def _check_exit(self):
        """检查出场信号"""
        i = len(self.data) - 1
        horizon = self.p.horizon
        
        # 计算目标出场点（持有horizon天）
        exit_idx = min(i + horizon, len(self.data.history()) - 1)
        
        # 检查是否达到止盈或止损条件
        # 这里使用简化的持有期出场模型
        entry_price = self.position.price
        
        # 扫描持有期内的最高/最低价
        start_idx = len(self.data) - 1 - horizon
        if start_idx < 0:
            start_idx = 0
            
        trade_returns = []
        for j in range(len(self.data) - 1, max(start_idx - 1, 0) - 1, -1):
            close = self.data.close[j]
            high = self.data.high[j]
            low = self.data.low[j]
            
            # 简单退出逻辑：持有期结束或触及目标
            # 这里使用：持有horizon天后平仓，或触及一定比例的止盈/止损
            
            # 记录交易
            ret = (close / entry_price - 1) - self.p.cost_pct
            trade_returns.append(ret)
        
        # 使用 horizon 天 later 的收盘价平仓
        if exit_idx >= 0 and exit_idx < len(self.data.close):
            exit_price = self.data.close[exit_idx]
            ret = (exit_price / entry_price - 1) - self.p.cost_pct
            
            # 记录交易
            self.trades.append({
                'entry_price': entry_price,
                'exit_price': exit_price,
                'return': ret,
                'index': len(self.data) - 1,
                'exit_index': exit_idx,
            })
            
            self.log(f"卖出: 入价={entry_price:.2f}, 出价={exit_price:.2f}, 收益率={ret*100:.2f}%")
            
            # 平仓
            self.close()
    
    def log(self, text, dt=None):
        '''日志记录函数'''
        dt = dt or self.datas[0].datetime.date(0)
        print(f'{dt.isoformat()} {text}')
    
    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
        if order.status in [order.Completed]:
            if order.isbuy():
                self.log(f'买单执行: 价格={order.executed.price:.2f}, 成本={order.executedcommission:.4f}')
            else:
                self.log(f'卖单执行: 价格={order.executed.price:.2f}, 成本={order.executedcommission:.4f}')
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log(f'订单cancel/margin/rejected')
        self.order = None
    
    def notify_trade(self, trade):
        if trade.isclosed:
            self.log(f'交易闭合: 毛收益={trade.pnl:.2f}, 净收益={trade.pnlcomm:.2f}')


def run_backtest(symbol='sh600036', datalen=250, horizon=20, cost=0.004):
    """运行单股票回测"""
    print(f"\n=== 回测 {symbol} ===")
    print(f"参数: 数据长度={datalen}, 持有期={horizon}天, 交易成本={cost*100:.1f}%")
    
    # 获取数据
    df = fetch_kline(symbol, datalen=datalen, scale=240)
    df = add_indicators(df, symbol=symbol)
    
    if len(df) < 150:
        print(f"数据不足，无法回测 {symbol}")
        return None
    
    # 设置 day 列为 DatetimeIndex，供 backtrader 使用
    df = df.set_index('day')
    df = df.sort_index()
    
    # 创建 Cerebro 引擎
    cerebro = bt.Cerebro(stdstats=False)
    
    # 创建 backtrader DataFeed
    data = bt.feeds.PandasData(dataname=df)
    
    cerebro.adddata(data)
    cerebro.addstrategy(WyckoffStrategy)
    
    # 设置参数
    cerebro.broker.set_cash(100000.0)
    cerebro.broker.setcommission(commission=cost)  # 设置佣金
    
    print(f'初始资金: {cerebro.broker.get_value():.2f}')
    
    # 运行回测
    cerebro.run()
    
    # 获取结果
    final_value = cerebro.broker.get_value()
    print(f'最终资金: {final_value:.2f}')
    print(f'总收益: {(final_value - 100000) / 100000 * 100:.2f}%')
    
    # 输出交易记录
    strategy = cerebro.run()[0]
    if strategy.trades:
        trades = strategy.trades
        total_trades = len(trades)
        wins = [t for t in trades if t['return'] > 0]
        losses = [t for t in trades if t['return'] <= 0]
        
        win_rate = len(wins) / total_trades * 100 if total_trades > 0 else 0
        total_return = sum(t['return'] for t in trades) * 100
        avg_return = total_return / total_trades if total_trades > 0 else 0
        
        # 计算最大回撤
        cumulative = [1.0]
        for t in trades:
            cumulative.append(cumulative[-1] * (1 + t['return']))
        peak = max(cumulative)
        trough = min(cumulative)
        max_dd = (peak - trough) / peak * 100 if peak > 0 else 0
        
        # 计算夏普比率（年化，假设250个交易日）
        returns = [t['return'] for t in trades]
        if len(returns) > 1 and np.std(returns) > 0:
            # 年化夏普 = 平均收益/标准差 * sqrt(250)
            sharpe = np.mean(returns) / np.std(returns) * np.sqrt(250)
        else:
            sharpe = 0
        
        print(f'\n=== 回测结果 ===')
        print(f'总交易次数: {total_trades}')
        print(f'胜率: {win_rate:.2f}%')
        print(f'平均单笔收益: {avg_return:.2f}%')
        print(f'总收益: {total_return:.2f}%')
        print(f'最大回撤: {max_dd:.2f}%')
        print(f'夏普比率: {sharpe:.2f}')
        print(f'盈利交易: {len(wins)}, 亏损交易: {len(losses)}')
        
        # 打印前几笔交易详情
        print('\n前5笔交易详情:')
        for t in trades[:5]:
            print(f'  入价: {t["entry_price"]:.2f}, 出价: {t["exit_price"]:.2f}, 收益: {t["return"]*100:.2f}%')
    else:
        print('无交易记录')
    
    # 绘图
    try:
        cerebro.plot(iofunc=lambda fig: fig.set_size_inches(12, 6))[0][0]
    except Exception as e:
        print(f'绘图错误: {e}')
    
    return cerebro


def run_multi_backtest(stocks=None, datalen=250, horizon=20, cost=0.004):
    """运行多股票回测"""
    if stocks is None:
        stocks = ['sh600036', 'sz000001', 'sh601318', 'sz000858', 'sh600030']
    
    print(f"\n=== 多股票回测 ===")
    print(f'股票: {stocks}')
    print(f'参数: 数据长度={datalen}, 持有期={horizon}天, 交易成本={cost*100:.1f}%')
    
    cerebro = bt.Cerebro(stdstats=False)
    
for symbol in stocks:
        df = fetch_kline(symbol, datalen=datalen, scale=240)
        df = add_indicators(df, symbol=symbol)
        
        if len(df) < 150:
            print(f'跳过 {symbol}: 数据不足')
            continue
        
        # 设置 day 列为 DatetimeIndex
        df = df.set_index('day')
        df = df.sort_index()
        
        data = bt.feeds.PandasData(dataname=df)
        cerebro.adddata(data)
    
    cerebro.addstrategy(WyckoffStrategy)
    cerebro.broker.set_cash(100000.0)
    cerebro.broker.set_commission(commission=cost)
    
    print(f'初始总资金: {cerebro.broker.get_value():.2f}')
    
    cerebro.run()
    
    final_value = cerebro.broker.get_value()
    print(f'最终总资金: {final_value:.2f}')
    print(f'总收益: {(final_value - 100000*len(stocks)) / (100000*len(stocks)) * 100:.2f}%')
    
    # 输出每只股票的交易情况
    for i, datafeed in enumerate(cerebro.getdatas()):
        symbol = data._name if hasattr(data, '_name') else f'stock_{i}'
        strategy = cerebro.run()[0]
        if strategy.trades:
            trades = strategy.trades
            total_return = sum(t['return'] for t in trades) * 100
            wins = [t for t in trades if t['return'] > 0]
            win_rate = len(wins) / len(trades) * 100
            print(f'{symbol}: {len(trades)}笔交易, 总收益={total_return:.2f}%, 胜率={win_rate:.1f}%')
        else:
            print(f'{symbol}: 无交易')
    
    return cerebro


def main():
    """主函数"""
    print("=== 威科夫策略 backtrader 回测 ===")
    print("策略: 威科夫事件 + 高价值VSA标签 (Strategy 2)")
    print()
    
    # 单股票回测示例
    cerebro = run_backtest(
        symbol='sh600036',
        datalen=250,
        horizon=20,
        cost=0.004
    )
    
    # 多股票回测
    print('\n\n=== 多股票回测 ===')
    cerebro2 = run_multi_backtest(
        stocks=['sh600036', 'sz000001', 'sh601318'],
        datalen=250,
        horizon=20,
        cost=0.004
    )
    
    print('\n=== 回测完成 ===')


if __name__ == "__main__":
    main()