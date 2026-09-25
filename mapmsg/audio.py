"""Keep this computer out of the phone's audio output list — for as long as the app runs.

PipeWire offers this computer to the phone as a Bluetooth speaker (A2DP sink), so iOS lists it as an
audio destination. Rather than changing the system's audio configuration, the app drops that one
audio link with BlueZ's DisconnectProfile whenever it is up, and puts it back when allowed again.
Nothing is written to disk: quit the app and the next connection behaves as before.
"""
import logging
import threading

import dbus

from .backend import BLUEZ, ObexError, find_phone

log = logging.getLogger("imsg.audio")

# The phone's "Audio Source" service — the one our A2DP sink role connects to.
A2DP_SOURCE = "0000110a-0000-1000-8000-00805f9b34fb"
POLL_SECONDS = 4.0


def _device_path(addr):
    return "/org/bluez/hci0/dev_" + addr.replace(":", "_")


def audio_connected(bus, addr):
    """True while BlueZ has an audio transport (an A2DP stream endpoint) open with the phone."""
    device = _device_path(addr)
    manager = dbus.Interface(bus.get_object(BLUEZ, "/"), "org.freedesktop.DBus.ObjectManager")
    return any(str(ifaces["org.bluez.MediaTransport1"].get("Device")) == device
               for ifaces in manager.GetManagedObjects().values() if "org.bluez.MediaTransport1" in ifaces)


def _profile_call(bus, addr, method):
    dev = dbus.Interface(bus.get_object(BLUEZ, _device_path(addr)), "org.bluez.Device1")
    getattr(dev, method)(A2DP_SOURCE)


def drop_audio(bus, addr):
    _profile_call(bus, addr, "DisconnectProfile")


def restore_audio(bus, addr):
    _profile_call(bus, addr, "ConnectProfile")


class AudioGuard(threading.Thread):
    """While `hide` is on, drops the phone's audio link whenever it appears."""

    def __init__(self, hide):
        super().__init__(daemon=True)
        self.hide = threading.Event()
        if hide:
            self.hide.set()
        self.wake = threading.Event()
        self.hidden_before = hide  # so the first pass doesn't "restore" something never dropped

    def set_hide(self, hide):
        (self.hide.set if hide else self.hide.clear)()
        self.wake.set()  # act now instead of waiting for the next poll

    def run(self):
        bus = dbus.SystemBus(private=True)
        addr = None
        while True:
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()
            try:
                addr = addr or find_phone()
                self.tick(bus, addr)
            except ObexError:
                addr = None  # no phone paired yet
            except dbus.DBusException as e:
                log.debug("audio: %s", e.get_dbus_message())

    def shutdown(self):
        """Leave things as we found them: if we were keeping the audio link down, allow it again."""
        if not self.hide.is_set():
            return
        try:
            bus = dbus.SystemBus(private=True)
            restore_audio(bus, find_phone())
            log.info("audio: quitting, phone audio link restored")
        except (dbus.DBusException, ObexError) as e:
            log.debug("audio: nothing to restore on quit: %s", e)

    def tick(self, bus, addr):
        hide = self.hide.is_set()
        if hide:
            if audio_connected(bus, addr):
                log.info("audio: dropping the phone's audio link so this computer isn't listed as an output")
                drop_audio(bus, addr)
        elif self.hidden_before:
            log.info("audio: audio allowed again, reconnecting")
            try:
                restore_audio(bus, addr)
            except dbus.DBusException as e:
                log.debug("audio: reconnect not needed or refused: %s", e.get_dbus_message())
        self.hidden_before = hide
