from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MessageIdentity:
    platform: str
    external_user_id: str
    display_name: str
    message_type: str
    umo: str

    @property
    def is_private(self):
        return self.message_type in {"private", "friend", "direct", "dm", "private_message", "friendmessage", "friend_message"}


def _value(event, name, default=""):
    candidate = getattr(event, name, None)
    if callable(candidate):
        try:
            return candidate()
        except TypeError:
            return default
    return candidate if candidate is not None else default


def extract_identity(event) -> MessageIdentity:
    platform = _value(event, "get_platform_id", "") or _value(event, "get_platform_name", "") or _value(event, "platform_name", "")
    if not platform:
        candidate = _value(event, "platform", "")
        platform = getattr(candidate, "id", None) or getattr(candidate, "name", None) or candidate
    sender_id = _value(event, "get_sender_id", "") or _value(event, "sender_id", "")
    sender_name = _value(event, "get_sender_name", "") or _value(event, "sender_name", "")
    umo = _value(event, "unified_msg_origin", "") or _value(event, "get_unified_msg_origin", "")
    message_type = _value(event, "message_type", "") or _value(event, "get_message_type", "")
    if hasattr(message_type, "value"):
        message_type = message_type.value
    if hasattr(message_type, "name"):
        message_type = message_type.name
    if not message_type:
        message_type = "private" if bool(_value(event, "is_private_chat", False)) else "group"
    return MessageIdentity(
        platform=str(platform or "unknown").strip().lower(),
        external_user_id=str(sender_id or "").strip(),
        display_name=str(sender_name or "").strip()[:160],
        message_type=str(message_type or "group").strip().lower(),
        umo=str(umo or "").strip(),
    )


def route_key(platform: str, external_user_id: str) -> str:
    return f"{str(platform).strip().lower()}:{str(external_user_id).strip()}"
