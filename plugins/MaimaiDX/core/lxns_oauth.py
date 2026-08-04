import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from hmac import compare_digest
from threading import RLock
from time import monotonic
from urllib.parse import parse_qs, urlencode, urlparse

AUTHORIZATION_CODE_PATTERN = re.compile(
    r"^(?:[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}"
    r"|[A-Za-z0-9_-]{16,256})$"
)
OAUTH_STATE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
LXNS_OOB_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"


def is_oob_redirect_uri(value: str | None) -> bool:
    return str(value or "").strip().casefold() == LXNS_OOB_REDIRECT_URI


def build_authorize_url(
    client_id: str,
    redirect_uri: str,
    *,
    state: str | None = None,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "read_player",
    }
    if state:
        params["state"] = state
    query = urlencode(params)
    return f"https://maimai.lxns.net/oauth/authorize?{query}"


def is_binding_channel_allowed(*, private_only: bool, is_private: bool) -> bool:
    return is_private or not private_only


def extract_authorization_code(text: str) -> str | None:
    return extract_authorization_response(text).code


@dataclass(frozen=True)
class AuthorizationResponse:
    code: str | None
    state: str | None = None


def extract_authorization_response(text: str) -> AuthorizationResponse:
    value = text.strip()
    if AUTHORIZATION_CODE_PATTERN.fullmatch(value):
        return AuthorizationResponse(value)

    prefixed = re.fullmatch(r"授权码\s*[:：]?\s*(\S+)", value)
    if prefixed:
        code = prefixed.group(1)
        if AUTHORIZATION_CODE_PATTERN.fullmatch(code):
            return AuthorizationResponse(code)

    try:
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"}:
            query = parse_qs(parsed.query)
            code = query.get("code", [None])[0]
            state = query.get("state", [None])[0]
            if code and AUTHORIZATION_CODE_PATTERN.fullmatch(code):
                return AuthorizationResponse(code, state)
    except (UnicodeError, ValueError):
        pass

    return AuthorizationResponse(None)


BindingId = int | str
BindingKey = tuple[BindingId, BindingId]


@dataclass
class _PendingBinding:
    expires_at: float
    state: str
    in_flight: bool = False


@dataclass(frozen=True)
class PendingBindingClaim:
    key: BindingKey
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
        self._sessions: dict[BindingKey, _PendingBinding] = {}
        self._lock = RLock()

    def start(self, self_id: BindingId, user_id: BindingId) -> str:
        with self._lock:
            now = self._clock()
            self._clear_expired(now)
            state = secrets.token_urlsafe(32)
            self._sessions[(self_id, user_id)] = _PendingBinding(
                expires_at=now + self.ttl,
                state=state,
            )
            return state

    def is_active(self, self_id: BindingId, user_id: BindingId) -> bool:
        with self._lock:
            now = self._clock()
            self._clear_expired(now)
            return (self_id, user_id) in self._sessions

    def matches_state(
        self,
        self_id: BindingId,
        user_id: BindingId,
        state: str | None,
    ) -> bool:
        if not self._valid_state(state):
            return False
        with self._lock:
            now = self._clock()
            self._clear_expired(now)
            session = self._sessions.get((self_id, user_id))
            return session is not None and self._same_state(session.state, state)

    def claim(
        self,
        self_id: BindingId,
        user_id: BindingId,
        state: str | None,
    ) -> PendingBindingClaim | None:
        if not self._valid_state(state):
            return None
        key = (self_id, user_id)
        with self._lock:
            now = self._clock()
            self._clear_expired(now)
            session = self._sessions.get(key)
            if (
                session is None
                or session.in_flight
                or not self._same_state(session.state, state)
            ):
                return None
            session.in_flight = True
            return PendingBindingClaim(key=key, state=session.state)

    def claim_without_state(
        self,
        self_id: BindingId,
        user_id: BindingId,
    ) -> PendingBindingClaim | None:
        """Atomically claim the caller's active OOB binding session."""

        key = (self_id, user_id)
        with self._lock:
            now = self._clock()
            self._clear_expired(now)
            session = self._sessions.get(key)
            if session is None or session.in_flight:
                return None
            session.in_flight = True
            return PendingBindingClaim(key=key, state=session.state)

    def is_in_flight(self, self_id: BindingId, user_id: BindingId) -> bool:
        with self._lock:
            now = self._clock()
            self._clear_expired(now)
            session = self._sessions.get((self_id, user_id))
            return bool(session is not None and session.in_flight)

    def release(self, claim: PendingBindingClaim) -> bool:
        with self._lock:
            now = self._clock()
            self._clear_expired(now)
            session = self._sessions.get(claim.key)
            if (
                session is None
                or not session.in_flight
                or not self._same_state(session.state, claim.state)
            ):
                return False
            session.in_flight = False
            return True

    def complete(self, claim: PendingBindingClaim) -> bool:
        with self._lock:
            now = self._clock()
            self._clear_expired(now)
            session = self._sessions.get(claim.key)
            if (
                session is None
                or not session.in_flight
                or not self._same_state(session.state, claim.state)
            ):
                return False
            del self._sessions[claim.key]
            return True

    def discard(self, self_id: BindingId, user_id: BindingId) -> None:
        with self._lock:
            self._sessions.pop((self_id, user_id), None)

    @staticmethod
    def _valid_state(state: str | None) -> bool:
        return bool(
            isinstance(state, str)
            and state.isascii()
            and OAUTH_STATE_PATTERN.fullmatch(state)
        )

    @staticmethod
    def _same_state(expected: str, received: str) -> bool:
        return compare_digest(expected.encode("ascii"), received.encode("ascii"))

    def _clear_expired(self, now: float) -> None:
        expired = [
            key
            for key, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for key in expired:
            del self._sessions[key]
