"""Single source of truth for the Plugin SDK v2 Hook contract."""

HOOK_DEFINITIONS = {
    "registration.before_request": {"mode": "action", "failure": "closed"},
    "registration.before_complete": {"mode": "action", "failure": "closed"},
    "registration.after_complete": {"mode": "action", "failure": "open"},
    "journal.after_create": {"mode": "action", "failure": "open"},
    "journal.after_update": {"mode": "action", "failure": "open"},
    "journal.after_delete": {"mode": "action", "failure": "open"},
    "column.after_publish": {"mode": "action", "failure": "open"},
    "column.after_delete": {"mode": "action", "failure": "open"},
    "user.after_created": {"mode": "action", "failure": "open"},
    "user.before_delete": {"mode": "filter", "failure": "closed"},
    "user.after_delete": {"mode": "action", "failure": "open"},
}

SUPPORTED_HOOKS = frozenset(HOOK_DEFINITIONS)
ACTION_HOOKS = frozenset(name for name, definition in HOOK_DEFINITIONS.items() if definition["mode"] == "action")
FILTER_HOOKS = frozenset(name for name, definition in HOOK_DEFINITIONS.items() if definition["mode"] == "filter")
CLOSED_HOOKS = frozenset(name for name, definition in HOOK_DEFINITIONS.items() if definition["failure"] == "closed")

