import asyncio
import json
import os
import random
from pathlib import Path

import numpy as np
from PIL import Image

from ...config import log, lxnsconfig
from ...resources import group_alias_file, guess_file, local_alias_file
from ..merge import merge_alias_data, merge_music_data
from ..merge.alias_list import AliasList
from ..merge.models import (
    Alias,
    AliasesPush,
    GuessDefaultData,
    GuessPicData,
    GuessSwitch,
    SimpleSong,
    Song,
)
from ..merge.music_list import MusicList
from ..tool import openfile, writefile
from .diving_fish import get_music_list
from .lxns import get_music_aliases, get_music_data
from .yuzuchan import get_music_alias_list, get_plate_data


class MaiMusic:
    total_list: MusicList
    """曲目数据"""
    total_alias_list: AliasList
    """别名数据"""
    total_plate_id_list: dict[str, list[int]]
    """牌子ID列表数据"""
    total_level_data: dict[str, dict[str, list[SimpleSong]]]
    """等级列表数据"""
    total_level_value_map: dict[str, float]
    """定数字典数据，以`song_id-level_index`为key，例如`{"11451-3": 13.5}`"""

    def __init__(self) -> None:
        """封装所有曲目信息以及猜歌数据，便于更新"""

    async def get_music(self) -> None:
        """获取所有曲目数据"""
        df_music_data, df_stats_data = await get_music_list()
        log.success("水鱼查分器曲目数据已加载")
        if lxnsconfig.lxns_dev_token:
            lxns_data = await get_music_data()
            log.success("成功获取落雪查分器曲目数据")
        else:
            lxns_data = None
            log.opt(colors=True).warning(
                "<r>未配置落雪查分器开发者 Token，跳过获取落雪查分器曲目数据</r>"
            )

        log.info("正在合并曲目数据")
        self.total_list, self.total_level_value_map = await merge_music_data(
            diving_fish_list=df_music_data, lxns_list=lxns_data, stats_map=df_stats_data
        )
        log.success("曲目数据合并完成")

        self.total_level_data = self.total_list.by_level_list()

    async def get_music_alias(self) -> None:
        """获取所有曲目别名"""
        yuzu_data = await get_music_alias_list()
        log.success("Yuri-YuzuChaN别名数据已加载")
        if lxnsconfig.lxns_dev_token:
            lxns_data = await get_music_aliases()
            log.success("成功获取落雪查分器别名数据")
        else:
            lxns_data = None
            log.opt(colors=True).warning(
                "<r>未配置落雪查分器开发者 Token，跳过获取落雪查分器别名数据</r>"
            )

        local_alias_data = {}
        if local_alias_file.exists():
            local_alias_data = await openfile(local_alias_file)
        if not local_alias_data:
            local_alias_data = None

        log.info("正在合并别名数据")
        self.total_alias_list = await merge_alias_data(
            yuzu_data, lxns_data, local_alias_data
        )
        log.success("别名数据合并完成")

    async def get_plate_json(self) -> None:
        """获取所有牌子数据"""
        self.total_plate_id_list = await get_plate_data()
        log.success("牌子数据已加载")

    async def update(self) -> None:
        """更新数据"""
        await self.get_music()
        await self.get_music_alias()
        await self.get_plate_json()
        log.success("maimaiDX数据更新完毕")


mai = MaiMusic()


def _reserve_corrupt_backup(path: Path) -> Path:
    base = Path(f"{path}.corrupt")
    index = 0
    while True:
        candidate = base if index == 0 else Path(f"{base}.{index}")
        try:
            descriptor = os.open(
                candidate,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            index += 1
            continue
        os.close(descriptor)
        return candidate


def _backup_corrupt_file(path: Path) -> Path:
    backup = _reserve_corrupt_backup(path)
    try:
        os.replace(path, backup)
    except BaseException:
        try:
            backup.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return backup


def _load_switch_file(path: Path, model_type, label: str):
    if not path.exists():
        return model_type()
    try:
        with path.open("r", encoding="utf-8") as stream:
            return model_type.model_validate(json.load(stream))
    except Exception as exc:
        try:
            backup = _backup_corrupt_file(path)
        except Exception as backup_exc:
            log.warning(
                f"{label}状态文件损坏，备份失败，已采用默认值："
                f"{type(exc).__name__}/{type(backup_exc).__name__}"
            )
        else:
            log.warning(
                f"{label}状态文件损坏，已移动为 {backup.name} 并采用默认值："
                f"{type(exc).__name__}"
            )
        return model_type()


class GroupAlias:
    push: AliasesPush

    def __init__(self) -> None:
        """别名推送类"""
        self._state_lock = asyncio.Lock()
        self.push = _load_switch_file(
            group_alias_file,
            AliasesPush,
            "群别名推送",
        )

    async def on(self, gid: int) -> str:
        """开启推送"""
        async with self._state_lock:
            updated = self.push.model_copy(deep=True)
            if gid not in updated.enable:
                updated.enable.append(gid)
            if gid in updated.disable:
                updated.disable.remove(gid)
            await writefile(group_alias_file, updated.model_dump())
            self.push = updated
        return "群别名推送功能已开启"

    async def off(self, gid: int) -> str:
        """关闭推送"""
        async with self._state_lock:
            updated = self.push.model_copy(deep=True)
            if gid not in updated.disable:
                updated.disable.append(gid)
            if gid in updated.enable:
                updated.enable.remove(gid)
            await writefile(group_alias_file, updated.model_dump())
            self.push = updated
        return "群别名推送功能已关闭"

    async def alias_global_change(self, switch: bool, group_list: list[int]):
        """修改全局开关"""
        async with self._state_lock:
            updated = self.push.model_copy(deep=True)
            if switch:
                updated.disable.clear()
                updated.enable.clear()
                updated.enable.extend(group_list)
            else:
                updated.enable.clear()
                updated.disable.clear()
                updated.disable.extend(group_list)
            await writefile(group_alias_file, updated.model_dump())
            self.push = updated


alias = GroupAlias()


class Guess:
    _group: dict[int, GuessDefaultData | GuessPicData] = {}
    switch: GuessSwitch
    hot_music_ids: list[int] = []
    _guess_data: list[Song] = []

    def __init__(self) -> None:
        """猜歌类"""
        self._state_lock = asyncio.Lock()
        self.switch = _load_switch_file(
            guess_file,
            GuessSwitch,
            "群猜歌",
        )

    def guess(self):
        """初始化猜歌数据"""
        self.hot_music_ids = []
        self._guess_data = []
        for song in mai.total_list.root:
            count = 0
            for diff in song.difficulties:
                if diff.stats:
                    count += diff.stats.cnt if diff.stats.cnt else 0
            if count > 10000:
                self.hot_music_ids.append(song.song_id)
        self._guess_data = list(
            filter(lambda x: x.song_id in self.hot_music_ids, mai.total_list.root)
        )

    def start(self, gid: int):
        """开始猜歌"""
        self._group[gid] = self.guess_data()

    def startpic(self, gid: int):
        """开始猜曲绘"""
        self._group[gid] = self.guess_pic_data()

    def calculate_frequency_weights(self, image: Image.Image) -> np.ndarray:
        """
        计算图像的频率权重，用于在图像中选择裁剪区域

        Params:
            `image`: PIL.Image.Image, 输入图像
        Returns:
            `np.ndarray` 频率权重矩阵
        """
        gray_image = np.array(image.convert("L"))
        freq = np.fft.fft2(gray_image)
        freq_shift = np.fft.fftshift(freq)
        magnitude = np.abs(freq_shift)
        normalized_magnitude = magnitude / magnitude.max()
        weights = normalized_magnitude**2
        return weights

    def select_crop_region(
        self, weights: np.ndarray, crop_width: int, crop_height: int, top_p: int
    ) -> tuple[int, int]:
        h, w = weights.shape
        valid_regions = weights[: h - crop_height + 1, : w - crop_width + 1]
        flattened_weights = valid_regions.flatten()
        threshold = np.percentile(flattened_weights, top_p)
        valid_indices = np.where(flattened_weights >= threshold)[0]
        probabilities = flattened_weights[valid_indices]
        probabilities /= probabilities.sum()
        chosen_index = np.random.choice(valid_indices, p=probabilities)
        top_left_y = chosen_index // valid_regions.shape[1]
        top_left_x = chosen_index % valid_regions.shape[1]
        return top_left_x, top_left_y

    def _pic(self, song: Song) -> Image.Image:
        """裁切曲绘"""
        from ..image.tools import song_chart

        im = Image.open(song_chart(song.song_id))
        w, h = im.size
        weights = self.calculate_frequency_weights(im)
        scale = random.uniform(0.15, 0.4)  # 裁剪尺寸范围 可在此修改
        w2, h2 = int(w * scale), int(h * scale)
        top_p = min(1.3 - np.power(scale, 0.4), 0.95) * 100
        x, y = self.select_crop_region(weights, w2, h2, top_p)
        im = im.crop((x, y, x + w2, y + h2))
        return im

    def guess_pic_data(self) -> GuessPicData:
        """猜曲绘数据"""
        from ..image.tools import image_to_base64

        song = random.choice(self._guess_data)
        pic = self._pic(song)
        _alias = mai.total_alias_list.by_id(song.song_id)
        answer = list(_alias[0].alias) if _alias else []
        answer.append(str(song.song_id))
        return GuessPicData(
            song=song, img=image_to_base64(pic), answer=answer, end=False
        )

    def guess_data(self) -> GuessDefaultData:
        """猜歌数据"""
        from ..image.tools import image_to_base64

        song = random.choice(self._guess_data)
        guess_options = random.sample(
            [
                f"的 Expert 难度是 {song.difficulties[2].level}",
                f"的 Master 难度是 {song.difficulties[3].level}",
                f"的分类是 {song.genre}",
                f"的版本是 {song.version_str}",
                f"的艺术家是 {song.artist}",
                f"{'不' if song.type == 'SD' else ''}是 DX 谱面",
                f"{'没' if len(song.difficulties) == 4 else ''}有白谱",
                f"的 BPM 是 {song.bpm}",
            ],
            6,
        )
        _alias = mai.total_alias_list.by_id(song.song_id)
        answer = list(_alias[0].alias) if _alias else []
        answer.append(str(song.song_id))
        pic = self._pic(song)
        return GuessDefaultData(
            song=song,
            img=image_to_base64(pic),
            answer=answer,
            end=False,
            options=guess_options,
        )

    def end(self, gid: int):
        """结束猜歌"""
        del self._group[gid]

    async def on(self, gid: int) -> str:
        """开启猜歌"""
        async with self._state_lock:
            updated = self.switch.model_copy(deep=True)
            if gid not in updated.enable:
                updated.enable.append(gid)
            if gid in updated.disable:
                updated.disable.remove(gid)
            await writefile(guess_file, updated.model_dump())
            self.switch = updated
        return "群猜歌功能已开启"

    async def off(self, gid: int) -> str:
        """关闭猜歌"""
        async with self._state_lock:
            updated = self.switch.model_copy(deep=True)
            if gid not in updated.disable:
                updated.disable.append(gid)
            if gid in updated.enable:
                updated.enable.remove(gid)
            await writefile(guess_file, updated.model_dump())
            self.switch = updated
            if gid in self._group:
                self.end(gid)
        return "群猜歌功能已关闭"


guess = Guess()


_local_alias_lock = asyncio.Lock()


async def update_local_alias(song_id: int, alias_name: str) -> bool:
    try:
        async with _local_alias_lock:
            song_id_key = str(song_id)
            alias = alias_name.lower()

            local_alias_data: dict[str, list[str]] = {}
            if local_alias_file.exists():
                loaded = await openfile(local_alias_file)
                if not isinstance(loaded, dict):
                    raise ValueError("local alias state must be an object")
                local_alias_data = loaded

            if song_id_key not in local_alias_data:
                local_alias_data[song_id_key] = []
            if alias not in local_alias_data[song_id_key]:
                local_alias_data[song_id_key].append(alias)

            await writefile(local_alias_file, local_alias_data)

            entries = mai.total_alias_list.by_id(song_id)
            if entries:
                if alias not in entries[0].alias:
                    entries[0].alias.append(alias)
            else:
                _song = mai.total_list.by_id(song_id)
                mai.total_alias_list.root.append(
                    Alias(
                        song_id=song_id,
                        song_name=_song.song_name if _song else "",
                        alias=[alias],
                    )
                )
        return True
    except Exception as e:
        log.error(f"添加本地别名失败: {e}")
        return False
