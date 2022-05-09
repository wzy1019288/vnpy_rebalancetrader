from dataclasses import dataclass

from vnpy.event import EventEngine, Event
from vnpy.trader.engine import BaseEngine, MainEngine
from vnpy.trader.event import (
    EVENT_TIMER,
    EVENT_ORDER,
    EVENT_TRADE,
    EVENT_POSITION
)
from vnpy.trader.object import (
    ContractData,
    TickData,
    OrderData,
    TradeData,
    PositionData,
    LogData,
    OrderRequest,
    CancelRequest,
    SubscribeRequest
)
from vnpy.trader.constant import Direction, Offset, OrderType, Exchange
from vnpy.trader.converter import OffsetConverter
from vnpy.trader.utility import load_json, save_json

from .algo import AlgoStatus, TwapAlgo, DfTwapAlgo


APP_NAME: str = "RebalanceTrader"

EVENT_REBALANCE_LOG = "eRebalanceLog"
EVENT_REBALANCE_ALGO = "eRebalanceAlgo"
EVENT_REBALANCE_EXPOSURE = "eRebalanceExposure"
EVENT_REBALANCE_HOLDING = "eRebalanceHolding"


@dataclass
class HoldingData:
    """组合持仓数据"""
    vt_positionid: str
    symbol: str
    exchange: Exchange
    name: str
    direction: Direction
    volume: int
    price: float
    pnl: float
    value: float


class RebalanceEngine(BaseEngine):
    """篮子执行引擎"""

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """构造函数"""
        super().__init__(main_engine, event_engine, APP_NAME)

        # 对象字典
        self.algos: dict[str, TwapAlgo] = {}    # vt_symbol: TwapAlgo
        self.orders: dict[str, OrderData] = {}
        self.trades: dict[str, TradeData] = {}

        # 开平转换
        self.offset_converter: OffsetConverter = OffsetConverter(self.main_engine)

        # 统计数据
        self.long_value: int = 0
        self.short_value: int = 0
        self.net_value: int = 0

        # 敞口限制
        self.exposure_limit: int = 200_000
        self.long_pause: bool = False
        self.short_pause: bool = False

        # 查询函数
        self.get_contract = main_engine.get_contract
        self.get_tick = main_engine.get_tick

        # 算法状态
        self.algo_started = False

        self.register_event()

    def register_event(self) -> None:
        """注册事件监听"""
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)
        self.event_engine.register(EVENT_ORDER, self.process_order_event)
        self.event_engine.register(EVENT_TRADE, self.process_trade_event)
        self.event_engine.register(EVENT_POSITION, self.process_position_event)

    def process_timer_event(self, event: Event) -> None:
        """处理定时事件"""
        # 检查敞口
        self.check_exposure()

        # 生成列表避免字典改变
        algos: list = list(self.algos.values())

        for algo in algos:
            if algo.status == AlgoStatus.RUNNING:
                algo.on_timer()

                self.put_algo_event(algo)

    def process_position_event(self, event: Event):
        """处理持仓事件"""
        position: PositionData = event.data

        self.offset_converter.update_position(position)

    def process_trade_event(self, event: Event) -> None:
        """处理成交事件"""
        trade: TradeData = event.data

        # 过滤重复推送
        if trade.vt_tradeid in self.trades:
            return
        self.trades[trade.vt_tradeid] = trade

        # 更新到开平转换器
        self.offset_converter.update_trade(trade)

        # 计算成交市值
        self.update_value(trade)

        # 推送给算法
        algo: TwapAlgo = self.algos.get(trade.vt_symbol, None)
        if algo:
            algo.on_trade(trade)

            # 检查是否结束
            # df改成不结束
            if algo.traded_volume >= algo.total_volume:
                algo.status = AlgoStatus.FINISHED
                self.write_log(f"算法执行结束{trade.vt_symbol}")

            self.put_algo_event(algo)

    def process_order_event(self, event: Event) -> None:
        """处理委托事件"""
        order: OrderData = event.data

        # 过滤已经结束的委托推送
        existing_order = self.orders.get(order.vt_orderid, None)
        if existing_order and not existing_order.is_active():
            return
        self.orders[order.vt_orderid] = order

        self.offset_converter.update_order(order)

        algo: TwapAlgo = self.algos.get(order.vt_symbol, None)
        if algo:
            algo.on_order(order)

    def add_algo(
        self,
        vt_symbol: str,
        direction: Direction,
        limit_price: float,
        total_volume: int,
        time_interval: int,
        total_time: int
    ) -> bool:
        """添加算法"""
        # 检查合约信息
        contract: ContractData = self.get_contract(vt_symbol)
        if not contract:
            self.write_log(f"添加算法失败，找不到合约：{vt_symbol}")
            return

        # 订阅行情推送
        req = SubscribeRequest(
            symbol=contract.symbol,
            exchange=contract.exchange
        )
        self.main_engine.subscribe(req, contract.gateway_name)

        # 创建算法实例
        algo: TwapAlgo = TwapAlgo(
            self,
            vt_symbol,
            direction,
            limit_price,
            total_volume,
            time_interval,
            total_time
        )
        self.algos[vt_symbol] = algo
        self.put_algo_event(algo)

        self.write_log(f"添加算法成功{vt_symbol}")

    def start_algo(self, vt_symbol: str) -> bool:
        """启动算法"""
        algo: TwapAlgo = self.algos[vt_symbol]

        # 只允许启动【等待】状态的算法
        if algo.status != AlgoStatus.WAITING:
            return False

        algo.status = AlgoStatus.RUNNING
        self.put_algo_event(algo)

        self.write_log(f"启动算法执行{vt_symbol}")
        return True

    def pause_algo(self, vt_symbol: str) -> bool:
        """暂停算法"""
        algo: TwapAlgo = self.algos[vt_symbol]

        # 只允许暂停【运行】状态的算法
        if algo.status != AlgoStatus.RUNNING:
            return False

        algo.status = AlgoStatus.PAUSED
        self.put_algo_event(algo)

        self.write_log(f"暂停算法执行{vt_symbol}")
        return True

    def resume_algo(self, vt_symbol: str) -> bool:
        """恢复算法"""
        algo: TwapAlgo = self.algos[vt_symbol]

        # 只允许恢复【暂停】状态的算法
        if algo.status != AlgoStatus.PAUSED:
            return False

        algo.status = AlgoStatus.RUNNING
        self.put_algo_event(algo)

        self.write_log(f"恢复算法执行{vt_symbol}")
        return True

    def stop_algo(self, vt_symbol: str) -> bool:
        """暂停算法"""
        algo: TwapAlgo = self.algos[vt_symbol]

        # 只允许暂停【运行】、【暂停】状态的算法
        if algo.status not in {AlgoStatus.RUNNING, AlgoStatus.PAUSED}:
            return False

        algo.status = AlgoStatus.STOPPED
        self.put_algo_event(algo)

        self.write_log(f"停止算法执行{vt_symbol}")
        return True

    def start_algos(self) -> None:
        """批量启动算法"""
        for vt_symbol in self.algos.keys():
            self.start_algo(vt_symbol)

        self.algo_started = True

    def pause_algos(self, direction: Direction) -> None:
        """批量暂停算法"""
        for vt_symbol, algo in self.algos.items():
            if algo.direction == direction:
                self.pause_algo(vt_symbol)

    def resume_algos(self) -> None:
        """批量恢复算法"""
        for vt_symbol in self.algos.keys():
            self.resume_algo(vt_symbol)

    def stop_algos(self) -> None:
        """批量启动算法"""
        for vt_symbol in self.algos.keys():
            self.stop_algo(vt_symbol)

    def clear_algos(self) -> bool:
        """清空所有算法"""
        # 检查没有算法在运行
        for algo in self.algos.values():
            if algo.status in {AlgoStatus.RUNNING, AlgoStatus.PAUSED}:
                return False

        # 清空算法对象
        self.algos.clear()

        # 清空暂停状态
        self.long_pause = False
        self.short_pause = False

        self.put_exposure_event()

        # 清空启动状态
        self.algo_started = False

        self.write_log("清空所有算法")

        return True

    def send_order(
        self,
        algo: TwapAlgo,
        vt_symbol: str,
        direction: Direction,
        price: float,
        volume: float,
    ) -> str:
        """委托下单"""
        # 创建原始委托
        contract: ContractData = self.main_engine.get_contract(vt_symbol)

        original_req: OrderRequest = OrderRequest(
            symbol=contract.symbol,
            exchange=contract.exchange,
            direction=direction,
            type=OrderType.LIMIT,
            volume=volume,
            price=price,
            reference=f"{APP_NAME}_{vt_symbol}"
        )

        # 进行净仓位转换
        reqs: list[OrderRequest] = self.offset_converter.convert_order_request(
            original_req,
            lock=False,
            net=True
        )

        vt_orderids: list[str] = []
        for req in reqs:
            vt_orderid: str = self.main_engine.send_order(req, contract.gateway_name)

            if not vt_orderid:
                continue

            vt_orderids.append(vt_orderid)
            self.offset_converter.update_order_request(req, vt_orderid)

        return vt_orderids

    def cancel_order(self, algo: TwapAlgo, vt_orderid: str) -> None:
        """委托撤单"""
        order: OrderData = self.main_engine.get_order(vt_orderid)

        if not order:
            self.write_log(f"委托撤单失败，找不到委托：{vt_orderid}", algo)
            return

        req: CancelRequest = order.create_cancel_request()
        self.main_engine.cancel_order(req, order.gateway_name)

    def write_log(self, msg: str) -> None:
        """输出日志"""
        log: LogData = LogData(msg=msg, gateway_name=APP_NAME)
        event: Event = Event(EVENT_REBALANCE_LOG, data=log)
        self.event_engine.put(event)

    def put_algo_event(self, algo: TwapAlgo) -> None:
        """推送事件"""
        event: Event = Event(EVENT_REBALANCE_ALGO, data=algo)
        self.event_engine.put(event)

    def update_value(self, trade: TradeData) -> None:
        """更新成交市值"""
        contract: ContractData = self.get_contract(trade.vt_symbol)
        trade_value: float = trade.price * trade.volume * contract.size

        if trade.direction == Direction.LONG:
            self.long_value += trade_value
        else:
            self.short_value += trade_value
        self.net_value = self.long_value - self.short_value

    def check_exposure(self) -> None:
        """检查敞口"""
        if self.algo_started:
            # 多头太快
            if self.net_value > self.exposure_limit:
                if not self.long_pause:
                    self.pause_algos(Direction.LONG)
                    self.long_pause = True
            # 空头太块
            elif self.net_value < -self.exposure_limit:
                if not self.short_pause:
                    self.pause_algos(Direction.SHORT)
                    self.short_pause = True
            # 正常范围
            else:
                if self.long_pause or self.short_pause:
                    self.resume_algos()

        self.put_exposure_event()

    def put_exposure_event(self) -> None:
        """推送敞口事件"""
        event: Event = Event(
            type=EVENT_REBALANCE_EXPOSURE,
            data={
                "long_value": self.long_value,
                "short_value": self.short_value,
                "net_value": self.net_value,
                "long_pause": self.long_pause,
                "short_pause": self.short_pause,
            }
        )
        self.event_engine.put(event)



class DfRebalanceEngine(BaseEngine):
    """篮子执行引擎"""

    data_filename = "rebalance_trader_data.json"

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """构造函数"""
        super().__init__(main_engine, event_engine, APP_NAME)

        self.df_algo = DfTwapAlgo

        # 对象字典
        self.algos: dict[str, self.df_algo] = {}    # vt_symbol: self.df_algo
        self.orders: dict[str, OrderData] = {}
        self.trades: dict[str, TradeData] = {}

        # 开平转换
        self.offset_converter: OffsetConverter = OffsetConverter(self.main_engine)

        # 统计数据
        self.long_value: int = 0
        self.short_value: int = 0
        self.net_value: int = 0

        # 敞口限制
        self.exposure_limit: int = 2_000_000
        self.long_pause: bool = False
        self.short_pause: bool = False

        # 查询函数
        self.get_contract = main_engine.get_contract
        self.get_tick = main_engine.get_tick

        # 算法状态
        self.algo_started = False

    def init(self) -> bool:
        """初始化引擎"""
        self.register_event()

        n: bool = self.load_data()

        self.write_log("引擎初始化完成")

        return n

    def close(self) -> None:
        """关闭引擎"""
        self.save_data()

    def register_event(self) -> None:
        """注册事件监听"""
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)
        self.event_engine.register(EVENT_ORDER, self.process_order_event)
        self.event_engine.register(EVENT_TRADE, self.process_trade_event)
        self.event_engine.register(EVENT_POSITION, self.process_position_event)

    def process_timer_event(self, event: Event) -> None:
        """处理定时事件"""
        # 检查敞口
        self.check_exposure()

        # 生成列表避免字典改变
        algos: list = list(self.algos.values())

        for algo in algos:
            if algo.status == AlgoStatus.RUNNING:
                algo.on_timer()

                self.put_algo_event(algo)

    def process_position_event(self, event: Event):
        """处理持仓事件"""
        position: PositionData = event.data

        self.offset_converter.update_position(position)

        # 推送组合持仓更新
        tick: TickData = self.get_tick(position.vt_symbol)
        if not tick:
            self.subscribe(position.vt_symbol)
            return
        
        contract: ContractData = self.get_contract(position.vt_symbol)
        if not contract:
            return
        
        value: float = tick.last_price * position.volume * contract.size

        holding: HoldingData = HoldingData(
            vt_positionid=position.vt_positionid,
            symbol=position.symbol,
            exchange=position.exchange,
            name=contract.name,
            direction=position.direction,
            volume=position.volume,
            price=position.price,
            pnl=round(position.pnl, 0),
            value=round(value, 0)
        )

        # 计算实时市值
        self.update_value(position, tick)

        event: Event = Event(EVENT_REBALANCE_HOLDING, holding)
        self.event_engine.put(event)

    def process_trade_event(self, event: Event) -> None:
        """处理成交事件"""
        trade: TradeData = event.data

        # 过滤重复推送
        if trade.vt_tradeid in self.trades:
            return
        self.trades[trade.vt_tradeid] = trade

        # 更新到开平转换器
        self.offset_converter.update_trade(trade)

        # 计算成交市值
        # self.update_value(trade)

        # 推送给算法
        algo: self.df_algo = self.algos.get(trade.vt_symbol, None)
        if algo:
            algo.on_trade(trade)

            # 检查是否结束
            # df改成不结束
            if algo.traded_volume == algo.total_volume:
                # algo.status = AlgoStatus.FINISHED
                # self.write_log(f"算法执行结束{trade.vt_symbol}")
                self.write_log(f"{trade.vt_symbol}交易结束! [{algo.traded_volume}/{algo.total_volume}]")

            self.put_algo_event(algo)

    def process_order_event(self, event: Event) -> None:
        """处理委托事件"""
        order: OrderData = event.data

        # 过滤已经结束的委托推送
        existing_order = self.orders.get(order.vt_orderid, None)
        if existing_order and not existing_order.is_active():
            return
        self.orders[order.vt_orderid] = order

        self.offset_converter.update_order(order)

        algo: self.df_algo = self.algos.get(order.vt_symbol, None)
        if algo:
            algo.on_order(order)

    def subscribe(self, vt_symbol: str) -> None:
        """订阅行情"""
        contract: ContractData = self.get_contract(vt_symbol)

        req = SubscribeRequest(
            symbol=contract.symbol,
            exchange=contract.exchange
        )
        self.main_engine.subscribe(req, contract.gateway_name)

    def add_algo(
        self,
        vt_symbol: str,
        direction: Direction,
        # limit_price: float,
        total_volume: int,
        time_interval: int,
        # total_time: int,
        vol_percent: float
    ) -> bool:
        """添加算法"""
        # 检查合约信息
        contract: ContractData = self.get_contract(vt_symbol)
        if not contract:
            self.write_log(f"添加算法失败，找不到合约：{vt_symbol}")
            return

        # 订阅行情推送
        self.subscribe(vt_symbol)

        # 创建算法实例
        algo: self.df_algo = self.df_algo(
            self,
            vt_symbol,
            direction,
            # limit_price,
            total_volume,
            time_interval,
            # total_time,
            vol_percent
        )
        self.algos[vt_symbol] = algo
        self.put_algo_event(algo)

        self.write_log(f"添加算法成功{vt_symbol}")

    def start_algo(self, vt_symbol: str) -> bool:
        """启动算法"""
        algo: self.df_algo = self.algos[vt_symbol]

        # 只允许启动【等待】状态的算法
        if algo.status != AlgoStatus.WAITING:
            return False

        algo.status = AlgoStatus.RUNNING
        self.put_algo_event(algo)

        self.write_log(f"启动算法执行{vt_symbol}")
        return True

    def pause_algo(self, vt_symbol: str) -> bool:
        """暂停算法"""
        algo: self.df_algo = self.algos[vt_symbol]

        # 只允许暂停【运行】状态的算法
        if algo.status != AlgoStatus.RUNNING:
            return False

        algo.status = AlgoStatus.PAUSED
        self.put_algo_event(algo)

        self.write_log(f"暂停算法执行{vt_symbol}")
        return True

    def resume_algo(self, vt_symbol: str) -> bool:
        """恢复算法"""
        algo: self.df_algo = self.algos[vt_symbol]

        # 只允许恢复【暂停】状态的算法
        if algo.status != AlgoStatus.PAUSED:
            return False

        algo.status = AlgoStatus.RUNNING
        self.put_algo_event(algo)

        self.write_log(f"恢复算法执行{vt_symbol}")
        return True

    def stop_algo(self, vt_symbol: str) -> bool:
        """停止算法"""
        algo: self.df_algo = self.algos[vt_symbol]

        # 只允许停止【运行】、【暂停】状态的算法
        if algo.status not in {AlgoStatus.RUNNING, AlgoStatus.PAUSED}:
            return False

        algo.status = AlgoStatus.STOPPED
        self.put_algo_event(algo)

        self.write_log(f"停止算法执行{vt_symbol}")
        return True

    def start_algos(self) -> None:
        """批量启动算法"""
        for vt_symbol in self.algos.keys():
            self.start_algo(vt_symbol)

        self.algo_started = True

    def pause_algos(self, direction: Direction) -> None:
        """批量暂停算法"""
        for vt_symbol, algo in self.algos.items():
            if algo.direction == direction:
                self.pause_algo(vt_symbol)

    def resume_algos(self) -> None:
        """批量恢复算法"""
        for vt_symbol in self.algos.keys():
            self.resume_algo(vt_symbol)

    def stop_algos(self) -> None:
        """批量停止算法"""
        for vt_symbol in self.algos.keys():
            self.stop_algo(vt_symbol)

    def clear_algos(self) -> bool:
        """清空所有算法"""
        # 检查没有算法在运行
        for algo in self.algos.values():
            if algo.status in {AlgoStatus.RUNNING, AlgoStatus.PAUSED}:
                return False

        # 清空算法对象
        self.algos.clear()

        # 清空暂停状态
        self.long_pause = False
        self.short_pause = False

        self.put_exposure_event()

        # 清空启动状态
        self.algo_started = False

        self.write_log("清空所有算法")

        return True

    def send_order(
        self,
        algo: TwapAlgo,
        vt_symbol: str,
        direction: Direction,
        price: float,
        volume: float,
    ) -> str:
        """委托下单"""
        # 创建原始委托
        contract: ContractData = self.main_engine.get_contract(vt_symbol)

        original_req: OrderRequest = OrderRequest(
            symbol=contract.symbol,
            exchange=contract.exchange,
            direction=direction,
            type=OrderType.LIMIT,
            volume=volume,
            price=price,
            reference=f"{APP_NAME}_{vt_symbol}"
        )

        # 进行净仓位转换
        reqs: list[OrderRequest] = self.offset_converter.convert_order_request(
            original_req,
            lock=False,
            net=True
        )

        vt_orderids: list[str] = []
        for req in reqs:
            vt_orderid: str = self.main_engine.send_order(req, contract.gateway_name)

            if not vt_orderid:
                continue

            vt_orderids.append(vt_orderid)
            self.offset_converter.update_order_request(req, vt_orderid)

        return vt_orderids

    def cancel_order(self, 
                algo: TwapAlgo, 
                vt_orderid: str) -> None:
        """委托撤单"""
        order: OrderData = self.main_engine.get_order(vt_orderid)

        if not order:
            self.write_log(f"委托撤单失败，找不到委托：{vt_orderid}", algo)
            return

        req: CancelRequest = order.create_cancel_request()
        self.main_engine.cancel_order(req, order.gateway_name)

    def write_log(self, msg: str) -> None:
        """输出日志"""
        log: LogData = LogData(msg=msg, gateway_name=APP_NAME)
        event: Event = Event(EVENT_REBALANCE_LOG, data=log)
        self.event_engine.put(event)

    def put_algo_event(self, 
                algo: TwapAlgo
                ) -> None:
        """推送事件"""
        event: Event = Event(EVENT_REBALANCE_ALGO, data=algo)
        self.event_engine.put(event)

    def update_value(self, position: PositionData, tick: TickData) -> None:
        """更新成交市值"""
        contract: ContractData = self.get_contract(trade.vt_symbol)
        trade_value: float = trade.price * trade.volume * contract.size

        if trade.direction == Direction.LONG:
            if trade.offset == Offset.OPEN:
                self.long_value += trade_value
            else:
                self.short_value -= trade_value
        elif trade.direction == Direction.SHORT:
            if trade.offset == Offset.OPEN:
                self.short_value += trade_value
            else:
                self.long_value -= trade_value
        self.net_value = self.long_value - self.short_value

    def check_exposure(self) -> None:
        """检查敞口"""
        if self.algo_started:
            # 多头太快
            if self.net_value > self.exposure_limit:
                if not self.long_pause:
                    self.pause_algos(Direction.LONG)
                    self.long_pause = True
            # 空头太块
            elif self.net_value < -self.exposure_limit:
                if not self.short_pause:
                    self.pause_algos(Direction.SHORT)
                    self.short_pause = True
            # 正常范围
            else:
                if self.long_pause or self.short_pause:
                    self.long_pause = False
                    self.short_pause = False
                    self.resume_algos()

        self.put_exposure_event()

    def put_exposure_event(self) -> None:
        """推送敞口事件"""
        event: Event = Event(
            type=EVENT_REBALANCE_EXPOSURE,
            data={
                "long_value": self.long_value,
                "short_value": self.short_value,
                "net_value": self.net_value,
                "long_pause": self.long_pause,
                "short_pause": self.short_pause,
            }
        )
        self.event_engine.put(event)
    
    def save_data(self, data_filename=None) -> None:
        """保存数据"""
        data: list[dict] = []

        for algo in self.algos.values():
            d: dict = {
                # 参数
                "vt_symbol": algo.vt_symbol,
                "direction": algo.direction.value,
                # "limit_price": algo.limit_price,
                "total_volume": algo.total_volume,
                "time_interval": algo.time_interval,
                # "total_time": algo.total_time,
                "vol_percent": algo.vol_percent,
                # 变量
                # "total_count": algo.total_count,
                "traded_volume": algo.traded_volume
            }
            if d['traded_volume'] != 0 or d['total_volume'] != 0:
                data.append(d)

        if data_filename is not None:
            save_json(data_filename, data)
        else:
            save_json(self.data_filename, data)

    def load_data(self) -> bool:
        """载入数据"""
        data: list[dict] = load_json(self.data_filename)

        for d in data:
            # 添加算法实例
            self.add_algo(
                d["vt_symbol"],
                Direction(d["direction"]),
                # d["limit_price"],
                d["total_volume"],
                d["time_interval"],
                # d["total_time"],
                d["vol_percent"],
            )

            # 恢复算法变量
            algo: TwapAlgo = self.algos[d["vt_symbol"]]
            # algo.total_count = d["total_count"]
            algo.traded_volume = d["traded_volume"]

            # 判断算法状态
            # if algo.traded_volume == algo.total_volume:
            #     algo.status == AlgoStatus.FINISHED
            # else:
            algo.status == AlgoStatus.WAITING

            self.put_algo_event(algo)

        return bool(data)

    def close_all_pos(self) -> None:
        '''平所有仓'''
        for symbol, algo in self.algos.items():
            algo.total_volume = 0
            if algo.direction == Direction.LONG:
                algo.direction = Direction.SHORT
            elif algo.direction == Direction.SHORT:
                algo.direction = Direction.LONG
            
            self.put_algo_event(algo)
        

    def change_target_pos(self) -> None:
        '''改变目标仓位'''
        pass