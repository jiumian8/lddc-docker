# SPDX-License-Identifier: GPL-3.0-only
"""日志记录器，兼容桌面 Qt 和无头 Docker 环境。"""
import io
import logging
import os
import sys
import time
from logging import CRITICAL, DEBUG, ERROR, INFO, NOTSET, WARNING, Filter, LogRecord

try:
    from PySide6.QtCore import QLoggingCategory, QMessageLogContext, QtMsgType, qInstallMessageHandler
    HAS_QT = True
except ImportError:
    QLoggingCategory = QMessageLogContext = QtMsgType = None
    HAS_QT = False

try:
    from .args import args
except Exception:
    args = None
try:
    from .data.config import cfg
except ImportError:
    cfg = {"log_level": os.getenv("LOG_LEVEL", "INFO")}
from .paths import log_dir

log_file = log_dir / f"{time.strftime('%Y.%m.%d', time.localtime())}.log"
log_file.parent.mkdir(parents=True, exist_ok=True)


def str2log_level(level: str) -> int:
    levels = {"NOTSET": NOTSET, "DEBUG": DEBUG, "INFO": INFO, "WARNING": WARNING, "ERROR": ERROR, "CRITICAL": CRITICAL}
    if level not in levels:
        raise ValueError(f"Invalid log level: {level}")
    return levels[level]


class QtMessageFilter(Filter):
    def filter(self, record: LogRecord) -> bool:
        if HAS_QT and (qt := record.__dict__.get("qt")) and isinstance(qt, QMessageLogContext):
            record.filename = getattr(qt, "file", "?")
            record.module = getattr(qt, "category", "?")
            record.lineno = getattr(qt, "line", 0)
            record.funcName = getattr(qt, "function", "?")
        return True


class Logger:
    def __init__(self) -> None:
        self.name = "LDDC"
        self.__logger = logging.getLogger(self.name)
        self.__logger.addFilter(QtMessageFilter())
        self.level = str2log_level(os.getenv("LOG_LEVEL", cfg.get("log_level", "INFO")))
        formatter = logging.Formatter("[%(levelname)s]%(asctime)s- %(module)s(%(lineno)d) - %(funcName)s:%(message)s")
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        self.__logger.addHandler(file_handler)
        debug = bool(args and getattr(args, "get_service_port", False) is False)
        if os.getenv("DEBUG", "false").lower() == "true" or debug and HAS_QT:
            console_handler = logging.StreamHandler(sys.stdout)
            if isinstance(sys.stdout, io.TextIOWrapper):
                sys.stdout.reconfigure(encoding="utf-8")
            console_handler.setFormatter(formatter)
            self.__logger.addHandler(console_handler)
        self.set_level(self.level)
        self.debug, self.info, self.warning = self.__logger.debug, self.__logger.info, self.__logger.warning
        self.error, self.critical = self.__logger.error, self.__logger.critical
        self.log, self.exception = self.__logger.log, self.__logger.exception

    def set_level(self, level: int | str) -> None:
        if isinstance(level, str):
            level = str2log_level(level)
        self.level = level
        self.__logger.setLevel(level)
        for handler in self.__logger.handlers:
            handler.setLevel(level)
        if HAS_QT:
            QLoggingCategory.setFilterRules("*.debug=false\n*.info=true\n*.warning=true\n*.critical=true")


logger = Logger()

if HAS_QT:
    def qt_message_handler(mode: QtMsgType, context: QMessageLogContext, message: str) -> None:
        if mode == QtMsgType.QtDebugMsg:
            logger.debug(message, extra={"qt": context})
        elif mode == QtMsgType.QtInfoMsg:
            logger.info(message, extra={"qt": context})
        elif mode == QtMsgType.QtWarningMsg:
            logger.warning(message, extra={"qt": context})
        elif mode == QtMsgType.QtCriticalMsg:
            logger.error(message, extra={"qt": context})
        elif mode == QtMsgType.QtFatalMsg:
            logger.critical(message, extra={"qt": context})
    qInstallMessageHandler(qt_message_handler)
