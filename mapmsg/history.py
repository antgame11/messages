"""On-disk message history.

The iPhone only lists its 10 newest inbox messages and never lists sent ones, so every message
the app has seen or sent is kept here and shown alongside what the phone reports.

MAP message handles are not persistent across connections, so a message is identified by its
content (direction, number, timestamp, text) rather than by the phone's id.
"""
import collections
import hashlib
import json
import logging
import os
import time

from .backend import normalize

log = logging.getLogger("imsg.history")

MAX_MESSAGES = 5000
MAX_TOMBSTONES = 2000
LOCAL_MATCH_SECONDS = 300  # a phone-listed sent message this close to one we sent is the same one
NEAR_SECONDS = 1.5  # the phone reports the same message's timestamp a second apart between listings
FIELDS = ("outgoing", "address", "name", "text", "time", "read", "folder", "failed", "local", "full")


def content_key(m, shift=0):
    """shift moves the timestamp by whole seconds, to look up the same message reported a moment off."""
    who = normalize(m["address"]) or m["address"].lower()
    return f"{'o' if m['outgoing'] else 'i'}|{who}|{int(m['time']) + shift}|{m['text']}"


def same_text_key(m):
    """Direction, number and text — what stays the same when the phone's timestamp wobbles."""
    return (m["outgoing"], normalize(m["address"]) or m["address"].lower(), m["text"])


class History:
    def __init__(self, path):
        self.path = path
        self.items = {}      # content key -> message dict
        self.deleted = {}    # content key -> time of the deleted message; keeps it from coming back
        self.deleted_sent = []  # [who, text, time] of deleted app-sent messages (the phone may list them later)
        self.by_text = collections.defaultdict(list)  # same_text_key -> content keys, for near-duplicate lookup
        self.dirty = False
        self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(raw, list):  # older format: just the messages
            raw = {"messages": raw}
        for m in raw.get("messages", []):
            if m.pop("pending", False):  # the app closed mid-send, so we can't know it reached the phone
                m["failed"] = True
            m["id"] = self._id(content_key(m))
            self._put(content_key(m), m)
        self.deleted = {k: t for k, t in raw.get("deleted", [])}
        self.deleted_sent = raw.get("deleted_sent", [])
        self._collapse_near_duplicates()
        log.info("history: loaded %d messages (%d deleted markers) from %s", len(self.items),
                 len(self.deleted), self.path)

    # the index keeps near-duplicate lookups cheap: every insert and removal goes through these
    def _put(self, key, m):
        if key not in self.items:
            self.by_text[same_text_key(m)].append(key)
        self.items[key] = m

    def _remove(self, key):
        m = self.items.pop(key, None)
        if m is not None:
            keys = self.by_text.get(same_text_key(m), [])
            if key in keys:
                keys.remove(key)
        return m

    def _near(self, m):
        """An existing entry with the same direction, number and text within NEAR_SECONDS."""
        for key in self.by_text.get(same_text_key(m), ()):
            other = self.items[key]
            if abs(other["time"] - m["time"]) <= NEAR_SECONDS and not other.get("local"):
                return other
        return None

    def _collapse_near_duplicates(self):
        """Earlier versions stored a message twice when the phone's timestamp shifted by a second."""
        removed = 0
        for keys in list(self.by_text.values()):
            if len(keys) < 2:
                continue
            kept = None
            for key in sorted(keys, key=lambda k: self.items[k]["time"]):
                m = self.items[key]
                if kept and not m.get("local") and not kept.get("local") and m["time"] - kept["time"] <= NEAR_SECONDS:
                    kept["read"] = kept["read"] or m["read"]
                    self._remove(key)
                    removed += 1
                else:
                    kept = m
        if removed:
            self.dirty = True
            log.info("history: merged %d duplicate message(s) whose timestamps differed by a second", removed)

    @staticmethod
    def _id(key):
        return "h" + hashlib.sha1(key.encode()).hexdigest()[:12]

    def values(self):
        return self.items.values()

    def merge_phone(self, msgs):
        """Record what the phone lists; returns the messages we had not seen before."""
        fresh = []
        for m in msgs:
            key = content_key(m)
            if self._is_deleted(key, m):
                continue
            known = self.items.get(key) or self._near(m)
            if known:
                if m["read"] and not known["read"]:
                    known["read"], self.dirty = True, True
                continue
            entry = {f: m[f] for f in FIELDS if f in m}
            entry["id"] = self._id(key)
            self._put(key, entry)
            fresh.append(entry)
            if entry["outgoing"]:
                self._drop_local_copy(entry)
        if fresh:
            self.dirty = True
        return fresh

    def _is_deleted(self, key, m):
        if key in self.deleted:
            return True
        if any(content_key(m, d) in self.deleted for d in (-1, 1)):  # same message, timestamp a second off
            return True
        who = normalize(m["address"]) or m["address"].lower()
        return m["outgoing"] and any(w == who and t == m["text"] and abs(tm - m["time"]) < LOCAL_MATCH_SECONDS
                                     for w, t, tm in self.deleted_sent)

    def find(self, msg_id):
        return next((m for m in self.items.values() if m["id"] == msg_id), None)

    def delete(self, msgs):
        """Remove messages for good on this computer. The phone still lists its 10 newest on every
        poll, so each one leaves a marker that stops it from reappearing. Returns them for undo."""
        removed = []
        for m in msgs:
            key = content_key(m)
            if self.items.pop(key, None) is None:
                continue
            removed.append(m)
            self.deleted[key] = m["time"]
            if m["outgoing"]:
                self.deleted_sent.append([normalize(m["address"]) or m["address"].lower(), m["text"], m["time"]])
        if removed:
            self.dirty = True
        return removed

    def restore(self, msgs):
        """Undo a delete."""
        for m in msgs:
            key = content_key(m)
            self.items[key] = m
            self.deleted.pop(key, None)
            who = normalize(m["address"]) or m["address"].lower()
            self.deleted_sent = [d for d in self.deleted_sent if not (d[0] == who and d[1] == m["text"] and d[2] == m["time"])]
        self.dirty = True

    def _drop_local_copy(self, listed):
        """The phone listed a sent message; forget our own record of it."""
        for key, m in list(self.items.items()):
            if (m.get("local") and m["text"] == listed["text"]
                    and normalize(m["address"]) == normalize(listed["address"])
                    and abs(m["time"] - listed["time"]) < LOCAL_MATCH_SECONDS):
                del self.items[key]

    def add_local(self, address, text):
        """Record a message sent from this app."""
        m = {"outgoing": True, "address": address, "name": "", "text": text, "time": time.time(),
             "read": True, "folder": "sent", "local": True}
        key = content_key(m)
        m["id"] = self._id(key)
        self.items[key] = m
        self.dirty = True
        return m

    def mark_read(self, msgs):
        """Mark messages read here; returns the ones that were unread (to tell the phone too)."""
        changed = []
        for m in msgs:
            if not m["read"]:
                m["read"], self.dirty = True, True
                changed.append(m)
        return changed

    def touch(self):
        self.dirty = True

    def save_if_dirty(self):
        if not self.dirty:
            return
        newest = sorted(self.items.values(), key=lambda m: m["time"])[-MAX_MESSAGES:]
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            newest_deleted = sorted(self.deleted.items(), key=lambda kv: kv[1])[-MAX_TOMBSTONES:]
            with open(tmp, "w") as f:
                json.dump({"messages": [{k: v for k, v in m.items() if k != "id"} for m in newest],
                           "deleted": newest_deleted, "deleted_sent": self.deleted_sent[-MAX_TOMBSTONES:]}, f)
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError as e:
            log.error("history: could not save %s: %s", self.path, e)
