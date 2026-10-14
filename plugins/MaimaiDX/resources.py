"""Paths and readiness checks for the externally supplied resource pack."""

from pathlib import Path

from .config import maiconfig


Root = Path(__file__).resolve().parent
static = Path(maiconfig.maimaidx_path).resolve()
state_dir = Path(maiconfig.state_path).resolve()

font_dir = static / "font"
data_dir = static / "data"
mai_dir = static / "mai"
pic_dir = mai_dir / "pic"
cover_dir = mai_dir / "cover"
plate_dir = mai_dir / "plate"
shougou_dir = mai_dir / "shougou"
plate_version_dir = mai_dir / "plate_version"
plate_table_dir = mai_dir / "plate_table"
rating_table_dir = mai_dir / "rating_table"

pie_html_file = static / "temp_pie.html"
guess_file = data_dir / "group_guess_switch.json"
group_alias_file = data_dir / "group_alias_switch.json"
alias_file = data_dir / "music_alias.json"
lxns_alias_file = data_dir / "lxns_music_alias.json"
local_alias_file = data_dir / "local_music_alias.json"
music_file = data_dir / "music_data.json"
lxns_music_file = data_dir / "lxns_music_data.json"
chart_file = data_dir / "music_chart.json"
plate_file = data_dir / "plate_data.json"
merge_music_file = data_dir / "merge_music_data.json"
merge_alias_file = data_dir / "merge_music_alias.json"

SIYUAN = font_dir / "ResourceHanRoundedCN-Bold.ttf"
SHANGGUMONO = font_dir / "ShangguMonoSC-Regular.otf"
TBFONT = font_dir / "Torus SemiBold.otf"
FOTNEWRODIN = font_dir / "FOT-NewRodin Pro EB.otf"


def ensure_runtime_dirs() -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    plate_table_dir.mkdir(parents=True, exist_ok=True)
    rating_table_dir.mkdir(parents=True, exist_ok=True)


def resource_issues() -> tuple[str, ...]:
    expected = [
        font_dir,
        pic_dir,
        cover_dir,
        shougou_dir,
        plate_version_dir,
        SIYUAN,
        SHANGGUMONO,
        TBFONT,
        FOTNEWRODIN,
        cover_dir / "0.png",
    ]
    if not maiconfig.assets_online:
        expected.append(plate_dir)
    return tuple(str(path) for path in expected if not path.exists())


def resources_ready() -> bool:
    return not resource_issues()
