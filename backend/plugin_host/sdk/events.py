from threading import RLock

_listeners = {}
_lock = RLock()


def on_plugin_event(event_name, listener):
    if not str(event_name).startswith("plugin:"):
        raise ValueError("plugin events must use plugin:<slug>:<event> namespace")
    with _lock:
        _listeners.setdefault(event_name, set()).add(listener)
    return lambda: off_plugin_event(event_name, listener)


def off_plugin_event(event_name, listener):
    with _lock:
        _listeners.get(event_name, set()).discard(listener)


def emit_plugin_event(plugin_slug, event_name, payload=None):
    name = str(event_name or "")
    full_name = name if name.startswith("plugin:") else f"plugin:{plugin_slug}:{name}"
    if not full_name.startswith(f"plugin:{plugin_slug}:"):
        raise ValueError("plugin events must use the emitting plugin namespace")
    with _lock:
        listeners = tuple(_listeners.get(full_name, set()))
    for listener in listeners:
        listener(payload)
