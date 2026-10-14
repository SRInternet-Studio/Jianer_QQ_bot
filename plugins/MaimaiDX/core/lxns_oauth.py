import re
from dataclasses import dataclass
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


