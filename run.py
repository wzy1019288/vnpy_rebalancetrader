# flake8: noqa
from vnpy.event import EventEngine
from vnpy.trader.setting import SETTINGS
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp

# from vnpy_ctp import CtpGateway
# from vnpy_ctptest import CtptestGateway
from vnpy_rebalancetrader import RebalanceTraderApp
from vnpy_rohon import RohonGateway

from logging import DEBUG

SETTINGS["log.active"] = True
SETTINGS["log.level"] = DEBUG
SETTINGS["log.console"] = True
SETTINGS["log.file"] = True


def main():
    """"""
    qapp = create_qapp()

    event_engine = EventEngine()

    main_engine = MainEngine(event_engine)
    # main_engine.add_gateway(CtpGateway)
    # main_engine.add_gateway(CtptestGateway)
    main_engine.add_gateway(RohonGateway)
    main_engine.add_app(RebalanceTraderApp)
    
    main_window = MainWindow(main_engine, event_engine)
    main_window.showMaximized()

    qapp.exec()


if __name__ == "__main__":
    main()
