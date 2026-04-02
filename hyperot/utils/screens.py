import random


def rgb(r: int, g: int, b: int) -> tuple[int, int, int]:
    return r, g, b


def color_txt(text: str, color: tuple[int, int, int]) -> str:
    r = color[0]
    g = color[1]
    b = color[2]
    return f"\x1b[38;2;{r};{g};{b}m{text}\x1b[0m"


start_up = []


def play_startup():
    return


def play_info(version: str):
    print("    JianerQQ机器人 版本 NEXT4Preview2")
    print("    基于HypeR Bot by HarcicYang 版本 0.81.2开发\n")

class NerdICONs:
    def __init__(self, enable: bool):
        self.enable = enable

    def __getattribute__(self, item) -> str:
        if super().__getattribute__("enable"):
            return str(super().__getattribute__(item))
        else:
            return " "

    nf_fa_circle_info = " \uf05a"
    nf_cod_bracket_error = " \uebe6"
    nf_cod_error = " \uea87"
    nf_fa_warn = " \uf071"
    nf_cod_debug_alt = " \ueb91"
    nf_cod_debug_breakpoint_log = " \ueaab"
    nf_weather_time_4 = " \ue385"




