"""等待用户回填授权码的会话。

落雪和水鱼的绑定都是「BOT 先发起，用户再把一串码发回来」，两边都要回答
同一个问题：**这串码是不是发起绑定的那个人发回来的**。会话按
`(self_id, user_id)` 记，这个键本身就是那道约束——别人发的码落不到这条
记录上，也就走不进兑换那一步。

记录里同时存着是哪个服务在等码。两家的码长得一样（都是 `XXXX-XXXX-XXXX`），
不记服务的话，水鱼的确认码会被落雪的处理器接走，反之亦然。

一个用户同一时刻只保留一条：他发起新的绑定，就是不再继续上一条。
"""

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from hmac import compare_digest
from threading import RLock
from time import monotonic

from .merge.models import ServiceName


@dataclass
class _PendingBinding:
    service: ServiceName
    expires_at: float
    state: str
    in_flight: bool = False


@dataclass(frozen=True)
class PendingBindingClaim:
    key: tuple[int, int]
    state: str


class PendingBindingStore:
    def __init__(
        self,
        ttl: float = 10 * 60,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.ttl = ttl
        self._clock = clock
        self._sessions: dict[tuple[int, int], _PendingBinding] = {}
        self._lock = RLock()

    def start(
        self,
        self_id: int,
        user_id: int,
        service: ServiceName = ServiceName.LXNS,
        *,
        ttl: float | None = None,
    ) -> str:
        """记下这个用户正在等哪一家的码

        `ttl` 不填就用默认值。水鱼那条路要单独给：它的窗口是两段接起来的，
        见 `commands.mai_base.DIVINGFISH_SESSION_TTL`。
        """
        with self._lock:
            now = self._clock()
            self._clear_expired(now)
            state = secrets.token_urlsafe(32)
            self._sessions[(self_id, user_id)] = _PendingBinding(
                service=service,
                expires_at=now + (ttl or self.ttl),
                state=state,
            )
            return state

    def active(self, self_id: int, user_id: int) -> ServiceName | None:
        """这个用户此刻在等哪一家的码，没有则返回 None"""
        with self._lock:
            now = self._clock()
            self._clear_expired(now)
            session = self._sessions.get((self_id, user_id))
            return session.service if session else None

    def is_active(
        self,
        self_id: int,
        user_id: int,
        service: ServiceName | None = None,
    ) -> bool:
        active = self.active(self_id, user_id)
        return active is not None if service is None else active == service

    def consume(self, self_id: int, user_id: int) -> bool:
        with self._lock:
            now = self._clock()
            self._clear_expired(now)
            return self._sessions.pop((self_id, user_id), None) is not None

    def discard(self, self_id: int, user_id: int) -> None:
        with self._lock:
            self._sessions.pop((self_id, user_id), None)

    def matches_state(
        self,
        self_id: int,
        user_id: int,
        state: str | None,
    ) -> bool:
        if not state or not str(state).isascii():
            return False
        with self._lock:
            now = self._clock()
            self._clear_expired(now)
            session = self._sessions.get((self_id, user_id))
            return bool(
                session
                and compare_digest(session.state, str(state))
            )

    def claim(
        self,
        self_id: int,
        user_id: int,
        state: str | None,
    ) -> PendingBindingClaim | None:
        if not state:
            return None
        key = (self_id, user_id)
        with self._lock:
            now = self._clock()
            self._clear_expired(now)
            session = self._sessions.get(key)
            if (
                session is None
                or session.in_flight
                or not compare_digest(session.state, str(state))
            ):
                return None
            session.in_flight = True
            return PendingBindingClaim(key, session.state)

    def claim_without_state(
        self,
        self_id: int,
        user_id: int,
    ) -> PendingBindingClaim | None:
        key = (self_id, user_id)
        with self._lock:
            now = self._clock()
            self._clear_expired(now)
            session = self._sessions.get(key)
            if session is None or session.in_flight:
                return None
            session.in_flight = True
            return PendingBindingClaim(key, session.state)

    def is_in_flight(self, self_id: int, user_id: int) -> bool:
        with self._lock:
            now = self._clock()
            self._clear_expired(now)
            session = self._sessions.get((self_id, user_id))
            return bool(session and session.in_flight)

    def release(self, claim: PendingBindingClaim) -> bool:
        with self._lock:
            session = self._sessions.get(claim.key)
            if (
                session is None
                or not session.in_flight
                or not compare_digest(session.state, claim.state)
            ):
                return False
            session.in_flight = False
            return True

    def complete(self, claim: PendingBindingClaim) -> bool:
        with self._lock:
            session = self._sessions.get(claim.key)
            if (
                session is None
                or not session.in_flight
                or not compare_digest(session.state, claim.state)
            ):
                return False
            del self._sessions[claim.key]
            return True

    def _clear_expired(self, now: float) -> None:
        expired = [
            key
            for key, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for key in expired:
            del self._sessions[key]
