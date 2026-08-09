class AniMemoBridgeError(Exception):
    """Base error exposed by the bridge without leaking sensitive details."""


class BridgeConfigError(AniMemoBridgeError):
    pass


class BridgeAuthError(AniMemoBridgeError):
    pass


class BridgeConnectionError(AniMemoBridgeError):
    pass


class BridgeRateLimitError(AniMemoBridgeError):
    pass


class BridgeProtocolError(AniMemoBridgeError):
    pass


class BridgePairingError(AniMemoBridgeError):
    pass


class PairingResultUnknown(BridgePairingError):
    pass


class BridgeActionError(AniMemoBridgeError):
    pass


class BridgeEventError(AniMemoBridgeError):
    pass
