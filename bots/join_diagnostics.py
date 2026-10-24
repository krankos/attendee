"""Low-volume, correlated join diagnostics; no names, URLs, or media payloads."""

import hashlib
import json
import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


def meeting_key(meeting_url):
    url = urlsplit(meeting_url)
    # Ignore query credentials and presentation differences for the same room.
    room = f"{(url.hostname or '').lower()}{url.path.rstrip('/')}"
    return hashlib.sha256(room.encode()).hexdigest()[:24]


def bot_log_context(bot):
    metadata = bot.metadata if isinstance(bot.metadata, dict) else {}
    return {
        "bot_id": bot.id,
        "provider_bot_id": bot.object_id,
        "meeting_key": meeting_key(bot.meeting_url),
        **{key: metadata[key] for key in ("meeting_bot_id", "consultant_request_id") if isinstance(metadata.get(key), str)},
    }


def log_diagnostic(event, context, *, level=logging.INFO, **details):
    logger.log(level, "BOT_DIAGNOSTIC %s", json.dumps({**context, **details, "event": event, "at": datetime.now(timezone.utc).isoformat()}, separators=(",", ":")))


class JoinDiagnostics:
    def __init__(self, meeting_url, display_name):
        self.context = {
            "run_id": str(uuid.uuid4()),
            "launch_id": os.getenv("BOT_DIAGNOSTIC_LAUNCH_ID"),
            "launcher_task_id": os.getenv("BOT_DIAGNOSTIC_TASK_ID"),
            "container": socket.gethostname(),
            "pid": os.getpid(),
            "meeting_key": meeting_key(meeting_url),
        }
        self.display_name = display_name.strip().casefold()
        self.attempt = 0
        self.started = time.monotonic()
        self.attempt_started = self.started
        self.participants = {}
        self.self_joins = 0
        self._lock = threading.RLock()
        self._duplicate_ids = ()

    def emit(self, event, *, level=logging.INFO, **details):
        log_diagnostic(event, self.context, level=level, attempt=self.attempt, elapsed_ms=round((time.monotonic() - self.started) * 1000), **details)

    def begin_attempt(self, **details):
        with self._lock:
            self.attempt += 1
            self.attempt_started = time.monotonic()
            active_self = any(p.get("self") and p.get("active") for p in self.participants.values())
            self.emit("join_attempt_started", level=logging.WARNING if active_self else logging.INFO, self_already_in_meeting=active_self, **details)

    def failed(self, exception):
        # Exception text can contain a URL/password, so keep class and UI step only.
        self.emit("join_attempt_failed", level=logging.WARNING, exception_class=type(exception).__name__, step=getattr(exception, "step", None), attempt_elapsed_ms=round((time.monotonic() - self.attempt_started) * 1000))

    def participant_update(self, user):
        with self._lock:
            identity = user.get("deviceId")
            if not identity:
                return
            previous = self.participants.get(identity, {})
            current = dict(previous)
            if "fullName" in user:
                name = user["fullName"]
                current["assistant"] = isinstance(name, str) and name.strip().casefold() == self.display_name
            if "isCurrentUser" in user:
                current["self"] = bool(user["isCurrentUser"])
            if "active" in user:
                current["active"] = bool(user["active"])
            # Track only assistants and this process's own identity.
            if not (current.get("assistant") or current.get("self") or previous):
                return
            self.participants[identity] = current
            if current == previous:
                return
            # Device IDs identify sessions without exposing participant names.
            device_key = hashlib.sha256(identity.encode()).hexdigest()[:16]
            if current.get("self") and current.get("active") != previous.get("active", False):
                if current.get("active"):
                    self.self_joins += 1
                self.emit("self_joined" if current.get("active") else "self_left", device_key=device_key, self_join_count=self.self_joins)
            elif current.get("assistant") and not current.get("self"):
                self.emit("other_assistant_observed", level=logging.WARNING if current.get("active") else logging.INFO, device_key=device_key, active=bool(current.get("active")))

            active_ids = tuple(sorted(key for key, value in self.participants.items() if value.get("active") and (value.get("assistant") or value.get("self"))))
            if len(active_ids) > 1 and active_ids != self._duplicate_ids:
                self.emit("duplicate_assistant_detected", level=logging.WARNING, assistant_count=len(active_ids), device_keys=[hashlib.sha256(key.encode()).hexdigest()[:16] for key in active_ids])
            elif len(active_ids) <= 1 and self._duplicate_ids:
                self.emit("duplicate_assistant_cleared", assistant_count=len(active_ids))
            self._duplicate_ids = active_ids if len(active_ids) > 1 else ()
