from httpx import Response

from ....config import dfconfig
from ..exceptions import UnknownError, UserNotExistsError
from ..http import ApiClient
from .exceptions import (
    DivingFishTokenDisableError,
    DivingFishTokenError,
    DivingFishTokenNotFoundError,
    DivingFishUserDisabledQueryError,
    DivingFishUserNotFoundError,
)
from .models import PlayInfoDefault, PlayInfoDev, UserInfo, UserInfoDev, UserRanking


class DivingFishAPI(ApiClient):
    origin_url = "https://maimai.diving-fish.com/api/maimaidxprober"
    proxy_url = "https://proxy.yuzuchan.site"
    base_url = origin_url

    def __init__(self, qqid: int | None = None, username: str | None = None):
        trusted_origin = self.base_url.rstrip("/") == self.origin_url.rstrip("/")
        super().__init__(
            base_url=self.base_url,
            headers={"developer-token": dfconfig.divingfish_token}
            if dfconfig.divingfish_token and trusted_origin
            else None,
        )
        self.json = {}
        if qqid:
            self.json["qq"] = qqid
        if username:
            self.json["username"] = username
            self.json.pop("qq", None)

    def _handle_error(self, resp: Response):
        if resp.status_code == 200:
            return
        if resp.status_code == 400:
            self._handle_400(resp.json())
        elif resp.status_code == 403:
            raise DivingFishUserDisabledQueryError
        else:
            raise UnknownError

    def _handle_400(self, error: dict):
        msg = error.get("message") or error.get("msg")

        if msg is not None:
            match msg:
                case "no such user":
                    raise DivingFishUserNotFoundError
                case "user not exists":
                    raise UserNotExistsError
                case "开发者token有误":
                    raise DivingFishTokenError
                case "开发者token被禁用":
                    raise DivingFishTokenDisableError
                case "请先联系水鱼申请开发者token":
                    raise DivingFishTokenNotFoundError
                case _:
                    raise UnknownError

    async def _request_data(self, method: str, endpoint: str, **kwargs) -> dict | list:
        return await self._request(method, endpoint, **kwargs)

    @classmethod
    def set_proxy(cls) -> bool:
        # A developer token grants access to complete player records. Never
        # forward it to a third-party proxy; keep all authenticated traffic on
        # the official origin instead.
        if dfconfig.divingfish_token:
            cls.base_url = cls.origin_url
            return False
        cls.base_url = cls.proxy_url + "/maimaidxprober"
        return True

    @classmethod
    def reset_origin(cls) -> None:
        cls.base_url = cls.origin_url

    async def music_data(self) -> list:
        """获取曲目数据"""
        return await self._request_data("GET", "/music_data")

    async def chart_stats(self) -> dict[str, dict[str, list[dict]]]:
        """获取单曲数据"""
        return await self._request_data("GET", "/chart_stats")

    async def query_user_b50(self) -> UserInfo:
        """
        获取玩家B50

        Returns:
            `UserInfo` b50数据模型
        """
        self.json["b50"] = True
        result = await self._request_data("POST", "/query/player", json=self.json)
        return UserInfo.model_validate(result)

    async def query_user_plate(self, version: list[str]) -> list[PlayInfoDefault]:
        """
        请求用户数据

        Params:
            `version`: 版本
        Returns:
            `List[PlayInfoDefault]` 数据列表
        """
        self.json["version"] = version

        result = await self._request_data("POST", "/query/plate", json=self.json)

        return [PlayInfoDefault.model_validate(d) for d in result["verlist"]]

    async def query_user_get_dev(self) -> UserInfoDev:
        """
        使用开发者接口获取用户数据，请确保拥有和输入了开发者 `token`

        Returns:
            `UserInfoDev` 开发者用户信息
        """
        result = await self._request_data(
            "GET", "/dev/player/records", params=self.json
        )
        return UserInfoDev.model_validate(result)

    async def query_user_post_dev(
        self, *, song_id: str | int | list[str | int]
    ) -> list[PlayInfoDev]:
        """
        使用开发者接口获取用户指定曲目数据，请确保拥有和输入了开发者 `token`

        Params:
            `song_id`: 曲目id，可以为单个ID或者列表
        Returns:
            `List[PlayInfoDev]` 开发者成绩列表
        """
        if not isinstance(song_id, list):
            song_id = [song_id]
        self.json["music_id"] = song_id

        result = await self._request_data("POST", "/dev/player/record", json=self.json)
        if result == {}:
            return []

        return [PlayInfoDev.model_validate(d) for k, v in result.items() for d in v]

    async def rating_ranking(self) -> list[UserRanking]:
        """
        获取查分器排行榜

        Returns:
            `List[UserRanking]` 按`ra`从高到低排序后的查分器排行模型列表
        """
        result = await self._request_data("GET", "/rating_ranking")
        return sorted(
            [UserRanking.model_validate(u) for u in result],
            key=lambda x: x.ra,
            reverse=True,
        )
