from . import replace_res, replace_common, replace_listener


def load_onebot():
    from hyperot.LecAdapters.OneBotLib import Res as OneBotRes

    replace_res(OneBotRes)

    from hyperot.LecAdapters.OneBotLib import Manager as OneBotCommon

    replace_common(OneBotCommon)

    from hyperot.LecAdapters import OneBot as OneBotListener

    replace_listener(OneBotListener)


def load_milky():
    from hyperot.LecAdapters.MilkyLib import Res as MilkyRes

    replace_res(MilkyRes)

    from hyperot.LecAdapters.MilkyLib import Manager as MilkyCommon

    replace_common(MilkyCommon)

    from hyperot.LecAdapters import Milky as MilkyListener

    replace_listener(MilkyListener)


def load_feishu():
    from hyperot.LecAdapters.FeishuLib import Res as FeishuRes

    replace_res(FeishuRes)

    from hyperot.LecAdapters.FeishuLib import Manager as FeishuCommon

    replace_common(FeishuCommon)

    from hyperot.LecAdapters import Feishu as FeishuListener

    replace_listener(FeishuListener)
