"""User settings, saved as JSON in the XDG config directory (~/.config/imsg/settings.json)."""
import json
import logging
import os

log = logging.getLogger("imsg.settings")

DEFAULTS = {
    "notifications": True,       # desktop notification for each new message
    "hide_to_tray": True,        # closing the window keeps the app running in the tray
    "mark_read_on_phone": True,  # opening a conversation here also marks its messages read on the iPhone
    "show_battery": True,        # phone battery level in the sidebar header
    "low_battery_alert": True,   # notify when the phone drops to 20% and again at 10%
    "quiet_hours": False,        # no desktop notifications between the two hours below
    "quiet_start": 22,           # hour of day (0-23)
    "quiet_end": 7,
    "allow_phone_audio": False,  # False: keep this computer out of the phone's audio output list
    "media_controls": False,     # now-playing bar (AVRCP); off by default, the code stays available
}


def default_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "imsg", "settings.json")


class Settings:
    def __init__(self, path=None):
        self.path = path or default_path()
        self.values = dict(DEFAULTS)
        self.muted = set()  # conversation keys whose notifications are off
        self.listeners = []
        try:
            with open(self.path) as f:
                saved = json.load(f)
            self.values.update({k: v for k, v in saved.items()
                                if k in DEFAULTS and type(v) is type(DEFAULTS[k])})
            self.muted = {k for k in saved.get("muted_chats", []) if isinstance(k, str)}
        except (OSError, ValueError, AttributeError):
            pass  # first run, or an unreadable file: use the defaults

    def get(self, name):
        return self.values[name]

    def set(self, name, value):
        if name not in DEFAULTS or type(value) is not type(DEFAULTS[name]) or self.values[name] == value:
            return
        self.values[name] = value
        self._save()
        log.info("settings: %s = %s", name, value)
        for listener in self.listeners:
            listener(name, value)

    def is_muted(self, key):
        return key in self.muted

    def set_muted(self, key, muted):
        if (key in self.muted) == muted:
            return
        (self.muted.add if muted else self.muted.discard)(key)
        self._save()
        for listener in self.listeners:
            listener("muted_chats", sorted(self.muted))

    def subscribe(self, listener):
        """listener(name, value) is called after every change."""
        self.listeners.append(listener)

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(dict(self.values, muted_chats=sorted(self.muted)), f, indent=2)
        except OSError as e:
            log.error("settings: could not save %s: %s", self.path, e)
