from Tools.tools import _paginate_help_rows, _parse_help_rows


def test_help_rows_preserve_text_between_plugin_command_groups():
    text = "\n".join(
        [
            "帮助",
            "注：对话前必须加上 ~",
            "~插件视角 -> 查看已加载功能",
            "~设置帮助模式 -> 切换帮助样式",
            "插件功能：",
            "~wordle -> 开始猜单词",
            "~wordle 提示 -> 查看提示",
            "可用词典：CET4、CET6",
        ]
    )

    title, rows = _parse_help_rows(text)

    assert title == "帮助"
    assert rows == [
        ("text", "注：对话前必须加上 ~"),
        (
            "commands",
            (
                ("~插件视角", "查看已加载功能"),
                ("~设置帮助模式", "切换帮助样式"),
            ),
        ),
        ("text", "插件功能："),
        (
            "commands",
            (
                ("~wordle", "开始猜单词"),
                ("~wordle 提示", "查看提示"),
            ),
        ),
        ("text", "可用词典：CET4、CET6"),
    ]


def test_help_pages_fit_forty_commands_in_five_columns_and_eight_rows():
    fourteen = "\n".join(
        ["帮助", *(f"命令{i} -> 说明{i}" for i in range(14))]
    )
    forty = "\n".join(
        ["帮助", *(f"命令{i} -> 说明{i}" for i in range(40))]
    )
    forty_one = "\n".join(
        ["帮助", *(f"命令{i} -> 说明{i}" for i in range(41))]
    )

    _, fourteen_rows = _parse_help_rows(fourteen)
    _, forty_rows = _parse_help_rows(forty)
    _, forty_one_rows = _parse_help_rows(forty_one)

    assert len(_paginate_help_rows(fourteen_rows)) == 1
    assert len(_paginate_help_rows(forty_rows)) == 1
    assert len(_paginate_help_rows(forty_one_rows)) == 2
