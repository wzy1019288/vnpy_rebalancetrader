from multiprocessing.connection import wait
import traceback
from csv import DictReader
from functools import partial

from vnpy.event import EventEngine, Event
from vnpy.trader.object import LogData
from vnpy.trader.engine import MainEngine
from vnpy.trader.constant import Direction
from vnpy.trader.ui import QtWidgets, QtCore, qt
from vnpy.trader.ui.widget import (
    BaseMonitor,
    TradeMonitor,
    OrderMonitor,
    BaseCell,
    EnumCell,
    DirectionCell,
    PnlCell
)

from ..engine import APP_NAME, EVENT_TICK, EVENT_REBALANCE_ALGO, EVENT_REBALANCE_EXPOSURE, EVENT_REBALANCE_HOLDING, EVENT_REBALANCE_LOG
from ..engine import DfRebalanceEngine
from ..algo import DfTwapAlgo

COLOR_LONG = qt.QtGui.QColor("red")
COLOR_SHORT = qt.QtGui.QColor("green")
COLOR_WHITE = qt.QtGui.QColor("white")


class RebalanceWidget(QtWidgets.QWidget):
    """篮子交易监控组件"""

    signal_log = QtCore.pyqtSignal(Event)
    signal_algo = QtCore.pyqtSignal(Event)
    signal_exposure = QtCore.pyqtSignal(Event)
    signal_tick = QtCore.pyqtSignal(Event)

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """"""
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine

        self.engine: DfRebalanceEngine = main_engine.get_engine(APP_NAME)

        # 修改框中数字后, 将待该仓位缓存于该字典中, 确认后进行修改
        self.target_pos_waiting_to_change: dict = {}

        self.show = self.showMaximized

        self.init_ui()
        self.register_event()

    def init_ui(self) -> None:
        """初始化界面"""
        self.setWindowTitle("组合调仓")

        self.long_monitor: AlgoMonitor = AlgoMonitor(self.main_engine, self.event_engine)
        self.short_monitor: AlgoMonitor = AlgoMonitor(self.main_engine, self.event_engine)

        self.order_monitor: RebalanceOrderMonitor = RebalanceOrderMonitor(self.main_engine, self.event_engine)
        self.trade_monitor: RebalanceTradeMonitor = RebalanceTradeMonitor(self.main_engine, self.event_engine)
        self.holding_monitor: RebalanceHoldingMonitor = RebalanceHoldingMonitor(self.main_engine, self.event_engine)

        self.log_monitor: QtWidgets.QTextEdit = QtWidgets.QTextEdit()
        self.log_monitor.setReadOnly(True)

        self.control_monitor: QtWidgets.QGridLayout = QtWidgets.QGridLayout()
        self.control_monitor.setRowStretch(10, 6)

        self.init_button = QtWidgets.QPushButton("初始化")
        self.init_button.clicked.connect(self.init_engine)

        self.csv_button = QtWidgets.QPushButton("载入CSV")
        self.csv_button.clicked.connect(self.load_csv)
        self.csv_button.setEnabled(False)

        self.start_button = QtWidgets.QPushButton("启动算法")
        self.start_button.clicked.connect(self.start_algos)
        self.start_button.setEnabled(False)

        self.stop_button = QtWidgets.QPushButton("停止算法")
        self.stop_button.clicked.connect(self.stop_algos)
        self.stop_button.setEnabled(False)

        self.close_pos_button = QtWidgets.QPushButton("一键平仓")
        self.close_pos_button.clicked.connect(self.close_all_pos)
        self.close_pos_button.setEnabled(False)

        self.clear_button = QtWidgets.QPushButton("清空算法")
        self.clear_button.clicked.connect(self.clear_algos)
        self.clear_button.setEnabled(False)

        self.long_value_label = QtWidgets.QLabel()
        self.short_value_label = QtWidgets.QLabel()
        self.net_value_label = QtWidgets.QLabel()
        self.deviate_label = QtWidgets.QLabel()
        self.long_pause_label = QtWidgets.QLabel()
        self.short_pause_label = QtWidgets.QLabel()

        self.limit_spin = QtWidgets.QSpinBox()
        self.limit_spin.setRange(0, 1_000_000_000)
        self.limit_spin.setSingleStep(10_000)
        self.limit_spin.setValue(self.engine.exposure_limit)
        self.limit_spin.valueChanged.connect(self.update_exposure_limit)

        hbox1 = QtWidgets.QHBoxLayout()
        hbox1.addWidget(self.init_button)
        hbox1.addWidget(self.csv_button)
        hbox1.addWidget(self.start_button)
        hbox1.addWidget(self.stop_button)
        hbox1.addWidget(self.close_pos_button)
        hbox1.addStretch()
        hbox1.addWidget(QtWidgets.QLabel("敞口上限"))
        hbox1.addWidget(self.limit_spin)
        hbox1.addStretch()
        hbox1.addWidget(self.clear_button)

        vbox1 = QtWidgets.QVBoxLayout()
        vbox1.addWidget(self.long_monitor)
        vbox1.addWidget(self.short_monitor)

        vbox2 = QtWidgets.QVBoxLayout()
        vbox2.addWidget(self.order_monitor)
        vbox2.addWidget(self.trade_monitor)
        vbox2.addWidget(self.holding_monitor)

        vbox3 = QtWidgets.QVBoxLayout()
        vbox3.addLayout(self.control_monitor)
        vbox3.addWidget(self.log_monitor)

        hbox2 = QtWidgets.QHBoxLayout()
        hbox2.addLayout(vbox1)
        hbox2.addLayout(vbox2)
        hbox2.addLayout(vbox3)

        hbox3 = QtWidgets.QHBoxLayout()
        hbox3.addWidget(self.long_value_label)
        hbox3.addStretch()
        hbox3.addWidget(self.short_value_label)
        hbox3.addStretch()
        hbox3.addWidget(self.net_value_label)
        hbox3.addStretch()
        hbox3.addWidget(self.deviate_label)
        hbox3.addStretch()
        hbox3.addWidget(self.long_pause_label)
        hbox3.addStretch()
        hbox3.addWidget(self.short_pause_label)

        vbox = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox1)
        vbox.addLayout(hbox2)
        vbox.addLayout(hbox3)

        self.setLayout(vbox)

    def init_control_monitor(self) -> None:
        '''初始化控制组件'''
        self.qlabel = {}        # 各品种的label
        for i, (symbol, algo) in enumerate(self.engine.algos.items()):
            
            self.qlabel[symbol] = QtWidgets.QLabel(symbol)
            self.control_monitor.addWidget(self.qlabel[symbol], i, 0)

            waiting_botton = QtWidgets.QPushButton("暂停")
            reset_waiting_status = partial(self.reset_status, symbol=symbol, status='paused')
            waiting_botton.clicked.connect(reset_waiting_status)
            self.control_monitor.addWidget(waiting_botton, i, 1)

            running_botton = QtWidgets.QPushButton("运行")
            reset_running_status = partial(self.reset_status, symbol=symbol, status='running')
            running_botton.clicked.connect(reset_running_status)
            self.control_monitor.addWidget(running_botton, i, 2)

            self.control_monitor.addWidget(QtWidgets.QLabel('   更改目标仓位:'), i, 3)
            
            # 框内数字进行缓存
            target_pos_cell = QtWidgets.QSpinBox()
            if algo.direction == Direction.LONG:
                target_pos_cell.setRange(0, 10_000)
            elif algo.direction == Direction.SHORT:
                target_pos_cell.setRange(-10_000, 0)
            target_pos_cell.setSingleStep(1)
            target_pos_cell.setValue(algo.total_volume)
            wait_change_target_pos = partial(self.change_target_pos, symbol=symbol, waiting=True)
            target_pos_cell.valueChanged.connect(wait_change_target_pos)
            self.control_monitor.addWidget(target_pos_cell, i, 4)
            
            # 对所调仓位进行确认
            ensure_change_target_pos = partial(self.change_target_pos, pos=None, symbol=symbol, waiting=False)
            ensure_botton = QtWidgets.QPushButton("确认")
            ensure_botton.clicked.connect(ensure_change_target_pos)
            self.control_monitor.addWidget(ensure_botton, i, 5)

    def register_event(self) -> None:
        """注册事件监听"""
        self.signal_tick.connect(self.process_tick_event)
        self.signal_log.connect(self.process_log_event)
        self.signal_algo.connect(self.process_algo_event)
        self.signal_exposure.connect(self.process_exposure_event)

        self.event_engine.register(EVENT_REBALANCE_LOG, self.signal_log.emit)
        self.event_engine.register(EVENT_REBALANCE_ALGO, self.signal_algo.emit)
        self.event_engine.register(EVENT_REBALANCE_EXPOSURE, self.signal_exposure.emit)
        self.event_engine.register(EVENT_TICK, self.signal_tick.emit)

    def process_algo_event(self, event: Event) -> None:
        """处理算法事件"""
        algo: DfTwapAlgo = event.data

        if algo.direction == Direction.LONG:
            self.long_monitor.process_event(event)
        else:
            self.short_monitor.process_event(event)

    def process_log_event(self, event: Event) -> None:
        """处理日志事件"""
        log: LogData = event.data
        self.log_monitor.append(f"{log.time}: {log.msg}")
    
    def process_tick_event(self, event: Event) -> None:
        """处理tick事件"""
        for symbol in self.engine.algos.keys():
            color = self.engine.update_color(symbol)
            if color:
                self.qlabel[symbol].setStyleSheet(f"color:{color}")

    def process_exposure_event(self, event: Event) -> None:
        """处理敞口事件"""
        data: dict = event.data

        long_value: float = data["long_value"]
        short_value: float = data["short_value"]
        net_value: float = data["net_value"]
        deviate: float = data["deviate"]
        long_pause: bool = data["long_pause"]
        short_pause: bool = data["short_pause"]

        self.long_value_label.setText(f"买入成交 {long_value}")
        self.short_value_label.setText(f"卖出成交 {short_value}")
        self.net_value_label.setText(f"当前敞口 {net_value}")
        self.deviate_label.setText(f"偏离度 {deviate}")

        if long_pause:
            self.long_pause_label.setText("买入暂停")
        else:
            self.long_pause_label.setText("买入正常")

        if short_pause:
            self.short_pause_label.setText("卖出暂停")
        else:
            self.short_pause_label.setText("卖出正常")

    def init_engine(self) -> None:
        """初始化引擎"""
        n: bool = self.engine.init()

        if not n:
            self.csv_button.setEnabled(True)
        else:
            self.csv_button.setEnabled(False)
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(True)
            self.clear_button.setEnabled(True)
            self.init_control_monitor()

    def load_csv(self) -> None:
        """加载CSV文件"""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            u"导入委托篮子",
            "",
            "CSV(*.csv)"
        )

        if not path:
            return

        # 遍历CSV文件加载
        try:
            with open(path, "r", encoding="utf8") as f:
                # 创建读取器
                reader = DictReader(f)

                # 逐行遍历
                for row in reader:
                    self.engine.add_algo(
                        str(row["vt_symbol"]),
                        Direction(row["direction"]),
                        int(row["total_volume"]),
                        int(row["time_interval"]),
                        float(row["vol_percent"]),
                    )
        except Exception:
            self.engine.write_log(f"委托篮子数据导入失败：{path}")
            self.engine.write_log(f"报错信息：{traceback.format_exc()}")
            return

        self.engine.write_log(f"委托篮子数据导入成功：{path}")

        self.csv_button.setEnabled(False)
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        self.clear_button.setEnabled(True)

        # 初始化控制组件
        self.init_control_monitor()

    def clear_algos(self) -> None:
        """清空所有算法"""
        n = self.engine.clear_algos()
        if not n:
            return

        for monitor in [self.long_monitor, self.short_monitor]:
            monitor.setRowCount(0)
            monitor.cells.clear()
        
        if self.control_monitor.count():
            for i in range(self.control_monitor.count()):
                self.control_monitor.itemAt(i).widget().deleteLater()

        self.csv_button.setEnabled(True)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.clear_button.setEnabled(False)
        self.close_pos_button.setEnabled(False)

    def start_algos(self) -> None:
        """启动所有算法"""
        self.engine.start_algos()
        self.start_button.setEnabled(False)

    def stop_algos(self) -> None:
        """停止所有算法"""
        self.engine.stop_algos()
        self.engine.close()
        self.close_pos_button.setEnabled(True)

    def update_exposure_limit(self, limit: int) -> None:
        """更新敞口限制"""
        self.engine.exposure_limit = limit

    def close_all_pos(self) -> None:
        '''一键平仓'''
        self.engine.close_all_pos()
        self.close_pos_button.setEnabled(False)
        self.init_control_monitor()
    
    def change_target_pos(self, pos: int, symbol: str, waiting: bool) -> None:
        '''改变目标仓位'''
        if waiting:
            self.target_pos_waiting_to_change[symbol] = pos
        else:
            pos = self.target_pos_waiting_to_change[symbol]
            self.engine.change_target_pos(pos, symbol)
    
    def reset_status(self, symbol: str, status: str):
        '''重置运行状态'''
        self.engine.reset_status(symbol, status)
                    


class AlgoMonitor(BaseMonitor):
    """算法监控组件"""

    event_type = EVENT_REBALANCE_ALGO
    data_key = "vt_symbol"

    headers = {
        "vt_symbol": {"display": "算法标的", "cell": BaseCell, "update": False},
        "direction": {"display": "组合方向", "cell": DirectionCell, "update": False},
        "total_volume": {"display": "目标仓位", "cell": BaseCell, "update": True},
        "current_pos": {"display": "当前仓位", "cell": BaseCell, "update": True},
        "offset": {"display": "交易方向", "cell": BaseCell, "update": True},
        "status": {"display": "状态", "cell": EnumCell, "update": True},
        "timer_count": {"display": "本轮读秒", "cell": BaseCell, "update": True},
        "time_interval": {"display": "每轮间隔", "cell": BaseCell, "update": False},
        "vol_percent": {"display": "盘口百分比", "cell": BaseCell, "update": False},
    }

    def register_event(self) -> None:
        """重载移除"""
        pass

    def save_setting(self) -> None:
        """"""
        pass

    def load_stting(self) -> None:
        """"""
        pass


class RebalanceTradeMonitor(TradeMonitor):

    def save_setting(self) -> None:
        pass


class RebalanceOrderMonitor(OrderMonitor):

    def save_setting(self) -> None:
        pass


class RebalanceHoldingMonitor(BaseMonitor):

    event_type = EVENT_REBALANCE_HOLDING
    data_key = "vt_positionid"
    sorting = True

    headers = {
        "symbol": {"display": "持仓代码", "cell": BaseCell, "update": False},
        "direction": {"display": "方向", "cell": DirectionCell, "update": False},
        "volume": {"display": "数量", "cell": BaseCell, "update": True},
        "pnl": {"display": "盈亏", "cell": PnlCell, "update": True},
        "value": {"display": "市值", "cell": BaseCell, "update": True},
        "exchange": {"display": "交易所", "cell": EnumCell, "update": False},
        "name": {"display": "名称", "cell": BaseCell, "update": False},
        "price": {"display": "均价", "cell": BaseCell, "update": True},
    }

    def save_setting(self) -> None:
        pass
