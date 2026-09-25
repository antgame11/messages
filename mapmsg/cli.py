"""Send and receive iPhone messages over Bluetooth MAP, via BlueZ's obexd.

    imsg.py devices                      paired devices
    imsg.py list [-n 20] [-f inbox]      show recent messages
    imsg.py read ID                      print a full message body
    imsg.py send NUMBER-OR-NAME TEXT...  send an SMS
    imsg.py contacts [QUERY]             phone contacts (PBAP)
    imsg.py calls [-n 20]                call history: missed / received / dialed (PBAP)
    imsg.py battery                      the phone's battery level
    imsg.py watch                        print incoming messages as they arrive
    imsg.py shell                        interactive: watch + send in one session

Use -a AA:BB:CC:DD:EE:FF to pick the phone; otherwise the first paired
iPhone is used. The GTK app is imsg_gtk.py.
"""
import argparse
import select
import sys
import time

import dbus

from . import battery
from .backend import (Messages, Obex, ObexError, Phonebook, find_phone,
                      normalize, paired_devices, reset_link)

POLL_SECONDS = 0.7


def fmt(m, names):
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["time"]))
    who = names.get(normalize(m["address"])) or m["name"] or m["address"]
    arrow = "->" if m["outgoing"] else "  "
    flag = " " if m["read"] else "*"
    return f"{flag}[{m['id']}] {when} {arrow} {who} <{m['address']}>: {m['text']}"


def contact_names(contacts):
    return {normalize(n): c["name"] for c in contacts for _, n in c["numbers"]}


def load_contacts(obex, addr):
    try:
        return Phonebook(obex, addr).contacts()
    except ObexError as e:
        print(f"(contacts unavailable: {e})", file=sys.stderr)
        return []


def resolve(target, contacts):
    """A name like 'mom' -> her number; anything with digits passes through."""
    if any(ch.isdigit() for ch in target):
        return target
    hits = [c for c in contacts if target.lower() in c["name"].lower()]
    if len(hits) != 1:
        sys.exit(f"{len(hits)} contacts match {target!r}" +
                 "".join(f"\n  {c['name']}" for c in hits))
    return hits[0]["numbers"][0][1]


def shell_cmd(conn, line, names):
    parts = line.split(None, 2)
    if not parts:
        return True
    cmd = parts[0].lower()
    try:
        if cmd in ("quit", "exit", "q"):
            return False
        if cmd == "list":
            for m in conn.list("inbox", 15):
                print(fmt(m, names))
        elif cmd == "read" and len(parts) > 1:
            m = next((m for m in conn.list("inbox", 50) if m["id"] == parts[1]), None)
            print(conn.body(m["path"]) if m else "no such id")
        elif cmd == "send" and len(parts) == 3:
            conn.send(parts[1], parts[2])
            print("sent")
        else:
            print("usage: send NUMBER TEXT | list | read ID | quit")
    except ObexError as e:
        print(f"error: {e}")
    return True


def watch(conn, names, shell=False):
    seen = {m["path"] for m in conn.list("inbox", 50)}
    print("commands: send NUMBER TEXT | list | read ID | quit. Incoming messages print live."
          if shell else "watching inbox (Ctrl-C to stop)")
    while True:
        if shell:
            print("> ", end="", flush=True)
            ready, _, _ = select.select([sys.stdin], [], [], POLL_SECONDS)
            if ready:
                line = sys.stdin.readline()
                if not line or not shell_cmd(conn, line.strip(), names):
                    return
                seen |= {m["path"] for m in conn.list("inbox", 50)}
                continue
            print("\r", end="")
        else:
            time.sleep(POLL_SECONDS)
        for m in conn.list("inbox", 50):
            if m["path"] not in seen:
                seen.add(m["path"])
                print(f"\r{fmt(m, names)}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-a", "--addr")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices")
    ls = sub.add_parser("list")
    ls.add_argument("-n", type=int, default=20)
    ls.add_argument("-f", "--folder", default="inbox",
                    choices=["inbox", "sent", "outbox", "draft", "deleted"])
    sub.add_parser("read").add_argument("id")
    sd = sub.add_parser("send")
    sd.add_argument("target")
    sd.add_argument("text", nargs="+")
    sub.add_parser("contacts").add_argument("query", nargs="?", default="")
    calls = sub.add_parser("calls")
    calls.add_argument("-n", type=int, default=20)
    sub.add_parser("battery")
    sub.add_parser("watch")
    sub.add_parser("shell")
    args = p.parse_args()

    if args.cmd == "devices":
        for a, name, connected in paired_devices():
            print(f"{a}  {name}{'  (connected)' if connected else ''}")
        return

    if args.cmd == "battery":
        level = battery.read(dbus.SystemBus(private=True), find_phone(args.addr))
        if level is None:
            sys.exit("the phone does not report a battery level")
        print(f"{level}%")
        return

    obex = Obex()
    try:
        addr = find_phone(args.addr)
        if args.cmd == "calls":
            book = Phonebook(obex, addr)
            names = contact_names(book.contacts())
            for c in book.call_history()[:args.n]:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(c["call"]["time"]))
                number = c["numbers"][0][1]
                who = names.get(normalize(number)) or c["name"]
                print(f"{when}  {c['call']['type']:9} {who}  <{number}>")
            return
        if args.cmd == "contacts":
            for c in Phonebook(obex, addr).contacts():
                if args.query.lower() in c["name"].lower():
                    print(f"{c['name']}: " + ", ".join(
                        f"{n}{f' ({l})' if l else ''}" for l, n in c["numbers"]))
            return
        contacts = load_contacts(obex, addr) if args.cmd in ("send", "list", "watch", "shell") else []
        names = contact_names(contacts)
        try:
            conn = Messages(obex, addr)
        except ObexError as e:
            if "refused" not in str(e) and "Internal Server Error" not in str(e):
                raise
            print("phone still holds an old session; resetting the Bluetooth link…", file=sys.stderr)
            reset_link(addr)
            conn = Messages(obex, addr)
        if args.cmd == "list":
            for m in conn.list(args.folder, args.n):
                print(fmt(m, names))
        elif args.cmd == "read":
            m = next((m for m in conn.list("inbox", 100) if m["id"] == args.id), None)
            sys.exit("no such id in inbox") if not m else print(conn.body(m["path"]))
        elif args.cmd == "send":
            conn.send(resolve(args.target, contacts), " ".join(args.text))
            print("sent")
        elif args.cmd == "watch":
            watch(conn, names)
        elif args.cmd == "shell":
            watch(conn, names, shell=True)
    except ObexError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        pass
    finally:
        obex.close()

