import datetime
import typing
import traceback
import sys

from .utils.screens import color_txt, rgb, NerdICONs
from . import configurator


config = configurator.BotConfig.get("hyper-bot")
nf_icons = NerdICONs(getattr(config, "log_use_nf", False))


class Levels:
    def __init__(self):
        self.TRACE = color_txt(f"|{nf_icons.nf_cod_debug_breakpoint_log} Trace    |", rgb(184, 255, 254))
        self.INFO = color_txt(f"|{nf_icons.nf_fa_circle_info} Info     |", rgb(90, 221, 225))
        self.WARNING = color_txt(f"|{nf_icons.nf_fa_warn} Warning  |", rgb(82, 171, 237))
        self.ERROR = color_txt(f"|{nf_icons.nf_cod_error} Error    |", rgb(255, 48, 70))
        self.CRITICAL = color_txt(f"|{nf_icons.nf_cod_bracket_error} Critical |", rgb(178, 33, 48))
        self.DEBUG = color_txt(f"|{nf_icons.nf_cod_debug_alt} Debug    |", rgb(93, 227, 144))

        self.level_nums = {
            self.TRACE: -1,
            self.INFO: 0,
            self.WARNING: 1,
            self.ERROR: 2,
            self.CRITICAL: 3,
            self.DEBUG: 10,
        }

        self.level_names = {
            "TRACE": self.TRACE,
            "INFO": self.INFO,
            "WARNING": self.WARNING,
            "ERROR": self.ERROR,
            "CRITICAL": self.CRITICAL,
            "DEBUG": self.DEBUG,
        }


levels = Levels()


class Logger:
    running_loggers = {}

    def __init__(self):
        self.log_level = levels.INFO

    @classmethod
    def create(cls, key: str, level: str):
        c = cls()
        c.set_level(level)
        cls.running_loggers[key] = c
        print(key)
        print(c)
        print(cls.running_loggers)
        return c

    @classmethod
    def fetch(cls, key: str):
        print(cls.running_loggers)
        return cls.running_loggers.get(key)

    def set_level(self, level: str):
        if level in levels.level_names:
            self.log_level = levels.level_names[level]
        else:
            self.log("未知的日志等级", levels.ERROR)

        return self

    @staticmethod
    def format_exec():
        exc_type, exc_value, exc_traceback = sys.exc_info()
        formatted = color_txt("\nHypeR Bot Exception traceback: \n\n", rgb(255, 47, 47))
        tb_frames = traceback.extract_tb(exc_traceback)
        FILE = color_txt("File", rgb(85, 173, 238))
        LINE = color_txt("line", rgb(85, 173, 238))
        for frame in tb_frames:
            filename, lineno, func_name, code = frame
            formatted += (
                f"  {FILE} {color_txt(filename, rgb(104, 255, 244))},"
                f" {LINE} {color_txt(str(lineno), rgb(215, 255, 255))},"
                f" in {color_txt(func_name, rgb(70, 172, 107))}\n"
                f"      {color_txt(code, rgb(255, 255, 255))}\n\n"
            )
        formatted += f"{color_txt(exc_type.__name__, rgb(255, 47, 47))}: "
        formatted += color_txt(exc_value, rgb(255, 255, 255)) + "\n"

        return formatted

    def register_hook(self) -> None:
        def hook(exc_t: typing.Any, exc_v: typing.Any, exc_tb: typing.Any) -> None:
            self.error(self.format_exec())

        sys.excepthook = hook

    def log(self, message: str, level: str = levels.INFO) -> None:
        if levels.level_nums[level] < levels.level_nums[self.log_level]:
            return
        time = color_txt(nf_icons.nf_weather_time_4 + " " + str(datetime.datetime.now())[:-4], rgb(65, 128, 176))
        if "\n" in message:
            listed = message.split("\n")
            for i in listed:
                if listed.index(i) == 0:
                    listed[0] = "\n"
                    content = f" {time} {level} {color_txt(i, rgb(215, 255, 255))}"
                else:
                    content = " " * int((len(f"{time}{level}") - 2) / 2) + color_txt(i, rgb(215, 255, 255))
                print(content)
        else:
            content = f" {time} {level} {color_txt(message, rgb(215, 255, 255))}"
            print(content)

    def info(self, message: str) -> None:
        self.log(message, levels.INFO)

    def warning(self, message: str) -> None:
        self.log(message, levels.WARNING)

    def error(self, message: str) -> None:
        self.log(message, levels.ERROR)

    def critical(self, message: str) -> None:
        self.log(message, levels.CRITICAL)

    def debug(self, message: str) -> None:
        self.log(message, levels.DEBUG)

    def trace(self, message: str) -> None:
        self.log(message, levels.TRACE)
