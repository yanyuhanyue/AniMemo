"""Single source of truth for the Plugin SDK v2 Hook contract."""

USER_SCOPED = "user"
SYSTEM_SCOPED = "system"

HOOK_DEFINITIONS = {
    "registration.before_request": {"mode": "action", "failure": "closed", "scope": SYSTEM_SCOPED},
    "registration.before_complete": {"mode": "action", "failure": "closed", "scope": SYSTEM_SCOPED},
    "registration.after_complete": {"mode": "action", "failure": "open", "scope": SYSTEM_SCOPED},
    "journal.after_create": {"mode": "action", "failure": "open", "scope": USER_SCOPED},
    "journal.after_update": {"mode": "action", "failure": "open", "scope": USER_SCOPED},
    "journal.after_delete": {"mode": "action", "failure": "open", "scope": USER_SCOPED},
    "column.after_publish": {"mode": "action", "failure": "open", "scope": USER_SCOPED},
    "column.after_delete": {"mode": "action", "failure": "open", "scope": USER_SCOPED},
    "user.after_created": {"mode": "action", "failure": "open", "scope": SYSTEM_SCOPED},
    "user.before_delete": {"mode": "filter", "failure": "closed", "scope": SYSTEM_SCOPED},
    "user.after_delete": {"mode": "action", "failure": "open", "scope": SYSTEM_SCOPED},
}

SUPPORTED_HOOKS = frozenset(HOOK_DEFINITIONS)
ACTION_HOOKS = frozenset(name for name, definition in HOOK_DEFINITIONS.items() if definition["mode"] == "action")
FILTER_HOOKS = frozenset(name for name, definition in HOOK_DEFINITIONS.items() if definition["mode"] == "filter")
CLOSED_HOOKS = frozenset(name for name, definition in HOOK_DEFINITIONS.items() if definition["failure"] == "closed")
USER_SCOPED_HOOKS = frozenset(name for name, definition in HOOK_DEFINITIONS.items() if definition["scope"] == USER_SCOPED)
SYSTEM_SCOPED_HOOKS = frozenset(name for name, definition in HOOK_DEFINITIONS.items() if definition["scope"] == SYSTEM_SCOPED)


def hook_scope(hook_name):
    try:
        return HOOK_DEFINITIONS[hook_name]["scope"]
    except KeyError as error:
        raise ValueError(f"unknown plugin hook: {hook_name}") from error


def resolve_hook_target_user_id(hook_name, context):
    """Resolve one authoritative user id for a USER-scoped business event."""

    if hook_name.startswith("journal."):
        return getattr(context, "user_id", None)
    if hook_name.startswith("column."):
        author_id = getattr(context, "author_id", None)
        if author_id is not None:
            return author_id
        column_id = getattr(context, "column_id", None)
        if column_id is None:
            return None
        from journal.models import Column

        return Column.objects.filter(pk=column_id).values_list("author_id", flat=True).first()
    return None
