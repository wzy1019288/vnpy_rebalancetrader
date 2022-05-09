from typing import TYPE_CHECKING
from enum import Enum
from math import floor, ceil

from vnpy.trader.utility import round_to
from vnpy.trader.constant import Direction, Offset
from vnpy.trader.object import TickData, OrderData, TradeData, ContractData

if TYPE_CHECKING:
    from .engine import RebalanceEngine, DfRebalanceEngine


class AlgoStatus(Enum):
    """算法状态"""
    WAITING = "等待"
    RUNNING = "运行"
    PAUSED = "暂停"
    FINISHED = "结束"
    STOPPED = "撤销"


class TwapAlgo:
    """TWAP算法"""

    def __init__(
        self,
        engine: "RebalanceEngine",
        vt_symbol: str,
        direction: Direction,
        limit_price: float,
        total_volume: int,
        time_interval: int,
        total_time: int
    ) -> None:
        """构造函数"""
        self.engine: RebalanceEngine = engine

        # 参数
        self.vt_symbol: str = vt_symbol
        self.direction: Direction = direction
        self.limit_price: float = limit_price
        self.total_volume: int = total_volume
        self.time_interval: int = time_interval
        self.total_time: int = total_time

        # 变量
        self.status: AlgoStatus = AlgoStatus.WAITING
        self.timer_count: int = 0
        self.total_count: int = 0
        self.traded_volume: int = 0
        self.active_orderids: set[str] = set()
        self.to_run: bool = False

    def on_trade(self, trade: TradeData):
        """成交推送"""
        # 累加成交量
        self.traded_volume += trade.volume

    def on_order(self, order: OrderData) -> None:
        """委托推送"""
        # 移除活动委托号
        if not order.is_active() and order.vt_orderid in self.active_orderids:
            self.active_orderids.remove(order.vt_orderid)

        # 检查是否要执行
        if self.to_run and not self.active_orderids:
            self.to_run = False
            self.run()

    def on_timer(self) -> None:
        """定时推送"""
        self.total_count += 1

        # 间隔检查
        self.timer_count += 1
        if self.timer_count < self.time_interval:
            return
        self.timer_count = 0

        # 委托检查
        if self.active_orderids:
            for vt_orderid in self.active_orderids:
                self.engine.cancel_order(self, vt_orderid)

            self.to_run = True
            return

        # 执行下单
        self.run()

    def run(self) -> None:
        """执行下单"""
        # 过滤无行情的情况
        tick: TickData = self.engine.get_tick(self.vt_symbol)
        if not tick:
            return

        contract: ContractData = self.engine.get_contract(self.vt_symbol)

        # 检查价格满足条件
        if self.direction == Direction.LONG and tick.ask_price_1 > self.limit_price:
            return
        elif self.direction == Direction.SHORT and tick.bid_price_1 < self.limit_price:
            return

        # 计算剩余委托量
        volume_left: int = self.total_volume - self.traded_volume

        # 计算剩余周期数
        time_left: int = self.total_time - self.total_count
        interval_left: int = floor(time_left / self.time_interval) + 1

        # 计算本轮委托数量
        order_volume: int = ceil(volume_left / interval_left)
        order_volume: int = min(order_volume, volume_left)

        # 计算委托价格，1档盘口超加1个pricetick
        if self.direction == Direction.LONG:
            order_price: float = tick.ask_price_1 + contract.pricetick
        else:
            order_price: float = tick.bid_price_1 - contract.pricetick

        # 发出委托
        vt_orderids = self.engine.send_order(
            self,
            self.vt_symbol,
            self.direction,
            order_price,
            order_volume
        )

        self.active_orderids.update(vt_orderids)


class DfTwapAlgo:
    """盾枫TWAP算法"""

    def __init__(
        self,
        engine: "DfRebalanceEngine",
        vt_symbol: str,
        direction: Direction,
        # limit_price: float,
        total_volume: int,
        time_interval: int,
        # total_time: int
        vol_percent: float = 0.1
    ) -> None:
        """构造函数"""
        self.engine: DfRebalanceEngine = engine

        # 参数
        self.vt_symbol: str = vt_symbol
        self.direction: Direction = direction
        # self.limit_price: float = limit_price
        self.total_volume: int = total_volume   # 总目标成交量
        self.time_interval: int = time_interval # 时间间隔
        # self.total_time: int = total_time
        self.vol_percent = vol_percent          #　对手价盘口的百分比

        # 变量
        self.status: AlgoStatus = AlgoStatus.WAITING
        self.timer_count: int = 0
        # self.total_count: int = 0
        self.traded_volume: int = 0
        self.active_orderids: set[str] = set()
        self.to_run: bool = False

    def on_trade(self, trade: TradeData):
        """成交推送"""
        # 累加成交量
        if trade.offset == Offset.OPEN:
            self.traded_volume += trade.volume
        elif trade.offset in {Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY}:
            self.traded_volume -= trade.volume
            
        self.engine.write_log(f'[成交推送][{self.vt_symbol}] : \n\
                                \t成交时间: {trade.datetime}\n\
                                \t成交动作: {trade.direction.value} {trade.offset.value}\n\
                                \t成交价格: {trade.price}\n\
                                \t成交数量: {trade.volume}\
                                    ')

    def on_order(self, order: OrderData) -> None:
        """委托推送"""
        # 移除活动委托号
        if not order.is_active() and order.vt_orderid in self.active_orderids:
            self.active_orderids.remove(order.vt_orderid)

        # 检查是否要执行
        if self.to_run and not self.active_orderids:
            self.to_run = False
            self.run()

    def on_timer(self) -> None:
        """定时推送"""

        # 间隔检查
        self.timer_count += 1
        self.engine.save_data("rebalance_trader_data_backup.json")
        if self.timer_count < self.time_interval:
            return
        self.timer_count = 0

        # 委托检查
        if self.active_orderids:
            for vt_orderid in self.active_orderids:
                self.engine.cancel_order(self, vt_orderid)

            self.to_run = True
            return

        # 执行下单
        self.run()

    def run(self) -> None:
        """执行下单"""
        # 过滤无行情的情况
        tick: TickData = self.engine.get_tick(self.vt_symbol)
        if not tick:
            return

        contract: ContractData = self.engine.get_contract(self.vt_symbol)

        # 计算剩余委托量
        volume_left: int = abs(self.total_volume - self.traded_volume)
        # 已完成所有交易, 则过滤
        if not volume_left:
            return

        # 计算委托价格，1档盘口超加1个pricetick; 计算委托成交量，1档盘口量的百分比
        if self.direction == Direction.LONG:
            order_price: float = tick.ask_price_1 + contract.pricetick
            order_volume = tick.ask_volume_1 * self.vol_percent
            order_volume = round_to(order_volume, contract.min_volume)
            order_volume = min(order_volume, volume_left)
        else:
            order_price: float = tick.bid_price_1 - contract.pricetick
            order_volume = tick.bid_volume_1 * self.vol_percent
            order_volume = round_to(order_volume, contract.min_volume)
            order_volume = min(order_volume, volume_left)

        if order_volume == 0:
                # self.engine.write_log(f'{self.vt_symbol}: order_volume[{order_volume}]需要加{contract.min_volume}')
                # self.engine.write_log(f'{self.vt_symbol}: \n \
                #                         last_price: {tick.last_price} \n\
                #                         last_volume: {tick.last_volume} \n\
                #                         open_interest: {tick.open_interest} \n\
                #                         ask_price_1: {tick.ask_price_1} \n\
                #                         ask_volume_1: {tick.ask_volume_1} \n\
                #                         bid_price_1: {tick.bid_price_1} \n\
                #                         bid_volume_1: {tick.bid_volume_1} \n\
                #                         datetime: {tick.datetime} \n\
                #                         localtime: {tick.localtime} \n')
                order_volume = contract.min_volume
                if order_volume == 0:
                    self.engine.write_log(f'[{self.vt_symbol}] order_volume和contract.min_volume都为0')

        # 发出委托
        vt_orderids = self.engine.send_order(
            self,
            self.vt_symbol,
            self.direction,
            order_price,
            order_volume
        )

        self.active_orderids.update(vt_orderids)
