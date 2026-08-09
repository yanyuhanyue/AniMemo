from __future__ import annotations

import json
import os
import tempfile
from collections import deque
from pathlib import Path


class EventState:
    def __init__(self, path, *, max_delivered=1000):
        self.path = Path(path)
        self.max_delivered = max(500, int(max_delivered))
        self.cursor = 0
        self.delivered_event_ids = deque(maxlen=self.max_delivered)
        self.last_successful_poll = None
        self.last_error = ""
        self.load()

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.cursor = max(0, int(data.get("cursor", 0)))
            ids = data.get("delivered_event_ids", [])
            self.delivered_event_ids = deque((int(item) for item in ids if int(item) > 0), maxlen=self.max_delivered)
            self.last_successful_poll = data.get("last_successful_poll")
            self.last_error = str(data.get("last_error") or "")[:240]
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            self.cursor = 0
            self.delivered_event_ids.clear()
            self.last_successful_poll = None
            self.last_error = ""

    def has_delivered(self, event_id):
        return int(event_id) in self.delivered_event_ids

    def mark_delivered(self, event_id):
        event_id = int(event_id)
        if event_id not in self.delivered_event_ids:
            self.delivered_event_ids.append(event_id)

    def advance(self, cursor):
        self.cursor = max(self.cursor, int(cursor))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"cursor": self.cursor, "delivered_event_ids": list(self.delivered_event_ids), "last_successful_poll": self.last_successful_poll, "last_error": self.last_error}
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
