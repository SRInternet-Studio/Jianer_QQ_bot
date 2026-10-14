import asyncio
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from httpx import Response

from ....config import lxnsconfig
from ...database.qq import get_user, update_user
from ..exceptions import UnknownError, UserNotBindError
from ..http import ApiClient
from .exceptions import (
    LXNSNotFoundError,
    LXNSOAuthError,
    LXNSParamsError,
    LXNSPermissionDeniedError,
    LXNSTokenError,
    LXNSTooManyRequestsError,
)
from .models import (
    Aliases,
    APIResult,
    BaseToken,
    Best50,
    Collection,
    LevelIndex,
    OAuth2Token,
    Player,
    RatingTrend,
    Score,
    Song,
    Songs,
    SongType,
)


_refresh_locks: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, asyncio.Lock],
] = WeakKeyDictionary()


def _oauth_token_payload(result: dict) -> dict:
    """Prefer the OAuth 2.0 top-level token response over its legacy wrapper."""

    if not isinstance(result, dict):
        return result
    if "access_token" in result:
        return result
    legacy = result.get("data")
    return legacy if isinstance(legacy, dict) else result


def _refresh_lock(user_id: int | str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _refresh_locks.setdefault(loop, {})
    return locks.setdefault(str(user_id), asyncio.Lock())


@dataclass(frozen=True)
class _LxnsAuthSnapshot:
    authorization: str | None
    access_token: str | None
    refresh_token: str | None


class OAuth2(ApiClient):
    def __init__(self):
        super().__init__(
            base_url="https://maimai.lxns.net",
        )
        self.client_id = lxnsconfig.lx_client_id
        self.client_secret = lxnsconfig.lx_client_secret
        self.redirect_uri = lxnsconfig.redirect_uri
        self.token: OAuth2Token | BaseToken | None = None

    async def fetch_token(self, code: str) -> OAuth2Token:
        """通过授权码获取 `access_token`"""
        json = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        }
        result = await self._request_data("POST", "/api/v0/oauth/token", json=json)
        self.token = OAuth2Token.model_validate(_oauth_token_payload(result))
        return self.token

    async def refresh_token(self) -> OAuth2Token:
        if not self.token:
            raise LXNSTokenError

        json = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self.token.refresh_token,
        }
        result = await self._request_data("POST", "/api/v0/oauth/token", json=json)
        self.token = OAuth2Token.model_validate(_oauth_token_payload(result))
        return self.token

    async def _request_data(self, method: str, endpoint: str, **kwargs) -> dict:
        return await self._request(method, endpoint, **kwargs)

    def _handle_error(self, resp: Response) -> None:
        if 200 <= resp.status_code < 300:
            return
        try:
            payload = resp.json()
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        raise LXNSOAuthError(
            payload.get("error") or f"http_{resp.status_code}",
            payload.get("error_description"),
        )


class LxnsClient(ApiClient):
    def __init__(
        self,
        *,
        base_url: str,
        headers: dict[str, str],
        user_id: int | str | None,
        token: OAuth2Token | BaseToken | None = None,
    ):
        super().__init__(base_url=base_url, headers=headers)
        self.user_id = user_id
        self._token = token
        self._friend_code: int | None = None

    def _request_auth_snapshot(self, headers: dict) -> _LxnsAuthSnapshot:
        token = self._token
        authorization = headers.get("Authorization")
        return _LxnsAuthSnapshot(
            authorization=(
                str(authorization) if authorization is not None else None
            ),
            access_token=token.access_token if token else None,
            refresh_token=token.refresh_token if token else None,
        )

    async def _on_unauthorized(
        self, auth_snapshot: object | None = None
    ) -> bool:
        """
        刷新 token
        """
        if self.user_id is None:
            return False

        if not isinstance(auth_snapshot, _LxnsAuthSnapshot):
            auth_snapshot = self._request_auth_snapshot(dict(self.headers))
        if not auth_snapshot.access_token or not auth_snapshot.refresh_token:
            return False

        try:
            user_id = int(self.user_id)
        except (TypeError, ValueError):
            return False

        async with _refresh_lock(user_id):
            current_token = self._token
            if current_token and not self._token_matches_snapshot(
                current_token, auth_snapshot
            ):
                self._adopt_token(current_token)
                return True

            try:
                user = await get_user(user_id)
            except UserNotBindError:
                latest_token = None
            except Exception:
                return False
            else:
                latest_token = (
                    BaseToken(
                        access_token=user.access_token,
                        refresh_token=user.refresh_token,
                    )
                    if user.access_token and user.refresh_token
                    else None
                )

            if latest_token and not self._token_matches_snapshot(
                latest_token, auth_snapshot
            ):
                self._adopt_token(latest_token)
                return True

            oauth = OAuth2()
            oauth.token = latest_token or current_token or BaseToken(
                access_token=auth_snapshot.access_token,
                refresh_token=auth_snapshot.refresh_token,
            )

            try:
                new_token = await oauth.refresh_token()
                await update_user(user_id, token=new_token)
            except Exception:
                self._token = None
                return False

            self._adopt_token(new_token)
            return True

    @staticmethod
    def _token_matches_snapshot(
        token: OAuth2Token | BaseToken,
        auth_snapshot: _LxnsAuthSnapshot,
    ) -> bool:
        return (
            token.access_token == auth_snapshot.access_token
            and token.refresh_token == auth_snapshot.refresh_token
        )

    def _adopt_token(self, token: OAuth2Token | BaseToken) -> None:
        self._token = token
        token_type = getattr(token, "token_type", "Bearer")
        self.headers["Authorization"] = (
            f"{token_type} {token.access_token}"
        )
        self._friend_code = None

    def _handle_error(self, resp: Response):
        match resp.status_code:
            case 200:
                return
            case 400:
                raise LXNSParamsError
            case 401:
                self._friend_code = None
                raise LXNSOAuthError
            case 403:
                raise LXNSPermissionDeniedError
            case 404:
                raise LXNSNotFoundError
            case 429:
                raise LXNSTooManyRequestsError
            case _:
                raise UnknownError

    async def _request_data(self, method: str, endpoint: str, **kwargs) -> APIResult:
        data = await self._request(method, endpoint, **kwargs)
        return APIResult.model_validate(data)

    async def _request_base_data(self, method: str, endpoint: str, **kwargs) -> dict:
        return await self._request(method, endpoint, **kwargs)


class LxnsAPI:
    def __init__(
        self, user_id: str | None = None, token: OAuth2Token | BaseToken | None = None
    ):
        self._oauth_client = (
            LxnsClient(
                base_url="https://maimai.lxns.net/api/v0/user/maimai/player",
                headers={"Authorization": f"Bearer {token.access_token}"},
                user_id=user_id,
                token=token,
            )
            if token
            else None
        )

        self._dev_client = LxnsClient(
            base_url="https://maimai.lxns.net/api/v0/maimai",
            headers={"Authorization": lxnsconfig.lxns_dev_token},
            user_id=user_id,
            token=None,
        )

    async def music_data(self) -> Songs:
        """获取曲目数据"""
        result = await self._dev_client._request_base_data(
            "GET", "/song/list", params={"notes": True}
        )
        return Songs.model_validate(result)

    async def single_music_data(self, song_id: str) -> Song:
        """获取单个曲目数据"""
        result = await self._dev_client._request_base_data("GET", f"/song/{song_id}")
        return Song.model_validate(result)

    async def music_alias_data(self) -> Aliases:
        """获取别名列表"""
        result = await self._dev_client._request_base_data("GET", "/alias/list")
        return Aliases.model_validate(result)

    async def player(
        self, *, friend_code: int | None = None, qq: int | None = None
    ) -> Player:
        """获取玩家信息"""

        if friend_code is not None:
            result = await self._dev_client._request_data(
                "GET", f"/player/{friend_code}"
            )
        elif qq is not None:
            result = await self._dev_client._request_data("GET", f"/player/qq/{qq}")
        else:
            result = await self._oauth_client._request_data("GET", "")

        return Player.model_validate(result.data)

    async def single_best(
        self, song_id: int, level_index: LevelIndex, song_type: SongType
    ) -> Score:
        """
        获取曲目指定难度成绩
        """
        params = {
            "song_id": song_id,
            "level_index": level_index.value,
            "song_type": song_type.value,
        }
        result = await self._oauth_client._request_data("GET", "/best", params=params)
        return Score.model_validate(result.data)

    async def best50(self) -> Best50:
        """
        获取 `b50`
        """
        result = await self._oauth_client._request_data("GET", "/bests")
        return Best50.model_validate(result.data)

    async def ap50(self, friend_code: int) -> Best50:
        """
        获取 `ap50`
        """
        result = await self._dev_client._request_data(
            "GET", f"/player/{friend_code}/bests/ap"
        )
        return Best50.model_validate(result.data)

    async def song_bests(self, song_id: int, song_type: SongType) -> list[Score]:
        """
        获取指定曲目所有难度成绩
        """
        params = {"song_id": song_id, "song_type": song_type.value}
        result = await self._oauth_client._request_data("GET", "/bests", params=params)
        return [Score.model_validate(s) for s in result.data]

    async def recent50(self) -> list[Score]:
        """
        获取最近游玩的 50 个成绩
        """
        result = await self._oauth_client._request_data("GET", "/recents")
        return [Score.model_validate(s) for s in result.data]

    async def all_best(self) -> list[Score]:
        """
        获取所有成绩
        """
        result = await self._oauth_client._request_data("GET", "/scores")
        return [Score.model_validate(s) for s in result.data]

    async def heatmap(self) -> dict[str, int]:
        """
        获取玩家上传热力图
        """
        result = await self._oauth_client._request_data("GET", "/heatmap")
        return result.data

    async def trend(self, version: int) -> list[RatingTrend]:
        """
        获取玩家 DX Rating 趋势
        """
        params = {"version": version}
        result = await self._oauth_client._request_data("GET", "/trend", params=params)
        return [RatingTrend.model_validate(s) for s in result.data]

    async def history(
        self, song_id: int, song_type: SongType, level_index: LevelIndex
    ) -> list[Score]:
        """
        获取玩家成绩游玩历史记录
        """
        params = {
            "song_id": song_id,
            "song_type": song_type.value,
            "level_index": level_index.value,
        }
        result = await self._oauth_client._request_data(
            "GET", "/score/history", params=params
        )
        return [Score.model_validate(s) for s in result.data]

    async def collection(self, collection_type: str, collection_id: int) -> Collection:
        """
        获取玩家收藏品进度
        """
        result = await self._oauth_client._request_data(
            "GET", f"/{collection_type}/{collection_id}"
        )
        return Collection.model_validate(result.data)
