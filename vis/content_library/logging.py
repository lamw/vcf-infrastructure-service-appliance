from logging import Logger, DEBUG, Formatter, basicConfig, NullHandler
import gzip
import json
import os
import shutil
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import override

_DEFAULT_SYNC_LOG_NAME = "content-library-sync"
_BACKUP_LOG_COUNT = 10

def initialize_clean_logging():
    basicConfig(handlers=[NullHandler()], force=True)

def new_sync_logger(name: str = _DEFAULT_SYNC_LOG_NAME, log_path: Path = Path("/opt/vis/state")) -> Logger:
    current_log = log_path / f"{name}.log"

    def namer(name: str) -> str:
        return f"{name}.gz"

    def rotator(source, dest) -> None:
        with open(source, "rb") as f_in, gzip.open(dest, "rb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        os.remove(source)

    handler = RotatingFileHandler(filename=current_log, mode="w", maxBytes=1, backupCount=10, delay=True)
    handler.namer = namer
    handler.rotator = rotator
    handler.setFormatter(
        JsonFormatter(
            {
                "level": "levelname",
                "message": "message",
                "loggerName": "name",
                "processName": "processName",
                "timestamp": "asctime",
                "current_task": "taskName",
                "module": "module",
            }
        )
    )

    log = Logger(name=name, level=DEBUG)
    log.addHandler(handler)

    return log


# Source - https://stackoverflow.com/a/70223539
# Posted by Bogdan Mircea, modified by community. See post 'Timeline' for change history
# Retrieved 2026-08-10, License - CC BY-SA 4.0
class JsonFormatter(Formatter):
    """
    Formatter that outputs JSON strings after parsing the LogRecord.

    @param dict fmt_dict: Key: logging format attribute pairs. Defaults to {"message": "message"}.
    @param str time_format: time.strftime() format string. Default: "%Y-%m-%dT%H:%M:%S"
    @param str msec_format: Microsecond formatting. Appended at the end. Default: "%s.%03dZ"
    """

    def __init__(self, fmt_dict: dict = None, time_format: str = "%Y-%m-%dT%H:%M:%S", msec_format: str = "%s.%03dZ"):
        self.fmt_dict = fmt_dict if fmt_dict is not None else {"message": "message"}
        self.default_time_format = time_format
        self.default_msec_format = msec_format
        self.datefmt = None

    @override
    def usesTime(self) -> bool:
        """
        Overwritten to look for the attribute in the format dict values instead of the fmt string.
        """
        return "asctime" in self.fmt_dict.values()

    def __as_dict(self, record) -> dict:
        """
        Overwritten to return a dictionary of the relevant LogRecord attributes instead of a string.
        KeyError is raised if an unknown attribute is provided in the fmt_dict.
        """
        return {fmt_key: record.__dict__[fmt_val] for fmt_key, fmt_val in self.fmt_dict.items()}

    @override
    def format(self, record) -> str:
        """
        Mostly the same as the parent's class method, the difference being that a dict is manipulated and dumped as JSON
        instead of a string.
        """
        record.message = record.getMessage()

        if self.usesTime():
            record.asctime = self.formatTime(record, self.datefmt)

        message_dict = self.__as_dict(record)

        if record.exc_info:
            # Cache the traceback text to avoid converting it multiple times
            # (it's constant anyway)
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)

        if record.exc_text:
            message_dict["exc_info"] = record.exc_text

        if record.stack_info:
            message_dict["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(message_dict, default=str)
