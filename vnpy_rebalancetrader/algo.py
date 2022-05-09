from typing import TYPE_CHECKING
from enum import Enum
from math import floor, ceil

from vnpy.trader.utility import round_to
from vnpy.trader.constant import Direction, Offset
from vnpy.trader.object import TickData, OrderData, TradeData, ContractData

if TYPE_CHECKING:
    from .engine import DfRebalanceEngine


class AlgoStatus(Enum):
    """算法状态"""
    WAITING = "等待"
    RUNNING = "运行"
    PAUSED = "暂停"
    FINISHED = "结束"
    STOPPED = "撤销"


class DfTwapAlgo:
    """盾枫TWAP算法"""

    def __init__(
        self,
        engine: "DfRebalanceEngine",
        vt_symbol: str,
        direction: Direction,
        total_volume: int,
        time_interval: int,
        vol_percent: float = 0.1
    ) -> None:
        """构造函数"""
        self.engine: DfRebalanceEngine = engine

        # 参数
        self.vt_symbol: str = vt_symbol
        self.direction: Direction = direction
        self.total_volume: int = total_volume           # 总目标成交量
        if self.direction == Direction.SHORT:
            self.total_volume = -self.total_volume
        self.time_interval: int = time_interval         # 时间间隔
        self.vol_percent = vol_percent                  #　对手价盘口的百分比

        # 变量
        self.status: AlgoStatus = AlgoStatus.WAITING
        self.offset: str = None
        if self.time_interval < 2:
            self.engine.write_log(f'[{self.vt_symbol}] 交易时间间隔为{self.time_interval}, 不得小于2 -- 停止交易')
            self.status = AlgoStatus.STOPPED
            return
        self.timer_count: int = self.time_interval - 2
        self.current_pos: int = 0               # 当前仓位
        self.active_orderids: set[str] = set()
        self.to_run: bool = False

    def on_trade(self, trade: TradeData):
        """成交推送"""
        # 累加成交量
        if trade.direction == Direction.LONG:
            self.current_pos += trade.volume
        elif trade.direction == Direction.SHORT:
            self.current_pos -= trade.volume
            
        # self.engine.write_log(f'[成交推送][{self.vt_symbol}] : \n\
        #                         \t成交时间: {trade.datetime}\n\
        #                         \t成交动作: {trade.direction.value} {trade.offset.value}\n\
        #                         \t成交价格: {trade.price}\n\
        #                         \t成交数量: {trade.volume}\
        #                             ')

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
        volume_left: int = abs(self.total_volume) - abs(self.current_pos)
        # 已完成所有交易, 则过滤
        if not volume_left:
            return

        # 开仓阶段
        if volume_left > 0:
            direction = self.direction
            self.offset = f'{direction.value}开'
        # 平仓阶段
        elif volume_left < 0:
            if self.direction == Direction.LONG:
                direction = Direction.SHORT
                # 防止反向开仓
                if self.current_pos < 0:
                    self.engine.write_log(f'[{self.vt_symbol}] 空平阶段: 策略记录的current_pos 与 实际持仓不符, 需检查')
                    return
            elif self.direction == Direction.SHORT:
                direction = Direction.LONG
                # 防止反向开仓
                if self.current_pos > 0:
                    self.engine.write_log(f'[{self.vt_symbol}] 多平阶段: 策略记录的current_pos 与 实际持仓不符, 需检查')
                    return
            self.offset = f'{direction.value}平'
        volume_left = abs(volume_left)

        # 计算委托价格，1档盘口超加1个pricetick; 计算委托成交量，1档盘口量的百分比
        if direction == Direction.LONG:
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
                order_volume = contract.min_volume
                if order_volume == 0:
                    self.engine.write_log(f'[{self.vt_symbol}] order_volume和contract.min_volume都为0')

        # 发出委托
        vt_orderids = self.engine.send_order(
            self,
            self.vt_symbol,
            direction,
            order_price,
            order_volume
        )

        self.active_orderids.update(vt_orderids)
