from dataclasses import dataclass
from typing import Protocol

from .turnstile import verify_turnstile


@dataclass(frozen=True)
class AntiAbuseChallenge:
    provider: str
    token: str


class AntiAbuseChallengeAdapter(Protocol):
    provider: str

    def verify(self, challenge: AntiAbuseChallenge, *, remote_ip: str = "") -> bool: ...


class TurnstileChallengeAdapter:
    provider = "turnstile"

    def verify(self, challenge, *, remote_ip=""):
        return verify_turnstile(challenge.token, remote_ip=remote_ip)


_ADAPTERS = {
    TurnstileChallengeAdapter.provider: TurnstileChallengeAdapter(),
}
_DEFAULT_PROVIDER = TurnstileChallengeAdapter.provider


def challenge_from_payload(payload):
    if not hasattr(payload, "get"):
        return None

    if "challenge" in payload:
        value = payload.get("challenge")
        if not isinstance(value, dict):
            return None
        provider = str(value.get("provider") or "").strip().lower()
        token = str(value.get("token") or "").strip()
        if provider and token:
            return AntiAbuseChallenge(provider=provider, token=token)
        return None

    legacy_token = str(payload.get("cf-turnstile-response") or "").strip()
    if legacy_token:
        return AntiAbuseChallenge(provider="turnstile", token=legacy_token)
    return None


def verify_anti_abuse_challenge(challenge, *, remote_ip=""):
    if challenge is None:
        challenge = AntiAbuseChallenge(provider=_DEFAULT_PROVIDER, token="")
    adapter = _ADAPTERS.get(challenge.provider)
    if adapter is None:
        return False
    return bool(adapter.verify(challenge, remote_ip=remote_ip))
