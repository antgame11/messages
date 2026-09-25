"""BlueZ obexd client for a paired phone: MAP (messages) and PBAP (contacts).

Everything here is blocking D-Bus. Own an Obex() from a single thread.
"""
import base64
import binascii
import logging
import os
import re
import tempfile
import time

import dbus

BLUEZ = "org.bluez"
OBEX = "org.bluez.obex"
FIELDS = ["subject", "timestamp", "sender", "sender-address", "recipient",
          "recipient-address", "type", "size", "status", "read", "sent"]
OUTGOING = ("sent", "outbox", "draft")
log = logging.getLogger("imsg.backend")


class ObexError(Exception):
    pass


def paired_devices():
    """[(address, name, connected)] for every paired Bluetooth device."""
    system = dbus.SystemBus(private=True)
    om = dbus.Interface(system.get_object(BLUEZ, "/"),
                        "org.freedesktop.DBus.ObjectManager")
    out = []
    for ifaces in om.GetManagedObjects().values():
        dev = ifaces.get("org.bluez.Device1")
        if dev and dev.get("Paired"):
            out.append((str(dev["Address"]), str(dev.get("Name", "?")),
                        bool(dev.get("Connected"))))
    system.close()
    return out


def find_phone(addr=None):
    if addr:
        return addr
    devs = paired_devices()
    for a, name, _ in devs:
        if "iphone" in name.lower():
            return a
    if len(devs) == 1:
        return devs[0][0]
    raise ObexError("Could not pick a phone automatically; pass an address.")


def reset_link(addr):
    """Drop the ACL link; clears a stale MAP connection the phone still holds."""
    system = dbus.SystemBus(private=True)
    try:
        dev = system.get_object(BLUEZ, "/org/bluez/hci0/dev_" + addr.replace(":", "_"))
        dbus.Interface(dev, "org.bluez.Device1").Disconnect()
    except dbus.DBusException:
        pass
    finally:
        system.close()
    time.sleep(2)


def normalize(number):
    """Digits only, last 10 — so +1 (919) 586-2066 and 9195862066 match."""
    digits = re.sub(r"\D", "", number or "")
    return digits[-10:] if len(digits) >= 10 else digits


def parse_timestamp(ts):
    try:
        return time.mktime(time.strptime(ts[:15], "%Y%m%dT%H%M%S"))
    except (ValueError, TypeError):
        return 0.0


def parse_vcards(text):
    """-> [{"name": str, "numbers": [(label, number)], "photo": bytes | None,
    "call": {"type": "missed"|"received"|"dialed", "time": epoch} (call-history entries only)}]"""
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold continuation lines
    contacts = []
    for block in re.findall(r"BEGIN:VCARD(.*?)END:VCARD", text, re.S):
        name, structured, numbers, photo, call = "", "", [], None, None
        for line in block.splitlines():
            key, _, val = line.partition(":")
            params = key.split(";")
            prop = params[0].split(".")[-1].upper()
            if prop == "FN":
                name = val.strip()
            elif prop == "N":
                parts = val.split(";")
                structured = " ".join(p.strip() for p in (parts[1:2] + parts[:1]) if p.strip())
            elif prop == "TEL" and val.strip():
                label = next((p.split("=")[-1].lower() for p in params[1:]
                              if p.upper().startswith("TYPE=") and
                              p.split("=")[-1].upper() not in ("VOICE", "PREF")), "")
                numbers.append((label, val.strip()))
            elif prop == "X-IRMC-CALL-DATETIME":
                call = {"type": params[1].lower() if len(params) > 1 else "unknown",
                        "time": parse_timestamp(val.strip())}
            elif prop == "PHOTO" and "BASE64" in key.upper().replace("ENCODING=B", "BASE64"):
                try:
                    photo = base64.b64decode(val.strip()) or None
                except (binascii.Error, ValueError):
                    pass
        name = name or structured
        if numbers:
            entry = {"name": name or numbers[0][1], "numbers": numbers, "photo": photo}
            if call:
                entry["call"] = call
            contacts.append(entry)
    return contacts


class Obex:
    def __init__(self, heal=True):
        """heal: drop the Bluetooth link when the phone refuses a session. A helper session that
        must not disturb the main one (contacts alongside messages) passes heal=False."""
        self.heal = heal
        self.bus = dbus.SessionBus(private=True)
        self.client = dbus.Interface(self.bus.get_object(OBEX, "/org/bluez/obex"),
                                     "org.bluez.obex.Client1")
        self.sessions = []

    def open(self, addr, target):
        """Open a MAP/PBAP session. If the phone refuses because it still holds an old session
        (or answers "Internal Server Error"), drop the Bluetooth link once and try again."""
        err = None
        for round_ in range(2):
            for attempt in range(3):  # iOS sometimes answers "Internal Server Error" once
                try:
                    path = self.client.CreateSession(addr, {"Target": target})
                    self.sessions.append(path)
                    log.debug("obex: %s session %s opened (attempt %d)", target, path, attempt + 1)
                    return path
                except dbus.DBusException as e:
                    err = e.get_dbus_message() or str(e)
                    log.warning("obex: %s session attempt %d failed: %s", target, attempt + 1, err)
                    if "0x43" in err or "Unable to find" in err:  # permission not granted on the phone
                        raise ObexError(f"{target.upper()} connect failed: {err}")
                    time.sleep(1.5)
            if round_ == 0 and self.heal and ("refused" in err or "Internal Server Error" in err):
                log.warning("obex: resetting the Bluetooth link to clear the phone's stale session")
                reset_link(addr)
            else:
                break
        raise ObexError(f"{target.upper()} connect failed: {err}")

    def proxy(self, path, iface):
        return dbus.Interface(self.bus.get_object(OBEX, path), iface)

    def wait(self, xfer, timeout=60):
        props = self.proxy(xfer, "org.freedesktop.DBus.Properties")
        started = time.monotonic()
        end, last = time.time() + timeout, None
        while time.time() < end:
            try:
                status = str(props.Get("org.bluez.obex.Transfer1", "Status"))
            except dbus.DBusException:  # object is removed once the transfer finishes
                log.debug("obex: transfer finished after %.0f ms", (time.monotonic() - started) * 1000)
                return
            if status != last:
                log.debug("obex: transfer %s after %.0f ms", status, (time.monotonic() - started) * 1000)
                last = status
            if status == "complete":
                return
            if status == "error":
                raise ObexError("transfer failed")
            time.sleep(0.1)
        raise ObexError("transfer timed out")

    def download(self, start, timeout=60):
        """start(tmpfile) -> (transfer_path, props); returns the file's text."""
        tmp = tempfile.mktemp(suffix=".obex")
        try:
            xfer, _ = start(tmp)
            self.wait(xfer, timeout)
            with open(tmp, encoding="utf-8", errors="replace") as f:
                return f.read()
        except (dbus.DBusException, OSError) as e:
            raise ObexError(str(e))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def close(self):
        for path in self.sessions:
            try:
                self.client.RemoveSession(path)
            except dbus.DBusException:
                pass
        self.sessions = []
        self.bus.close()


class Messages:
    def __init__(self, obex, addr):
        self.obex = obex
        self.path = obex.open(addr, "map")
        self.map = obex.proxy(self.path, "org.bluez.obex.MessageAccess1")
        self._folder("/telecom/msg")

    def _folder(self, path):
        try:
            self.map.SetFolder(path)
        except dbus.DBusException as e:
            raise ObexError(f"SetFolder {path}: {e.get_dbus_message()}")

    def list(self, folder="inbox", count=50):
        try:
            raw = self.map.ListMessages(
                folder, {"MaxCount": dbus.UInt16(count), "SubjectLength": dbus.Byte(255),
                         "Fields": dbus.Array(FIELDS, signature="s")})
        except dbus.DBusException as e:
            raise ObexError(f"ListMessages {folder}: {e.get_dbus_message()}")
        msgs = []
        for path, props in raw.items():
            p = {str(k): (bool(v) if isinstance(v, dbus.Boolean) else str(v))
                 for k, v in props.items()}
            outgoing = folder in OUTGOING
            addr = p.get("RecipientAddress" if outgoing else "SenderAddress", "")
            msgs.append({
                "id": str(path).rsplit("message", 1)[-1],
                "path": str(path),
                "folder": folder,
                "outgoing": outgoing,
                "address": addr,
                "name": p.get("Recipient" if outgoing else "Sender", ""),
                "text": p.get("Subject", "").replace("\ufffc", "").strip() or "📎 Attachment",
                "time": parse_timestamp(p.get("Timestamp", "")),
                "read": p.get("Read", True),
                "type": p.get("Type", ""),
                "size": int(p["Size"]) if p.get("Size", "").isdigit() else 0,  # full length; text is cut at ~255 bytes
            })
        msgs.sort(key=lambda m: m["time"])
        return msgs

    def body(self, msg_path):
        msg = self.obex.proxy(msg_path, "org.bluez.obex.Message1")
        raw = self.obex.download(lambda tmp: msg.Get(tmp, False))
        start, end = raw.find("BEGIN:MSG"), raw.rfind("END:MSG")
        if start == -1 or end == -1:
            return raw
        return raw[start + len("BEGIN:MSG"):end].strip()

    def set_read(self, paths):
        """Mark messages read on the phone (MAP SetMessageStatus). Returns how many succeeded."""
        done = 0
        for path in paths:
            try:
                props = self.obex.proxy(path, "org.freedesktop.DBus.Properties")
                props.Set("org.bluez.obex.Message1", "Read", dbus.Boolean(True))
                done += 1
            except dbus.DBusException as e:
                log.warning("obex: could not mark %s read: %s", path, e.get_dbus_message())
        return done

    def send(self, number, text):
        body = f"BEGIN:MSG\r\n{text}\r\nEND:MSG\r\n"
        bmsg = (
            "BEGIN:BMSG\r\nVERSION:1.0\r\nSTATUS:UNREAD\r\nTYPE:SMS_GSM\r\n"
            "FOLDER:telecom/msg/outbox\r\n"
            "BEGIN:VCARD\r\nVERSION:2.1\r\nN:\r\nTEL:\r\nEND:VCARD\r\n"
            "BEGIN:BENV\r\n"
            f"BEGIN:VCARD\r\nVERSION:2.1\r\nN:\r\nTEL:{number}\r\nEND:VCARD\r\n"
            "BEGIN:BBODY\r\nCHARSET:UTF-8\r\n"
            f"LENGTH:{len(body.encode('utf-8'))}\r\n"
            f"{body}"
            "END:BBODY\r\nEND:BENV\r\nEND:BMSG\r\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".bmsg", delete=False,
                                         newline="", encoding="utf-8") as f:
            f.write(bmsg)
        laps, last = {}, time.monotonic()

        def lap(name):
            nonlocal last
            now = time.monotonic()
            laps[name] = (now - last) * 1000
            last = now
        try:
            # obexd pushes into the session's current folder and rejects a folder arg
            self._folder("/telecom/msg/outbox")
            lap("open outbox")
            xfer, _ = self.map.PushMessage(f.name, "", {})
            lap("push accepted")
            self.obex.wait(xfer, 30)
            lap("phone finished")
        except dbus.DBusException as e:
            raise ObexError(e.get_dbus_message() or str(e))
        finally:
            os.unlink(f.name)
            try:
                self._folder("/telecom/msg")
                lap("folder back")
            except ObexError:
                pass
            log.info("send timing: %s", ", ".join(f"{k} {v:.0f} ms" for k, v in laps.items()))


class Phonebook:
    def __init__(self, obex, addr):
        self.obex = obex
        self.path = obex.open(addr, "pbap")
        self.pbap = obex.proxy(self.path, "org.bluez.obex.PhonebookAccess1")

    def contacts(self, photos=True):
        """All contacts. photos=False asks the phone to leave photos out (a few KB instead of ~1 MB);
        a phone that ignores that request simply sends everything."""
        flt = {"Format": "vcard30", "MaxCount": dbus.UInt16(65535)}
        if not photos:
            flt["Fields"] = dbus.Array(["VERSION", "FN", "N", "TEL"], signature="s")
        try:
            self.pbap.Select("int", "pb")
            text = self.obex.download(lambda tmp: self.pbap.PullAll(tmp, flt), timeout=120)
        except dbus.DBusException as e:
            raise ObexError(f"phonebook: {e.get_dbus_message()}")
        return parse_vcards(text)

    def call_history(self):
        """Combined call history, newest first. Each entry has a "call" dict (type, time)."""
        try:
            self.pbap.Select("int", "cch")
            text = self.obex.download(
                lambda tmp: self.pbap.PullAll(tmp, {"Format": "vcard30", "MaxCount": dbus.UInt16(65535)}), 60)
        except dbus.DBusException as e:
            raise ObexError(f"call history: {e.get_dbus_message()}")
        calls = [c for c in parse_vcards(text) if "call" in c]
        calls.sort(key=lambda c: c["call"]["time"], reverse=True)
        log.debug("pbap: %d call history entries", len(calls))
        return calls
