"""The phone's battery level, from BlueZ's Battery1 interface (exact percent).

BlueZ only exposes Battery1 while the phone has a Bluetooth LE link up, because the level comes
from the phone's GATT battery service. The LE bearer often goes stale while classic Bluetooth
stays connected (BlueZ answers "Already Connected" yet resolves no services); cycling the bearer
brings it back.
"""
import logging
import time

import dbus

from .backend import BLUEZ

log = logging.getLogger("imsg.battery")


def _path(addr):
    return "/org/bluez/hci0/dev_" + addr.replace(":", "_")


def percent(bus, addr):
    """Battery percent if BlueZ has one right now, else None. `bus` is a system bus."""
    try:
        props = dbus.Interface(bus.get_object(BLUEZ, _path(addr)), "org.freedesktop.DBus.Properties")
        return int(props.Get("org.bluez.Battery1", "Percentage"))
    except dbus.DBusException:
        return None


def restore_le_link(bus, addr, wait=10):
    """Ask BlueZ to (re)establish the LE link, then wait for the battery to appear."""
    bearer = dbus.Interface(bus.get_object(BLUEZ, _path(addr)), "org.bluez.Bearer.LE1")

    def connect():
        try:
            bearer.Connect()
            return True
        except dbus.DBusException as e:
            if "Already Connected" in (e.get_dbus_message() or ""):
                return False
            raise

    try:
        if not connect():  # BlueZ thinks it's connected but resolved nothing: cycle the bearer
            log.info("battery: LE bearer looks stale, cycling it")
            bearer.Disconnect()
            time.sleep(2)
            connect()
    except dbus.DBusException as e:
        log.warning("battery: could not restore the LE link: %s", e.get_dbus_message() or e)
        return None
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        level = percent(bus, addr)
        if level is not None:
            return level
        time.sleep(1)
    log.warning("battery: LE link restored but the phone reported no battery level")
    return None


def read(bus, addr, reconnect=True):
    level = percent(bus, addr)
    if level is None and reconnect:
        level = restore_le_link(bus, addr)
    log.debug("battery: %s", level)
    return level
