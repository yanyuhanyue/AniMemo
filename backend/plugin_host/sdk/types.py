from dataclasses import dataclass


@dataclass(frozen=True)
class RegistrationRequestContext:
    email: str
    ip_address: str | None = None


@dataclass(frozen=True)
class RegistrationCompleteContext:
    email: str
    username: str
    user_id: int | None = None


@dataclass(frozen=True)
class JournalHookContext:
    user_id: int
    journal_entry_id: int
    source: str = "core"


@dataclass(frozen=True)
class ColumnHookContext:
    column_id: int
    actor_id: int | None = None
    source: str = "core"
    author_id: int | None = None


@dataclass(frozen=True)
class UserHookContext:
    user_id: int
    actor_id: int | None = None
    source: str = "core"
