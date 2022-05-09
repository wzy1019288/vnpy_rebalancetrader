import traceback
from csv import DictReader

from vnpy.event import EventEngine, Event
from vnpy.trader.object import LogData
from vnpy.trader.engine import MainEngine
from vnpy.trader.constant import Direction
from vnpy.trader.ui import QtWidgets, QtCore
from vnpy.trader.ui.widget import (
    BaseMonitor,
    TradeMonitor,
    OrderMonitor,
    BaseCell,
    EnumCell,
    DirectionCell,
    PnlCell
)

from ..engine import APP_NAME, EVENT_REBALANCE_ALGO, EVENT_REBALANCE_EXPOSURE, EVENT_REBALANCE_HOLDING, EVENT_REBALANCE_LOG
from ..engine import RebalanceEngine, DfRebalanceEngine
from ..algo import TwapAlgo, DfTwapAlgo


class RebalanceWidget(QtWidgets.QWidget):
    """篮子交易监控组件"""

    signal_log = QtCore.pyqtSignal(Event)
    signal_algo = QtCore.pyqtSignal(Event)
    signal_exposure = QtCore.pyqtSignal(Event)

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """"""
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine

        self.engine: DfRebalanceEngine = main_engine.get_engine(APP_NAME)

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
        self.log_monitor.setMaximumWidth(500)

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

        hbox2 = QtWidgets.QHBoxLayout()
        hbox2.addLayout(vbox1)
        hbox2.addLayout(vbox2)
        hbox2.addWidget(self.log_monitor)

        hbox3 = QtWidgets.QHBoxLayout()
        hbox3.addWidget(self.long_value_label)
        hbox3.addStretch()
        hbox3.addWidget(self.short_value_label)
        hbox3.addStretch()
        hbox3.addWidget(self.net_value_label)
        hbox3.addStretch()
        hbox3.addWidget(self.long_pause_label)
        hbox3.addStretch()
        hbox3.addWidget(self.short_pause_label)

        vbox = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox1)
        vbox.addLayout(hbox2)
        vbox.addLayout(hbox3)

        self.setLayout(vbox)

    def register_event(self) -> None:
        """注册事件监听"""
        self.signal_log.connect(self.process_log_event)
        self.signal_algo.connect(self.process_algo_event)
        self.signal_exposure.connect(self.process_exposure_event)

        self.event_engine.register(EVENT_REBALANCE_LOG, self.signal_log.emit)
        self.event_engine.register(EVENT_REBALANCE_ALGO, self.signal_algo.emit)
        self.event_engine.register(EVENT_REBALANCE_EXPOSURE, self.signal_exposure.emit)

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

    def process_exposure_event(self, event: Event) -> None:
        """处理敞口事件"""
        data: dict = event.data

        long_value: float = data["long_value"]
        short_value: float = data["short_value"]
        net_value: float = data["net_value"]
        long_pause: bool = data["long_pause"]
        short_pause: bool = data["short_pause"]

        self.long_value_label.setText(f"买入成交 {long_value}")
        self.short_value_label.setText(f"卖出成交 {short_value}")
        self.net_value_label.setText(f"当前敞口 {net_value}")

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
            self.close_pos_button.setEnabled(True)

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
                        # float(row["limit_price"]),
                        int(row["total_volume"]),
                        int(row["time_interval"]),
                        # int(row["total_time"]),
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
        self.close_pos_button.setEnabled(True)

    def clear_algos(self) -> None:
        """清空所有算法"""
        n = self.engine.clear_algos()
        if not n:
            return

        for monitor in [self.long_monitor, self.short_monitor]:
            monitor.setRowCount(0)
            monitor.cells.clear()

        self.csv_button.setEnabled(True)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.clear_button.setEnabled(False)
        self.close_pos_button.setEnabled(False)

    def start_algos(self) -> None:
        """启动所有算法"""
        self.engine.start_algos()

    def stop_algos(self) -> None:
        """停止所有算法"""
        self.engine.stop_algos()
        self.engine.close()

    def update_exposure_limit(self, limit: int) -> None:
        """更新敞口限制"""
        self.engine.exposure_limit = limit

    def close_all_pos(self) -> None:
        '''一键平仓'''
        self.engine.close_all_pos()
        self.close_pos_button.setEnabled(False)


class AlgoMonitor(BaseMonitor):
    """算法监控组件"""

    event_type = EVENT_REBALANCE_ALGO
    data_key = "vt_symbol"

    headers = {
        "vt_symbol": {"display": "算法标的", "cell": BaseCell, "update": False},
        "direction": {"display": "方向", "cell": DirectionCell, "update": False},
        # "limit_price": {"display": "价格", "cell": BaseCell, "update": False},
        "total_volume": {"display": "总数量", "cell": BaseCell, "update": False},
        "time_interval": {"display": "每轮间隔", "cell": BaseCell, "update": False},
        # "total_time": {"display": "总时间", "cell": BaseCell, "update": False},
        "vol_percent": {"display": "盘口百分比", "cell": BaseCell, "update": False},
        "status": {"display": "状态", "cell": EnumCell, "update": True},
        "timer_count": {"display": "本轮读秒", "cell": BaseCell, "update": True},
        # "total_count": {"display": "总读秒", "cell": BaseCell, "update": True},
        "traded_volume": {"display": "已成交", "cell": BaseCell, "update": True},
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
        "exchange": {"display": "交易所", "cell": EnumCell, "update": False},
        "name": {"display": "名称", "cell": BaseCell, "update": False},
        "direction": {"display": "方向", "cell": DirectionCell, "update": False},
        "volume": {"display": "数量", "cell": BaseCell, "update": True},
        "price": {"display": "均价", "cell": BaseCell, "update": True},
        "pnl": {"display": "盈亏", "cell": PnlCell, "update": True},
        "value": {"display": "市值", "cell": BaseCell, "update": True},
    }

    def save_setting(self) -> None:
        pass
