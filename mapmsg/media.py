"""Now-playing info and transport controls for the phone's music, over AVRCP.

BlueZ exposes the phone's media player as org.bluez.MediaPlayer1 (Status, Track, and methods like
Play/Pause/Next/Previous) whenever the phone is connected and something has played. Nothing here
touches the radio: it reads and calls that D-Bus object. No GTK; callbacks run on the monitor thread.
"""
import logging
import queue
import threading
import time

import dbus

from .backend import BLUEZ, ObexError, find_phone

log = logging.getLogger("imsg.media")

POLL_SECONDS = 2.0
COMMANDS = ("Play", "Pause", "Next", "Previous")


def find_player(bus, addr):
    """Path of the phone's MediaPlayer1 object, or None if it has none right now."""
    device = "/org/bluez/hci0/dev_" + addr.replace(":", "_")
    manager = dbus.Interface(bus.get_object(BLUEZ, "/"), "org.freedesktop.DBus.ObjectManager")
    for path, ifaces in manager.GetManagedObjects().items():
        player = ifaces.get("org.bluez.MediaPlayer1")
        if player and str(player.get("Device")) == device:
            return str(path)
    return None


def read_player(bus, path):
    """{"app", "status", "title", "artist", "album", "duration"} or None when nothing is queued."""
    props = dbus.Interface(bus.get_object(BLUEZ, path), "org.freedesktop.DBus.Properties")
    values = props.GetAll("org.bluez.MediaPlayer1")
    track = values.get("Track", {})
    info = {"app": str(values.get("Name", "")), "status": str(values.get("Status", "stopped")),
            "title": str(track.get("Title", "")), "artist": str(track.get("Artist", "")),
            "album": str(track.get("Album", "")), "duration": int(track.get("Duration", 0))}
    if not info["title"] and info["status"] in ("stopped", "paused"):
        return None  # a player with nothing loaded is not worth a bar
    return info


class MediaMonitor(threading.Thread):
    """Polls the player every couple of seconds and runs button presses.

    on_info(info or None) fires when what is playing changes; on_error(text) when the phone
    rejects a command (it does when no music app is active). While disabled the thread just
    sleeps: it reads nothing, sends nothing, and reports "nothing playing" once."""

    def __init__(self, on_info, on_error, enabled=True):
        super().__init__(daemon=True)
        self.on_info, self.on_error = on_info, on_error
        self.commands = queue.Queue()
        self.enabled = threading.Event()
        if enabled:
            self.enabled.set()

    def set_enabled(self, on):
        (self.enabled.set if on else self.enabled.clear)()

    def send(self, command):
        if command in COMMANDS and self.enabled.is_set():
            self.commands.put(command)

    def run(self):
        bus = dbus.SystemBus(private=True)
        addr = path = None
        last = object()  # nothing sent yet, so the first poll always reports
        while True:
            if not self.enabled.is_set():
                if last is not None:
                    last = None
                    self.on_info(None)  # hide whatever was showing
                self.enabled.wait()     # asleep until turned on again
                last, path = object(), None
                continue
            info = None
            try:
                addr = addr or find_phone()
                path = path or find_player(bus, addr)
                info = read_player(bus, path) if path else None
            except dbus.DBusException as e:
                if path:
                    log.info("media: player went away (%s)", e.get_dbus_message())
                path = None  # the phone disconnected or the app closed; look again next time
            except ObexError:
                addr = None
            if info != last:
                log.debug("media: %s", info)
                last = info
                self.on_info(info)
            try:
                command = self.commands.get(timeout=POLL_SECONDS)
            except queue.Empty:
                continue
            try:
                if not path:
                    raise dbus.DBusException("no player")
                getattr(dbus.Interface(bus.get_object(BLUEZ, path), "org.bluez.MediaPlayer1"), command)()
                log.info("media: sent %s", command)
            except dbus.DBusException as e:
                log.warning("media: %s failed: %s", command, e.get_dbus_message())
                self.on_error(e.get_dbus_message() or "no music app is active")
            time.sleep(0.4)  # give the phone a moment to change state, then poll again
