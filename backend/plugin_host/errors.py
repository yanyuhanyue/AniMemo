from dataclasses import dataclass


@dataclass(frozen=True)
class HostCapabilityError(ValueError):
    """Stable SDK error for a denied or invalid Host capability call."""

    code: str
    detail: object
    status_code: int = 400

    def __str__(self):
        return str(self.detail)
