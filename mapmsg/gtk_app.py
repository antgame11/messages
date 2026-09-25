"""iMessage-style GTK4/libadwaita client for an iPhone over Bluetooth (MAP + PBAP).

Run `./imsg_gtk.py [--debug] [--hidden]`. Closing the window hides it to the tray icon (if the panel
has one); `--hidden` starts with no window. Only warnings and errors are logged unless you pass
`--debug`, which logs everything to stderr and ~/.cache/imsg/debug.log.
"""
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import signal
import sys
import threading
import time

import dbus
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GObject, Gio, GLib, Gtk, Pango  # noqa: E402

from . import backend, battery  # noqa: E402
from .backend import normalize  # noqa: E402
from .audio import AudioGuard  # noqa: E402
from .history import History, content_key  # noqa: E402
from .media import MediaMonitor  # noqa: E402
from .private import harden, use_private_umask  # noqa: E402
from .settings import Settings, default_path  # noqa: E402
from .textutil import (CHAT_SCHEME, body_of, in_quiet_hours, is_truncated, linkify,  # noqa: E402
                       snippet)
from .tray import Tray  # noqa: E402

POLL_SECONDS = 0.7  # one poll is ~40 ms over Bluetooth
CALLS_REFRESH_SECONDS = 60
PHOTOS_REFRESH_SECONDS = 7 * 24 * 3600  # contact photos are ~1 MB; re-download them weekly
BATTERY_POLL_SECONDS = 30
BATTERY_RESTORE_SECONDS = 300  # at most one LE-link repair attempt per this long
CACHE = os.path.join(GLib.get_user_cache_dir(), "imsg")
LOG_FILE = os.path.join(CACHE, "debug.log")
log = logging.getLogger("imsg")


def setup_logging(debug):
    """Warnings and errors on stderr, nothing on disk. --debug / IMSG_DEBUG=1 turns on full detail
    (including message text) on stderr and in a rotating log file."""
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-5s %(name)-14s %(message)s", "%H:%M:%S")
    handlers = [logging.StreamHandler(sys.stderr)]
    if debug:
        try:
            os.makedirs(CACHE, exist_ok=True)
            handlers.append(logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2))
        except OSError:
            pass
    root = logging.getLogger("imsg")
    root.setLevel(logging.DEBUG if debug else logging.WARNING)
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    root.propagate = False
    if debug:
        log.info("debug logging on; also writing %s", LOG_FILE)


CHAT_ICONS = ("chat-bubbles-text-symbolic", "chat-message-new-symbolic", "mail-message-new-symbolic",
              "mail-unread-symbolic", "dialog-information-symbolic")


def chat_icon():
    """The first chat-style icon the current icon theme actually has (themes differ, and a missing
    one shows as a placeholder)."""
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    return next((name for name in CHAT_ICONS if theme.has_icon(name)), CHAT_ICONS[-1])


def clip(text, n=40):
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[:n - 1] + "…"


CSS = """
.bubble { padding: 8px 13px; border-radius: 18px; }
.bubble.in { background: alpha(@window_fg_color, 0.11); }
.bubble.out { background: #34c759; color: white; }
.bubble.out label selection { background: rgba(0,0,0,0.3); }
.bubble.emoji-only { background: none; padding: 0 4px; font-size: 32px; }
.thread-list { background: none; padding: 8px 0; }
.thread-list > row { padding: 0; background: none; }
.time-sep { font-size: 0.8em; opacity: 0.6; margin-top: 10px; margin-bottom: 2px; }
.unread-dot { color: #0a84ff; font-size: 0.7em; }
.battery { font-size: 0.85em; }
.tab-bar { padding: 4px 0; }
.call-note { font-size: 0.8em; opacity: 0.7; margin-top: 10px; margin-bottom: 2px; }
.bubble.out link { color: white; text-decoration: underline; }
.bubble.in link { color: #0a84ff; }
.draft { color: #ff3b30; }
.sending-dot { min-width: 9px; min-height: 9px; border-radius: 999px; background: alpha(@window_fg_color, 0.35); }
.media-bar { padding: 6px 10px; border-top: 1px solid alpha(@window_fg_color, 0.1); }
entry.composer { border-radius: 20px; padding: 6px 14px; min-height: 22px; }
button.send { border-radius: 999px; min-width: 32px; min-height: 32px; padding: 0;
              background: #34c759; color: white; }
button.send:disabled { background: alpha(@window_fg_color, 0.15); color: alpha(@window_fg_color, 0.4); }
.composer-bar { padding: 8px 12px; border-top: 1px solid alpha(@window_fg_color, 0.1); }
"""


# ---------------------------------------------------------------- formatting

def pretty(addr):
    d = "".join(ch for ch in addr if ch.isdigit())
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    if len(d) == 10 and not any(ch.isalpha() for ch in addr):
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return addr


def clock(t):
    return time.strftime("%-I:%M %p", time.localtime(t))


def day_label(t, with_time):
    now, then = time.localtime(), time.localtime(t)
    days = (time.mktime(time.struct_time((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1)))
            - time.mktime(time.struct_time((then.tm_year, then.tm_mon, then.tm_mday, 0, 0, 0, 0, 0, -1)))) // 86400
    if days == 0:
        day = "Today"
    elif days == 1:
        day = "Yesterday"
    elif days < 7:
        day = time.strftime("%A", then)
    else:
        day = time.strftime("%b %-d, %Y", then)
    return f"{day} {clock(t)}" if with_time else (clock(t) if days == 0 else day)


def key_of(addr):
    return normalize(addr) or addr.lower()


def is_emoji_only(text):
    t = text.strip()
    return 0 < len(t) <= 6 and all(ord(c) > 0x2000 and not c.isalnum() for c in t)


# -------------------------------------------------------------- contact cache

def save_contacts(contacts):
    os.makedirs(os.path.join(CACHE, "photos"), exist_ok=True)
    out = []
    for c in contacts:
        photo = None
        if c.get("photo"):
            photo = hashlib.sha1(c["photo"]).hexdigest()
            with open(os.path.join(CACHE, "photos", photo), "wb") as f:
                f.write(c["photo"])
        out.append({"name": c["name"], "numbers": c["numbers"], "photo": photo})
    with open(os.path.join(CACHE, "contacts.json"), "w") as f:
        json.dump(out, f)


def load_contacts():
    try:
        with open(os.path.join(CACHE, "contacts.json")) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []
    for c in raw:
        data = None
        if c["photo"]:
            try:
                with open(os.path.join(CACHE, "photos", c["photo"]), "rb") as f:
                    data = f.read()
            except OSError:
                pass
        c["photo"] = data
        c["numbers"] = [tuple(n) for n in c["numbers"]]
    return raw


DRAFTS_FILE = os.path.join(CACHE, "drafts.json")


def load_drafts():
    """-> {conversation key: {"text": unsent text, "address": number to send it to}}"""
    try:
        with open(DRAFTS_FILE) as f:
            return {k: d for k, d in json.load(f).items() if d.get("text", "").strip()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_drafts(drafts):
    try:
        os.makedirs(CACHE, exist_ok=True)
        with open(DRAFTS_FILE, "w") as f:
            json.dump(drafts, f)
    except OSError as e:
        log.error("drafts: could not save: %s", e)


# ---------------------------------------------------------------- BT worker

class Worker(threading.Thread):
    """Owns every obexd session; the UI talks to it through events and jobs."""

    def __init__(self, ui):
        super().__init__(daemon=True)
        self.ui = ui
        self.jobs = queue.Queue()
        self.retry = threading.Event()
        self.first_poll = threading.Event()  # set once the first message poll is done

    def emit(self, *event):
        GLib.idle_add(lambda: self.ui.on_event(*event) and False)

    def shutdown(self):
        """Release the phone's MAP/PBAP sessions; otherwise it holds them and refuses the next start."""
        obex = getattr(self, "obex", None)
        if obex:
            log.info("worker: closing sessions")
            obex.close()

    def submit(self, fn, done):
        self.jobs.put((fn, done, time.monotonic()))

    def fetch(self, count):
        started = time.monotonic()
        msgs = self.msgs.list("inbox", count)
        try:
            msgs += self.msgs.list("sent", count)
        except backend.ObexError as e:
            log.debug("worker: sent folder unavailable: %s", e)  # not every phone exposes one
        took = time.monotonic() - started
        (log.warning if took > 0.5 else log.debug)("worker: fetched %d messages (limit %d) in %.0f ms",
                                                   len(msgs), count, took * 1000)
        return msgs

    def run(self):
        addr = None
        while True:
            obex = self.obex = backend.Obex()
            try:
                self.emit("status", "Connecting to iPhone…", False)
                addr = addr or backend.find_phone()
                log.info("worker: connecting MAP to %s", addr)
                self.msgs = backend.Messages(obex, addr)
                log.info("worker: MAP connected")
                self.emit("connected")
                self.emit("messages", self.fetch(200), True)
                self.first_poll.set()
                self.serve()
            except (backend.ObexError, dbus.DBusException) as e:
                msg = getattr(e, "get_dbus_message", lambda: str(e))() or str(e)
                log.error("worker: connection lost/failed: %s", msg)
                self.emit("status", msg, True)
                if addr and ("refused" in msg or "Internal Server Error" in msg):
                    log.warning("worker: resetting Bluetooth link to clear the phone's stale session")
                    backend.reset_link(addr)  # phone still holds our old session
            finally:
                obex.close()
            log.info("worker: retrying in 1s")
            self.retry.wait(1)
            self.retry.clear()

    def serve(self):
        while True:
            try:
                fn, done, queued = self.jobs.get(timeout=POLL_SECONDS)
            except queue.Empty:
                self.emit("messages", self.fetch(30), False)
                continue
            waited = time.monotonic() - queued
            (log.warning if waited > 1 else log.debug)("worker: job waited %.1f s in the queue", waited)
            try:
                res, err = fn(self.msgs), None
            except Exception as e:  # reported to the caller, not fatal
                log.error("worker: job failed: %s", e)
                res, err = None, e
            GLib.idle_add(lambda: done(res, err) and False)


PHOTOS_MARKER = os.path.join(CACHE, "contacts.full")  # touched whenever photos were downloaded


def photos_stale():
    try:
        return time.time() - os.path.getmtime(PHOTOS_MARKER) > PHOTOS_REFRESH_SECONDS
    except OSError:
        return True


def attach_cached_photos(contacts, cached):
    """A names-only download has no photos; reuse the ones saved from the last full download."""
    by_number = {normalize(n): c["photo"] for c in cached if c.get("photo") for _, n in c["numbers"]}
    by_name = {c["name"].lower(): c["photo"] for c in cached if c.get("photo")}
    for c in contacts:
        if not c.get("photo"):
            c["photo"] = by_number.get(normalize(c["numbers"][0][1])) or by_name.get(c["name"].lower())


class PhonebookWorker(threading.Thread):
    """Contacts and call history over PBAP, on their own thread and obexd session.

    Contacts and messages share one Bluetooth link, and a full contact download (~1 MB of photos,
    ~20 s) starves message requests while it runs. So: wait for the first message poll, download
    only names and numbers on normal starts, and fetch photos just once a week."""

    def __init__(self, ui, worker):
        super().__init__(daemon=True)
        self.ui, self.worker = ui, worker
        self.obex = None

    def emit(self, *event):
        GLib.idle_add(lambda: self.ui.on_event(*event) and False)

    def shutdown(self):
        if self.obex:
            self.obex.close()

    def load_contacts(self, book):
        cached = load_contacts()
        with_photos = not cached or photos_stale()
        started = time.monotonic()
        contacts = book.contacts(photos=with_photos)
        if not with_photos:
            if not contacts:  # the phone ignored or rejected the names-only request
                log.warning("phonebook: names-only download came back empty, doing a full one")
                with_photos, contacts = True, book.contacts(photos=True)
            elif any(c.get("photo") for c in contacts):
                with_photos = True  # phone sent photos anyway
            else:
                attach_cached_photos(contacts, cached)
        if with_photos:
            os.makedirs(CACHE, exist_ok=True)
            open(PHOTOS_MARKER, "w").close()
        log.info("phonebook: %d contacts (%d with photos) in %.1f s (%s)", len(contacts),
                 sum(1 for c in contacts if c.get("photo")), time.monotonic() - started,
                 "photos refreshed" if with_photos else "names only, photos from cache")
        return contacts

    def run(self):
        self.worker.first_poll.wait(30)  # let messaging go first
        time.sleep(2)
        addr, warned = None, False
        while True:
            # heal=False: a refused PBAP session must not reset the link the message session uses
            obex = self.obex = backend.Obex(heal=False)
            try:
                addr = addr or backend.find_phone()
                book = backend.Phonebook(obex, addr)
                self.emit("contacts", self.load_contacts(book))
                while True:
                    started = time.monotonic()
                    calls = book.call_history()
                    log.debug("phonebook: %d calls in %.0f ms", len(calls), (time.monotonic() - started) * 1000)
                    self.emit("calls", calls)
                    time.sleep(CALLS_REFRESH_SECONDS)
            except (backend.ObexError, dbus.DBusException) as e:
                msg = getattr(e, "get_dbus_message", lambda: str(e))() or str(e)
                log.warning("phonebook: %s", msg)
                if not warned:
                    warned = True
                    self.emit("toast", f"Contacts unavailable — is Sync Contacts on? ({msg})")
            finally:
                obex.close()
            time.sleep(30)


class BatteryMonitor(threading.Thread):
    """Phone battery from BlueZ. Its own thread: repairing a stale LE link can take ~10 s and
    must not hold up message polling."""

    def __init__(self, ui):
        super().__init__(daemon=True)
        self.ui = ui

    def run(self):
        bus = dbus.SystemBus(private=True)
        addr, last_restore = None, float("-inf")
        while True:
            level = None
            try:
                addr = addr or backend.find_phone()
                level = battery.percent(bus, addr)
                if level is None and time.monotonic() - last_restore > BATTERY_RESTORE_SECONDS:
                    last_restore = time.monotonic()
                    log.info("battery: not exposed, trying to restore the LE link")
                    level = battery.restore_le_link(bus, addr)
            except (backend.ObexError, dbus.DBusException) as e:
                log.warning("battery: %s", e)
            log.debug("battery: %s", level)
            GLib.idle_add(lambda level=level: self.ui.on_event("battery", level) and False)
            time.sleep(BATTERY_POLL_SECONDS)


# ------------------------------------------------------------------------ UI

class ThreadItem(GObject.Object):
    """One row of a conversation: a message bubble or a call note, with an optional time label above."""

    def __init__(self, kind, data, sep, top_gap):
        super().__init__()
        self.kind, self.data, self.sep, self.top_gap = kind, data, sep, top_gap


CALL_LABELS = {"missed": "Missed", "received": "Incoming", "dialed": "Outgoing"}
CALL_ICONS = {"missed": "call-missed-symbolic", "received": "call-incoming-symbolic",
              "dialed": "call-outgoing-symbolic"}


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Messages", default_width=1000, default_height=700)
        self.app = app
        self.settings = Settings()
        self.settings.subscribe(self.on_setting_changed)
        self.battery_level = None
        self.battery_alerted = set()   # thresholds already notified this low-battery spell
        self.full_pending = set()      # long messages whose full text was already requested
        self.calls_by_key = {}
        self.history = History(os.path.join(CACHE, "history.json"))  # everything seen or sent, on disk
        self.drafts = {}         # key -> address for conversations with no messages yet
        self.calls = []          # phone call history, newest first
        self.convs = {}
        self.draft_text = load_drafts()  # key -> {"text", "address"}: unsent text per conversation
        for key, d in self.draft_text.items():
            self.drafts.setdefault(key, d["address"])  # a draft to a new number keeps its conversation
        self._loading_draft, self._draft_timer = False, 0
        self.contacts, self.by_num, self.textures = [], {}, {}
        self.current = None      # key of the open conversation
        self.sigs = {}
        self.loaded = False
        self._building = False

        new_message = Gio.SimpleAction.new("new-message", None)
        new_message.connect("activate", self.on_compose)
        self.add_action(new_message)
        open_settings = Gio.SimpleAction.new("settings", None)
        open_settings.connect("activate", self.on_settings)
        self.add_action(open_settings)
        quick_switch = Gio.SimpleAction.new("quick-switch", None)
        quick_switch.connect("activate", self.on_quick_switch)
        self.add_action(quick_switch)
        for name, handler in (("copy-message", self.on_copy_message), ("delete-message", self.on_delete_message),
                              ("delete-conversation", self.on_delete_conversation),
                              ("toggle-mute", self.on_toggle_mute)):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))  # target: message id / conversation key
            action.connect("activate", handler)
            self.add_action(action)
        self.connect("close-request", self.on_close_request)
        # coming back to the window marks the open conversation read
        self.connect("notify::is-active", lambda *_: self.is_active() and self.refresh())
        self.set_contacts(load_contacts())
        self.build()
        self.worker = Worker(self)
        self.worker.start()
        self.phonebook = PhonebookWorker(self, self.worker)
        self.phonebook.start()
        BatteryMonitor(self).start()
        self.media = MediaMonitor(lambda info: GLib.idle_add(lambda: self.on_event("media", info) and False),
                                  lambda msg: GLib.idle_add(lambda: self.on_event("media-error", msg) and False),
                                  enabled=self.settings.get("media_controls"))
        self.media.start()
        self.audio = AudioGuard(hide=not self.settings.get("allow_phone_audio"))
        self.audio.start()

    # ---- layout

    def build(self):
        prov = Gtk.CssProvider()
        prov.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.toasts = Adw.ToastOverlay()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.banner = Adw.Banner.new("Connecting to iPhone…")
        self.banner.set_button_label("Retry")
        self.banner.connect("button-clicked", lambda *_: self.worker.retry.set())
        self.banner.set_revealed(True)
        self.split = Adw.NavigationSplitView()
        self.split.set_vexpand(True)
        self.split.set_min_sidebar_width(300)
        self.split.set_max_sidebar_width(380)
        outer.append(self.banner)
        outer.append(self.split)
        self.toasts.set_child(outer)
        self.set_content(self.toasts)

        # sidebar
        self.stack = Adw.ViewStack()
        self.chat_list = self.make_list(self.chat_filter)
        self.chat_list.connect("row-selected", self.on_chat_selected)
        self.contact_list = self.make_list(self.contact_filter)
        self.contact_list.connect("row-activated", self.on_contact_activated)
        self.call_list = self.make_list(self.call_filter)
        self.call_list.connect("row-activated", self.on_call_activated)
        for lb, name, title, icon in ((self.chat_list, "chats", "Messages", chat_icon()),
                                      (self.call_list, "calls", "Calls", "call-start-symbolic"),
                                      (self.contact_list, "contacts", "Contacts", "system-users-symbolic")):
            sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
            sw.set_child(lb)
            self.stack.add_titled_with_icon(sw, name, title, icon)

        self.search = Gtk.SearchEntry(placeholder_text="Search", margin_start=10, margin_end=10,
                                      margin_top=6, margin_bottom=6)
        self.search.connect("search-changed", lambda *_: (self.chat_list.invalidate_filter(), self.update_snippets(),
                                                          self.call_list.invalidate_filter(),
                                                          self.contact_list.invalidate_filter()))
        compose = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="New message (Ctrl+N)")
        compose.connect("clicked", self.on_compose)
        self.side_title = Adw.WindowTitle.new("Messages", "")
        self.stack.connect("notify::visible-child", lambda *_: self.side_title.set_title(
            self.stack.get_page(self.stack.get_visible_child()).get_title()))
        self.battery_icon = Gtk.Image()
        self.battery_label = Gtk.Label(css_classes=["battery"])
        self.battery_box = Gtk.Box(spacing=4, visible=False, tooltip_text="iPhone battery")
        self.battery_box.append(self.battery_icon)
        self.battery_box.append(self.battery_label)
        header = Adw.HeaderBar()
        header.pack_start(compose)
        gear = Gtk.Button(icon_name="preferences-system-symbolic", tooltip_text="Settings (Ctrl+,)")
        gear.connect("clicked", self.on_settings)
        header.pack_end(gear)
        header.pack_end(self.battery_box)
        header.set_title_widget(self.side_title)
        tabs = Adw.ViewSwitcherBar(stack=self.stack, css_classes=["tab-bar"])
        tabs.set_reveal(True)
        side = Adw.ToolbarView()
        side.add_top_bar(header)
        side.add_bottom_bar(tabs)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.search)
        box.append(self.stack)
        box.append(self.build_media_bar())
        side.set_content(box)
        self.split.set_sidebar(Adw.NavigationPage.new(side, "Messages"))

        # conversation
        self.title_avatar = Adw.Avatar.new(28, "", False)
        self.title_label = Gtk.Label(label="", css_classes=["heading"])
        title = Gtk.Box(spacing=8)
        title.append(self.title_avatar)
        title.append(self.title_label)
        thread_header = Adw.HeaderBar()
        thread_header.set_title_widget(title)

        # A virtualized list: rows are only built for what is on screen, and the rest is estimated blank
        # space, so even very long conversations open instantly and scroll smoothly.
        self.thread_model = Gio.ListStore(item_type=ThreadItem)
        self._thread_sigs = []      # what each row shows, to update only the rows that changed
        self._thread_key = None     # the conversation currently in the list
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self.on_thread_setup)
        factory.connect("bind", self.on_thread_bind)
        self.thread_view = Gtk.ListView(model=Gtk.NoSelection(model=self.thread_model), factory=factory,
                                        css_classes=["thread-list"])
        self.thread_scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.thread_scroll.set_child(self.thread_view)
        self._stick_until = 0.0
        self.thread_scroll.get_vadjustment().connect("changed", self.on_thread_layout)

        self.entry = Gtk.Entry(placeholder_text="Text Message", hexpand=True, css_classes=["composer"])
        self.entry.connect("activate", self.on_send)
        self.entry.connect("changed", self.on_entry_changed)
        self.send_btn = Gtk.Button(icon_name="go-up-symbolic", css_classes=["send"],
                                   valign=Gtk.Align.CENTER, sensitive=False)
        self.send_btn.connect("clicked", self.on_send)
        composer = Gtk.Box(spacing=8, css_classes=["composer-bar"])
        composer.append(self.entry)
        composer.append(self.send_btn)

        thread = Adw.ToolbarView()
        thread.add_top_bar(thread_header)
        thread.set_content(self.thread_scroll)
        thread.add_bottom_bar(composer)

        empty = Adw.StatusPage(icon_name=chat_icon(), title="No Conversation Selected",
                               description="Pick a conversation, or start a new one.")
        empty_tv = Adw.ToolbarView()
        empty_tv.add_top_bar(Adw.HeaderBar())
        empty_tv.set_content(empty)
        self.content_stack = Gtk.Stack()
        self.content_stack.add_named(empty_tv, "empty")
        self.content_stack.add_named(thread, "thread")
        self.split.set_content(Adw.NavigationPage.new(self.content_stack, "Conversation"))

    def build_media_bar(self):
        """Now playing on the phone: title, artist, and previous / play-pause / next."""
        self.media_info = None
        self.media_title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"])
        self.media_sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["caption", "dim-label"])
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(self.media_title)
        text.append(self.media_sub)
        self.media_play = Gtk.Button(icon_name="media-playback-start-symbolic", css_classes=["flat", "circular"])
        self.media_play.connect("clicked", self.on_media_play)
        bar = Gtk.Box(spacing=4, visible=False, css_classes=["media-bar"])
        bar.append(text)
        back = Gtk.Button(icon_name="media-skip-backward-symbolic", tooltip_text="Previous",
                          css_classes=["flat", "circular"])
        back.connect("clicked", lambda _b: self.media.send("Previous"))
        bar.append(back)
        bar.append(self.media_play)
        forward = Gtk.Button(icon_name="media-skip-forward-symbolic", tooltip_text="Next",
                             css_classes=["flat", "circular"])
        forward.connect("clicked", lambda _b: self.media.send("Next"))
        bar.append(forward)
        self.media_bar = bar
        return bar

    def set_media(self, info):
        self.media_info = info
        self.media_bar.set_visible(info is not None)
        if not info:
            return
        self.media_title.set_label(info["title"] or info["app"] or "Now playing")
        self.media_sub.set_label(" · ".join(x for x in (info["artist"], info["app"]) if x))
        self.media_play.set_icon_name("media-playback-pause-symbolic" if info["status"] == "playing"
                                      else "media-playback-start-symbolic")
        self.media_play.set_tooltip_text("Pause" if info["status"] == "playing" else "Play")
        minutes, seconds = divmod(info["duration"] // 1000, 60)
        self.media_bar.set_tooltip_text(f"{info['title']}\n{info['album']}" + (f"\n{minutes}:{seconds:02d}" if info["duration"] else ""))

    def on_media_play(self, *_):
        self.media.send("Pause" if self.media_info and self.media_info["status"] == "playing" else "Play")

    @staticmethod
    def make_list(filter_func):
        lb = Gtk.ListBox(css_classes=["navigation-sidebar"])
        lb.set_filter_func(filter_func)
        return lb

    # ---- worker events

    def on_event(self, kind, *a):
        log.debug("event: %s", kind)
        if kind == "status":
            text, is_error = a
            (log.warning if is_error else log.info)("status: %s", text)
            self.banner.set_title(text)
            self.banner.set_button_label("Retry" if is_error else None)
            self.banner.set_revealed(True)
        elif kind == "connected":
            self.banner.set_revealed(False)
        elif kind == "toast":
            self.toasts.add_toast(Adw.Toast.new(a[0]))
        elif kind == "contacts":
            save_contacts(a[0])
            self.set_contacts(a[0])
            self.sigs.clear()
            self.refresh()
        elif kind == "messages":
            self.merge(*a)
            self.refresh()
        elif kind == "calls":
            self.calls = a[0]
            self.calls_by_key = {}
            for call in self.calls:
                self.calls_by_key.setdefault(key_of(call["numbers"][0][1]), []).append(call)
            log.debug("calls: %d entries", len(self.calls))
            self.refresh()
        elif kind == "battery":
            self.set_battery(a[0])
        elif kind == "media":
            self.set_media(a[0])
        elif kind == "media-error":
            self.toast(f"The phone didn't accept that: {a[0]}")
        return False

    def set_battery(self, level):
        self.battery_level = level
        self.battery_box.set_visible(level is not None and self.settings.get("show_battery"))
        if level is None:
            return
        self.battery_icon.set_from_icon_name(f"battery-level-{min(level // 10 * 10, 100)}-symbolic")
        self.battery_label.set_label(f"{level}%")
        (self.battery_label.add_css_class if level <= 20 else self.battery_label.remove_css_class)("error")
        self.battery_box.set_tooltip_text(f"iPhone battery: {level}%")
        self.check_battery_alert(level)

    def quiet_now(self):
        return self.settings.get("quiet_hours") and in_quiet_hours(
            time.localtime().tm_hour, self.settings.get("quiet_start"), self.settings.get("quiet_end"))

    def check_battery_alert(self, level):
        """Notify once when the phone falls to 20%, and again at 10%; re-arm after it recovers."""
        if level > 25:
            self.battery_alerted.clear()
            return
        if not self.settings.get("low_battery_alert") or self.quiet_now():
            return
        for threshold in (10, 20):  # most urgent first
            if level <= threshold and threshold not in self.battery_alerted:
                self.battery_alerted |= {t for t in (10, 20) if t >= threshold}
                log.info("battery: low battery alert at %d%%", level)
                n = Gio.Notification.new(f"iPhone battery low: {level}%")
                n.set_body("Charge your phone soon.")
                n.set_icon(Gio.ThemedIcon.new("battery-caution-symbolic"))
                self.app.send_notification("battery-low", n)
                break

    def merge(self, msgs, initial):
        fresh = self.history.merge_phone(msgs)
        if fresh or initial:
            log.info("merge: %d from phone, %d new%s (history now %d)", len(msgs), len(fresh),
                     " (initial load)" if initial else "", len(self.history.items))
        for m in fresh:
            log.debug("merge: new %s id=%s from=%s text=%r", "outgoing" if m["outgoing"] else "incoming",
                      m["id"], m["address"], clip(m["text"]))
        self.fetch_full_texts(msgs)
        if self.loaded and not initial:
            for m in fresh:
                if not m["outgoing"]:
                    self.notify(m)
        self.loaded = True
        self.history.save_if_dirty()

    def fetch_full_texts(self, msgs):
        """The phone's listing cuts long messages at ~255 bytes. Ask for the whole text once per message."""
        for p in msgs:
            if p["outgoing"] or not is_truncated(p["text"], p.get("size", 0)):
                continue
            key = content_key(p)
            entry = self.history.items.get(key)
            if not entry or entry.get("full") or key in self.full_pending:
                continue
            self.full_pending.add(key)  # stays set after a failure too, so a bad one isn't retried every poll
            prefix = p["text"][:20]

            def done(full, err, entry=entry, prefix=prefix):
                if err or not full or len(full) <= len(prefix) or not full.startswith(prefix):
                    log.warning("full text: not usable (%s)", err or "unexpected body")
                    return
                entry["full"] = full
                log.debug("full text: fetched %d characters", len(full))
                self.history.touch()
                self.history.save_if_dirty()
                self.refresh()
            self.worker.submit(lambda maps, path=p["path"]: maps.body(path), done)

    def notify(self, m):
        if not self.settings.get("notifications") or self.quiet_now():
            return
        if self.settings.is_muted(key_of(m["address"])):
            log.debug("notify: skipped, conversation is muted")
            return
        key = key_of(m["address"])
        if self.is_active() and self.current == key:
            log.debug("notify: skipped, conversation %s is open and focused", key)
            return
        log.debug("notify: showing notification for %s", m["id"])
        n = Gio.Notification.new(self.display_name(key, m["address"], m["name"]))
        n.set_body(m["text"])
        # clicking the notification runs app.open-chat with the sender's number (see App)
        n.set_default_action_and_target("app.open-chat", GLib.Variant.new_string(m["address"]))
        self.app.send_notification(f"msg-{m['id']}", n)

    # ---- data model

    def set_contacts(self, contacts):
        self.contacts = sorted(contacts, key=lambda c: c["name"].lower())
        self.by_num = {normalize(n): c for c in self.contacts for _, n in c["numbers"]}
        self.textures = {}

    def texture(self, contact):
        if not contact or not contact.get("photo"):
            return None
        key = id(contact)
        if key not in self.textures:
            try:
                self.textures[key] = Gdk.Texture.new_from_bytes(GLib.Bytes.new(contact["photo"]))
            except GLib.Error:
                self.textures[key] = None
        return self.textures[key]

    def avatar(self, size, contact, name):
        a = Adw.Avatar.new(size, name, bool(contact))
        tex = self.texture(contact)
        if tex:
            a.set_custom_image(tex)
        return a

    def display_name(self, key, address, msg_name=""):
        c = self.by_num.get(key)
        if c:
            return c["name"]
        if msg_name and key_of(msg_name) != key:
            return msg_name
        return pretty(address)

    def conversations(self):
        convs = {}
        for m in self.history.values():
            if not m["address"]:
                continue
            k = key_of(m["address"])
            convs.setdefault(k, {"key": k, "address": m["address"], "msgs": []})["msgs"].append(m)
        for k, addr in self.drafts.items():
            convs.setdefault(k, {"key": k, "address": addr, "msgs": []})
        for k, c in convs.items():
            c["msgs"].sort(key=lambda m: m["time"])
            last = c["msgs"][-1] if c["msgs"] else None
            c["address"] = last["address"] if last else c["address"]
            c["contact"] = self.by_num.get(k)
            c["name"] = self.display_name(k, c["address"], next(
                (m["name"] for m in reversed(c["msgs"]) if not m["outgoing"]), ""))
            c["time"] = last["time"] if last else time.time()
            c["preview"] = last["text"].replace("\n", " ") if last else "New message"
            # the "Draft:" label appears only once you've left the conversation, not while you type in it
            c["muted"] = self.settings.is_muted(k)
            first = c["msgs"][0]["time"] if c["msgs"] else None  # only calls from this conversation's time span
            c["calls"] = [x for x in self.calls_by_key.get(k, []) if first is not None and x["call"]["time"] >= first]
            draft = "" if k == self.current else self.draft_text.get(k, {}).get("text", "").strip()
            c["draft"] = draft
            if draft:
                c["preview"] = "Draft: " + draft.replace("\n", " ")
            c["unread"] = any(not m["outgoing"] and not m["read"] for m in c["msgs"])
        return sorted(convs.values(), key=lambda c: -c["time"])

    # ---- rendering

    def sync_read(self, msgs):
        """Tell the phone these messages were read. Runs on the messaging thread, and looks the
        messages up in the phone's current listing because its handles change between connections."""
        msgs = [m for m in msgs if not m["outgoing"]]
        if not msgs or not self.settings.get("mark_read_on_phone"):
            return
        keys = {content_key(m) for m in msgs}

        def job(maps):
            paths = [p["path"] for p in maps.list("inbox", 30) if not p["read"] and content_key(p) in keys]
            return maps.set_read(paths) if paths else 0

        def done(count, err):
            if err:
                log.warning("read: could not update the phone: %s", err)
            else:
                log.debug("read: marked %s message(s) read on the phone", count)
        self.worker.submit(job, done)

    def mark_current_read(self, convs):
        """Messages in the conversation you're looking at (window focused) count as read.
        Returns True if it changed anything, so the caller rebuilds the list."""
        current = next((c for c in convs if c["key"] == self.current), None)
        if not current or not self.is_active():
            return False
        unseen = [m for m in current["msgs"] if not m["outgoing"] and not m["read"]]
        if not unseen:
            return False
        self.sync_read(self.history.mark_read(unseen))
        self.history.save_if_dirty()
        log.debug("read: marked %d message(s) in the open conversation as read", len(unseen))
        return True

    def refresh(self):
        convs = self.conversations()
        if self.mark_current_read(convs):
            convs = self.conversations()
        self.convs = {c["key"]: c for c in convs}
        sig = tuple((c["key"], c["time"], c["unread"], len(c["msgs"]), c["name"], c["draft"], c["muted"],
                     sum(1 for m in c["msgs"] if m.get("full"))) for c in convs)
        if sig != self.sigs.get("side"):
            if log.isEnabledFor(logging.DEBUG):
                before = {row[0]: row for row in self.sigs.get("side", ())}
                changed = [f"{row[0]}: {before.get(row[0])} -> {row}" for row in sig if before.get(row[0]) != row]
                log.debug("ui: rebuilding sidebar (%d conversations); changed: %s", len(convs),
                          "; ".join(changed) or "order only")
            self.sigs["side"] = sig
            self.rebuild_chats(convs)
        if self.current and self.current not in self.convs:  # the open conversation was deleted
            self.current = None
            self._thread_key = None
            self._loading_draft = True
            self.entry.set_text("")
            self._loading_draft = False
            self.content_stack.set_visible_child_name("empty")
        unread = sum(1 for c in convs if c["unread"] and not c["muted"])  # muted chats stay out of the badge
        if unread != self.sigs.get("unread"):
            self.sigs["unread"] = unread
            self.set_title(f"({unread}) Messages" if unread else "Messages")
            if self.app.tray:
                self.app.tray.set_unread(unread)
        self.rebuild_contacts_once()
        self.rebuild_calls()
        if self.current in self.convs:
            self.render_thread(self.convs[self.current])

    def rebuild_chats(self, convs):
        self._building = True
        lb = self.chat_list
        while (child := lb.get_first_child()):
            lb.remove(child)
        for c in convs:
            row = Gtk.ListBoxRow()
            row.key = c["key"]
            row.texts = [body_of(m) for m in reversed(c["msgs"])]  # newest first, for search snippets
            row.name_addr = f"{c['name']} {c['address']}".lower()
            row.search = f"{row.name_addr} {c['preview']} " + " ".join(t.lower() for t in row.texts)
            row.default_preview = c["preview"]
            click = Gtk.GestureClick(button=3, propagation_phase=Gtk.PropagationPhase.CAPTURE)
            click.connect("pressed", lambda g, n, x, y, r=row, key=c["key"], muted=c["muted"]: self.show_menu(
                g, r, x, y, [("Unmute Notifications" if muted else "Mute Notifications", "win.toggle-mute"),
                             ("Delete Conversation…", "win.delete-conversation")], key))
            row.add_controller(click)
            box = Gtk.Box(spacing=10, margin_top=8, margin_bottom=8, margin_start=4, margin_end=4)
            dot = Gtk.Label(label="●", css_classes=["unread-dot"], width_chars=1,
                            opacity=1.0 if c["unread"] else 0.0)
            box.append(dot)
            box.append(self.avatar(44, c["contact"], c["name"]))
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                           valign=Gtk.Align.CENTER)
            top = Gtk.Box(spacing=6)
            name = Gtk.Label(label=c["name"], xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                             css_classes=["heading"] if c["unread"] else [])
            when = Gtk.Label(label=day_label(c["time"], False), css_classes=["caption", "dim-label"])
            top.append(name)
            if c["muted"]:
                top.append(Gtk.Image(icon_name="audio-volume-muted-symbolic", css_classes=["dim-label"],
                                     tooltip_text="Notifications muted"))
            top.append(when)
            preview = Gtk.Label(label=c["preview"], xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                lines=2, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                css_classes=["caption", "draft"] if c["draft"] else ["caption", "dim-label"])
            row.preview_label = preview
            text.append(top)
            text.append(preview)
            box.append(text)
            row.set_child(box)
            lb.append(row)
            if c["key"] == self.current:
                lb.select_row(row)
        self._building = False
        self.update_snippets()

    def update_snippets(self):
        """While searching, a conversation that matched only on message text shows that text."""
        q = self.search.get_text().strip()
        row = self.chat_list.get_first_child()
        while row:
            label = getattr(row, "preview_label", None)
            if label:
                hit = None
                if q and q.lower() not in row.name_addr:
                    hit = next((snippet(t, q) for t in row.texts if q.lower() in t.lower()), None)
                label.set_label(hit or row.default_preview)
            row = row.get_next_sibling()

    def rebuild_calls(self):
        sig = tuple((c["call"]["time"], c["call"]["type"], c["numbers"][0][1]) for c in self.calls)
        if sig == self.sigs.get("calls") and self.sigs.get("calls_contacts") == len(self.contacts):
            return
        self.sigs["calls"], self.sigs["calls_contacts"] = sig, len(self.contacts)
        lb = self.call_list
        while (child := lb.get_first_child()):
            lb.remove(child)
        for c in self.calls:
            number, kind = c["numbers"][0][1], c["call"]["type"]
            contact = self.by_num.get(normalize(number))
            name = contact["name"] if contact else (pretty(number) if c["name"] == number else c["name"])
            row = Gtk.ListBoxRow()
            row.number, row.search = number, f"{name} {number}".lower()
            box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
            box.append(self.avatar(36, contact, name))
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
            text.append(Gtk.Label(label=name, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                  css_classes=["error"] if kind == "missed" else []))
            text.append(Gtk.Label(label=f"{CALL_LABELS.get(kind, kind.title())} · {day_label(c['call']['time'], True)}",
                                  xalign=0, css_classes=["caption", "dim-label"]))
            box.append(text)
            box.append(Gtk.Image(icon_name=CALL_ICONS.get(kind, "call-start-symbolic"), css_classes=["dim-label"]))
            row.set_child(box)
            lb.append(row)
        log.debug("ui: rebuilt call list (%d)", len(self.calls))

    def rebuild_contacts_once(self):
        if self.sigs.get("contacts") == len(self.contacts):
            return
        self.sigs["contacts"] = len(self.contacts)
        lb = self.contact_list
        while (child := lb.get_first_child()):
            lb.remove(child)
        for c in self.contacts:
            row = Gtk.ListBoxRow()
            row.contact = c
            row.search = (c["name"] + " " + " ".join(n for _, n in c["numbers"])).lower()
            box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
            box.append(self.avatar(36, c, c["name"]))
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
            text.append(Gtk.Label(label=c["name"], xalign=0, ellipsize=Pango.EllipsizeMode.END))
            label, num = c["numbers"][0]
            text.append(Gtk.Label(label=f"{pretty(num)}" + (f"  ·  {label}" if label else ""),
                                  xalign=0, css_classes=["caption", "dim-label"]))
            box.append(text)
            row.set_child(box)
            lb.append(row)

    def render_thread(self, conv):
        switched = self._thread_key != conv["key"]
        self._thread_key = conv["key"]
        self.title_label.set_label(conv["name"])
        self.title_avatar.set_text(conv["name"])
        self.title_avatar.set_show_initials(bool(conv["contact"]))
        self.title_avatar.set_custom_image(self.texture(conv["contact"]))

        timeline = sorted([(m["time"], 0, m) for m in conv["msgs"]] + [(x["call"]["time"], 1, x) for x in conv["calls"]],
                          key=lambda item: (item[0], item[1]))
        specs, prev_time, prev = [], None, None  # prev: the previous bubble, if the last item was one
        for when, kind, obj in timeline:
            sep = day_label(when, True) if prev_time is None or when - prev_time > 3600 else None
            if kind == 1:
                specs.append((("call", when, obj["call"]["type"], sep), ("call", obj, sep, 0)))
                prev = None
            else:
                same_side = prev is not None and prev["outgoing"] == obj["outgoing"] and when - prev_time < 3600
                gap = 2 if same_side else 10
                specs.append(((obj["id"], body_of(obj), obj.get("failed"), obj.get("pending"), sep, gap),
                              ("msg", obj, sep, gap)))
                prev = obj
            prev_time = when

        old, new = self._thread_sigs, [sig for sig, _ in specs]
        keep = 0
        if not switched:
            while keep < min(len(old), len(new)) and old[keep] == new[keep]:
                keep += 1
        if keep == len(old) == len(new):
            return
        adj = self.thread_scroll.get_vadjustment()
        near_bottom = adj.get_upper() - adj.get_value() - adj.get_page_size() < 80
        # only the rows from the first difference on are replaced, so a new message just appends
        self.thread_model.splice(keep, len(old) - keep, [ThreadItem(*args) for _, args in specs[keep:]])
        self._thread_sigs = new
        log.debug("thread: %d rows (kept %d, replaced %d)", len(new), keep, len(new) - keep)
        if switched or near_bottom:
            # rows below the screen only have estimated heights until they are built, so keep pinning to
            # the bottom for a moment while the estimates settle
            self._stick_until = time.monotonic() + (1.0 if switched else 0.5)
            GLib.idle_add(self.scroll_to_bottom)

    def scroll_to_bottom(self):
        count = self.thread_model.get_n_items()
        if count:
            self.thread_view.scroll_to(count - 1, Gtk.ListScrollFlags.NONE, None)
        return False

    def on_thread_layout(self, adj):
        if time.monotonic() < self._stick_until:
            adj.set_value(adj.get_upper() - adj.get_page_size())

    def on_thread_setup(self, _factory, list_item):
        list_item.set_activatable(False)
        list_item.set_child(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=16, margin_end=16))

    def on_thread_bind(self, _factory, list_item):
        """Build the row's widgets when it scrolls into view."""
        box, item = list_item.get_child(), list_item.get_item()
        while (child := box.get_first_child()):
            box.remove(child)
        if item.sep:
            box.append(Gtk.Label(label=item.sep, css_classes=["time-sep"]))
        box.append(self.call_note(item.data) if item.kind == "call" else self.bubble(item.data, top_gap=item.top_gap))

    def call_note(self, call):
        kind = call["call"]["type"]
        note = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER, css_classes=["call-note"])
        note.append(Gtk.Image(icon_name=CALL_ICONS.get(kind, "call-start-symbolic"),
                              css_classes=["error"] if kind == "missed" else []))
        note.append(Gtk.Label(label=f"{CALL_LABELS.get(kind, kind.title())} call · {clock(call['call']['time'])}",
                              css_classes=["error"] if kind == "missed" else []))
        return note

    def on_link(self, _label, uri):
        """A link in a message: web links open in the browser, phone numbers start a conversation."""
        if uri.startswith(CHAT_SCHEME):
            number = uri[len(CHAT_SCHEME):]
            self.open_conversation(key_of(number), number)
        elif uri.lower().startswith(("http://", "https://")):
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
            except GLib.Error as e:
                self.toast(f"Could not open the link: {e.message}")
        return True  # never let GTK hand any other kind of link to the system

    def bubble(self, m, top_gap):
        side = "out" if m["outgoing"] else "in"
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=top_gap,
                        halign=Gtk.Align.END if m["outgoing"] else Gtk.Align.START)
        # capture phase, so this wins over the selectable label's own right-click menu
        click = Gtk.GestureClick(button=3, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", lambda g, n, x, y, mid=m["id"]: self.show_menu(
            g, outer, x, y, [("Copy Message", "win.copy-message"), ("Delete for Me", "win.delete-message")], mid))
        outer.add_controller(click)
        text = body_of(m)
        markup, has_links = linkify(text)
        label = Gtk.Label(label=markup if has_links else text, use_markup=has_links, wrap=True,
                          wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=42, xalign=0, selectable=True)
        if has_links:
            label.connect("activate-link", self.on_link)
        classes = ["bubble", side] + (["emoji-only"] if is_emoji_only(m["text"]) else [])
        holder = Gtk.Box(css_classes=classes)
        holder.append(label)
        row = Gtk.Box(spacing=8)
        if m.get("pending"):  # still being sent: a faint dot, removed once the phone has it
            row.append(Gtk.Box(css_classes=["sending-dot"], valign=Gtk.Align.CENTER))
        row.append(holder)
        outer.append(row)
        if m.get("failed"):
            outer.append(Gtk.Label(label="Not Delivered", halign=Gtk.Align.END,
                                   css_classes=["caption", "error"]))
        return outer

    # ---- settings

    def on_setting_changed(self, name, value):
        if name == "media_controls":
            self.media.set_enabled(value)
        elif name == "allow_phone_audio":
            self.audio.set_hide(not value)
        elif name == "show_battery":
            self.set_battery(self.battery_level)
        elif name == "muted_chats":
            self.sigs.pop("side", None)
            self.refresh()

    def on_settings(self, *_):
        dlg = Adw.PreferencesDialog(title="Settings")
        page = Adw.PreferencesPage()

        def switch(name, title, subtitle):
            row = Adw.SwitchRow(title=title, subtitle=subtitle)
            row.set_active(self.settings.get(name))
            row.connect("notify::active", lambda r, _p: self.settings.set(name, r.get_active()))
            return row

        def group(title, *rows):
            g = Adw.PreferencesGroup(title=title)
            for row in rows:
                g.add(row)
            page.add(g)

        tray_row = switch("hide_to_tray", "Keep running in the tray",
                          "Closing the window hides it. Use the tray icon to reopen or quit.")
        if not self.app.tray:
            tray_row.set_sensitive(False)
            tray_row.set_subtitle("Unavailable: no system tray was found, so closing the window quits.")
        def spin(name, title):
            row = Adw.SpinRow.new_with_range(0, 23, 1)
            row.set_title(title)
            row.set_value(self.settings.get(name))
            row.connect("notify::value", lambda r, _p: self.settings.set(name, int(r.get_value())))
            return row

        quiet = switch("quiet_hours", "Quiet hours", "No desktop notifications during these hours. Messages still arrive.")
        quiet_from, quiet_to = spin("quiet_start", "From (hour, 0–23)"), spin("quiet_end", "Until (hour, 0–23)")
        for row in (quiet_from, quiet_to):
            quiet.bind_property("active", row, "sensitive", GObject.BindingFlags.SYNC_CREATE)
        group("Window and notifications",
              switch("notifications", "Desktop notifications", "Show a notification for each new message."),
              quiet, quiet_from, quiet_to, tray_row)
        group("Phone",
              switch("mark_read_on_phone", "Mark messages read on the iPhone",
                     "Opening a conversation here also clears its unread badge on the phone."),
              switch("low_battery_alert", "Low battery alert", "Notify when the phone reaches 20% and again at 10%."),
              switch("show_battery", "Show battery level", "The phone's battery next to the compose button."))
        group("Audio and media",
              switch("allow_phone_audio", "Allow phone audio on this computer",
                     "Off keeps this computer out of the phone's audio output list while Messages is open. "
                     "On lets the phone use it as a speaker."),
              switch("media_controls", "Media controls",
                     "A now-playing bar with play, pause and skip for the phone's music. It may need phone "
                     "audio allowed to work."))
        dlg.add(page)
        dlg.present(self)

    # ---- context menus

    def show_menu(self, gesture, widget, x, y, entries, target):
        """Popup at the click. Each entry is (label, action); the action receives `target` as a string."""
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        menu = Gio.Menu()
        for label, action in entries:
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(action, GLib.Variant.new_string(target))
            menu.append_item(item)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(widget)
        popover.set_has_arrow(False)
        spot = Gdk.Rectangle()
        spot.x, spot.y, spot.width, spot.height = int(x), int(y), 1, 1
        popover.set_pointing_to(spot)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    def toast(self, text, undo=None):
        toast = Adw.Toast.new(text)
        if undo:
            toast.set_button_label("Undo")
            toast.connect("button-clicked", lambda _t: undo())
        self.toasts.add_toast(toast)

    def on_copy_message(self, _action, target):
        m = self.history.find(target.get_string())
        if not m:
            return
        value = GObject.Value()
        value.init(GObject.TYPE_STRING)
        value.set_string(body_of(m))
        self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(value))
        log.debug("menu: copied message %s", m["id"])
        self.toast("Message copied")

    def on_toggle_mute(self, _action, target):
        key = target.get_string()
        self.settings.set_muted(key, not self.settings.is_muted(key))
        log.info("menu: notifications for %s %s", key, "muted" if self.settings.is_muted(key) else "unmuted")
        conv = self.convs.get(key)
        self.toast(f"Notifications {'muted' if self.settings.is_muted(key) else 'on'} for {conv['name'] if conv else 'this conversation'}")

    def on_delete_message(self, _action, target):
        m = self.history.find(target.get_string())
        if not m:
            return
        removed = self.history.delete([m])
        self.history.save_if_dirty()
        log.info("menu: deleted 1 message from this computer")
        self.refresh()

        def undo():
            self.history.restore(removed)
            self.history.save_if_dirty()
            self.refresh()
        self.toast("Message deleted from this computer", undo)

    def on_delete_conversation(self, _action, target):
        key = target.get_string()
        conv = self.convs.get(key)
        if not conv:
            return
        count = len(conv["msgs"])
        body = (f"This removes {count} message{'s' if count != 1 else ''} and any draft from this computer. "
                "Your iPhone keeps its own copy.") if count else "This removes the empty conversation and its draft."
        dlg = Adw.AlertDialog.new(f"Delete conversation with {conv['name']}?", body)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, response: response == "delete" and self.delete_conversation(key))
        dlg.present(self)

    def delete_conversation(self, key):
        conv = self.convs.get(key)
        if not conv:
            return
        removed = self.history.delete(conv["msgs"])
        self.history.save_if_dirty()
        self.draft_text.pop(key, None)
        self.drafts.pop(key, None)
        save_drafts(self.draft_text)
        log.info("menu: deleted conversation %s (%d messages)", key, len(removed))
        self.sigs.clear()
        self.refresh()
        self.toast(f"Conversation with {conv['name']} deleted")

    # ---- actions

    def open_conversation(self, key, address):
        if key not in self.convs:
            self.drafts[key] = address
            self.sigs.pop("side", None)
            self.refresh()
        self.select(key)

    def select(self, key):
        previous, self.current = self.current, key
        conv = self.convs.get(key)
        if not conv:
            return
        if previous != key:  # each conversation keeps its own unsent text
            self._loading_draft = True
            self.entry.set_text(self.draft_text.get(key, {}).get("text", ""))
            self.entry.set_position(-1)
            self._loading_draft = False
        self.sync_read(self.history.mark_read(conv["msgs"]))
        self.history.save_if_dirty()
        self.sigs.pop("side", None)
        self.refresh()
        self.stack.set_visible_child_name("chats")
        self.content_stack.set_visible_child_name("thread")
        self.split.set_show_content(True)
        self.entry.grab_focus()

    def on_entry_changed(self, entry):
        text = entry.get_text()
        self.send_btn.set_sensitive(bool(text.strip()))
        if self._loading_draft or not self.current:
            return
        if text.strip():
            conv = self.convs.get(self.current)
            self.draft_text[self.current] = {"text": text, "address": conv["address"] if conv
                                             else self.drafts.get(self.current, "")}
        else:
            self.draft_text.pop(self.current, None)
        if self._draft_timer:
            GLib.source_remove(self._draft_timer)
        self._draft_timer = GLib.timeout_add(500, self.flush_drafts)  # wait for a pause in typing

    def flush_drafts(self):
        self._draft_timer = 0
        save_drafts(self.draft_text)
        log.debug("drafts: saved %d", len(self.draft_text))
        self.refresh()  # the sidebar shows "Draft: …"
        return False

    def on_chat_selected(self, lb, row):
        if self._building or row is None:
            return
        if row.key != self.current or self.content_stack.get_visible_child_name() == "empty":
            self.select(row.key)

    def on_call_activated(self, lb, row):
        self.open_conversation(key_of(row.number), row.number)

    def on_contact_activated(self, lb, row):
        num = row.contact["numbers"][0][1]
        self.open_conversation(key_of(num), num)

    def compose_candidates(self, query):
        """Rows for the new-message dialog: a typed number, recent chats, matching contact numbers."""
        q = query.strip()
        qd = "".join(ch for ch in q if ch.isdigit())
        out = []
        if len(qd) >= 7 and not any(ch.isalpha() for ch in q):
            out.append({"name": pretty(q), "number": q, "sub": "Send to this number", "contact": None})
        if not q:
            recent = list(self.convs.values())[:6]
            if recent:
                out.append({"header": "Recent"})
                out += [{"name": c["name"], "number": c["address"], "sub": pretty(c["address"]),
                         "contact": c["contact"]} for c in recent]
            out.append({"header": "Contacts"})
        def rank(c):  # names starting with the query first, then a word starting with it, then the rest
            name = c["name"].lower()
            return (0 if name.startswith(q.lower()) else 1 if any(w.startswith(q.lower()) for w in name.split()) else 2,
                    name)
        shown = 0
        for c in sorted(self.contacts, key=rank) if q else self.contacts:
            if q and q.lower() not in c["name"].lower() and not (
                    qd and any(qd in "".join(ch for ch in n if ch.isdigit()) for _, n in c["numbers"])):
                continue
            for label, number in c["numbers"]:  # one row per number, so you pick home vs cell
                out.append({"name": c["name"], "number": number,
                            "sub": pretty(number) + (f"  ·  {label}" if label else ""), "contact": c})
                shown += 1
            if shown >= 60:
                break
        if q and not any("number" in item for item in out):
            out.append({"empty": f"No contacts match “{q}”. Type a full phone number to message it."})
        return out

    def compose_row(self, item):
        row = Gtk.ListBoxRow()
        row.number = item.get("number")
        if "number" not in item:
            row.set_activatable(False)
            row.set_selectable(False)
            row.set_child(Gtk.Label(label=item.get("header") or item["empty"], xalign=0, wrap=True,
                                    margin_start=10, margin_top=10, margin_bottom=4, margin_end=10,
                                    css_classes=["caption-heading"] if "header" in item else ["dim-label"]))
            return row
        box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        box.append(self.avatar(36, item["contact"], item["name"]))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        text.append(Gtk.Label(label=item["name"], xalign=0, ellipsize=Pango.EllipsizeMode.END))
        text.append(Gtk.Label(label=item["sub"], xalign=0, css_classes=["caption", "dim-label"]))
        box.append(text)
        row.set_child(box)
        return row

    def switch_candidates(self, query):
        """Existing conversations matching by name, number or message text (Ctrl+K)."""
        q, out = query.strip().lower(), []
        for c in self.convs.values():
            hit = None
            if q and q not in f"{c['name']} {c['address']}".lower():
                hit = next((snippet(body_of(m), q) for m in reversed(c["msgs"]) if q in body_of(m).lower()), None)
                if not hit:
                    continue
            out.append({"name": c["name"], "number": c["address"], "sub": hit or pretty(c["address"]),
                        "contact": c["contact"]})
            if len(out) >= 40:
                break
        return out or [{"empty": f"No conversation matches “{query.strip()}”."}]

    def on_quick_switch(self, *_):
        self.show_picker("Go to Conversation", "Search conversations and messages", self.switch_candidates)

    def on_compose(self, *_):
        self.show_picker("New Message", "To: name or number", self.compose_candidates)

    def show_picker(self, title, placeholder, candidates):
        dlg = Adw.Dialog(title=title, content_width=420, content_height=540)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        entry = Gtk.SearchEntry(placeholder_text=placeholder, margin_start=12, margin_end=12,
                                margin_top=6, margin_bottom=6)
        lb = Gtk.ListBox(css_classes=["navigation-sidebar"], selection_mode=Gtk.SelectionMode.NONE)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(lb)
        box.append(entry)
        box.append(scroll)
        toolbar.set_content(box)
        dlg.set_child(toolbar)

        def first_choice():
            row = lb.get_first_child()
            while row and not row.number:
                row = row.get_next_sibling()
            return row

        def pick(number):
            log.info("picker: opening conversation with %s", number)
            dlg.close()
            self.open_conversation(key_of(number), number)

        def populate(*_):
            while (child := lb.get_first_child()):
                lb.remove(child)
            for item in candidates(entry.get_text()):
                lb.append(self.compose_row(item))

        def on_key(_ctl, keyval, _code, _state):
            if keyval == Gdk.KEY_Down and first_choice():  # arrow down moves into the results
                first_choice().grab_focus()
                return True
            return False

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", on_key)
        entry.add_controller(keys)
        entry.connect("search-changed", populate)
        entry.connect("activate", lambda *_: first_choice() and pick(first_choice().number))  # Enter takes the top result
        lb.connect("row-activated", lambda _lb, row: row.number and pick(row.number))
        populate()
        dlg.present(self)
        entry.grab_focus()

    def on_close_request(self, *_):
        """With a tray icon to come back from, closing hides the window and keeps receiving."""
        if not self.app.tray or not self.settings.get("hide_to_tray"):
            self.app.quit()  # the tray keeps the app held open, so quit explicitly
            return False
        self.set_visible(False)
        if not self.app.told_hidden:
            self.app.told_hidden = True
            n = Gio.Notification.new("Messages is still running")
            n.set_body("It keeps receiving in the background. Use the tray icon to reopen or quit.")
            self.app.send_notification("still-running", n)
        return True

    def on_send(self, *_):
        text = self.entry.get_text().strip()
        conv = self.convs.get(self.current)
        if not text or not conv:
            return
        self.entry.set_text("")
        log.info("send: to %s (%d chars)", conv["address"], len(text))
        log.debug("send: text=%r", clip(text, 80))
        msg = self.history.add_local(conv["address"], text)
        msg["pending"] = True
        self.history.save_if_dirty()
        self.refresh()

        def done(_res, err):
            msg.pop("pending", None)
            self.history.touch()
            if not err:
                log.info("send: handed to the phone (%.1f s after you pressed send)", time.time() - msg["time"])
                self.history.save_if_dirty()
                self.refresh()  # removes the sending dot
            if err:
                log.error("send: failed: %s", err)
                msg["failed"] = True
                self.history.touch()
                self.history.save_if_dirty()
                self.toasts.add_toast(Adw.Toast.new(f"Send failed: {err}"))
                self.refresh()
        self.worker.submit(lambda maps: maps.send(conv["address"], text), done)

    # ---- filters

    def chat_filter(self, row):
        q = self.search.get_text().strip().lower()
        return not q or q in row.search

    def call_filter(self, row):
        q = self.search.get_text().strip().lower()
        return not q or q in row.search

    def contact_filter(self, row):
        q = self.search.get_text().strip().lower()
        return not q or q in row.search


class App(Adw.Application):
    def __init__(self, start_hidden=False):
        super().__init__(application_id="dev.andrew.Imsg")
        self.start_hidden, self.tray, self.told_hidden, self._activated = start_hidden, None, False, False

    def do_startup(self):
        Adw.Application.do_startup(self)
        open_chat = Gio.SimpleAction.new("open-chat", GLib.VariantType.new("s"))
        open_chat.connect("activate", self.on_open_chat)
        self.add_action(open_chat)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("win.new-message", ["<Control>n"])
        self.set_accels_for_action("win.settings", ["<Control>comma"])
        self.set_accels_for_action("win.quick-switch", ["<Control>k"])
        try:
            tray = Tray(on_activate=self.toggle_window, on_quit=self.quit)
            if tray.start():
                self.tray = tray
                self.hold()  # stay alive with the window hidden; the tray menu quits
        except Exception:  # never let a missing tray stop the app from starting
            log.exception("tray unavailable; closing the window will quit")
        for sig in (signal.SIGINT, signal.SIGTERM):  # quit cleanly so sessions get released
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda: self.quit() or GLib.SOURCE_REMOVE)

    def on_open_chat(self, _action, target):
        win = self.props.active_window
        if not win:
            return
        address = target.get_string()
        log.info("notification clicked: opening conversation with %s", address)
        win.present()
        win.open_conversation(key_of(address), address)

    def toggle_window(self):
        win = self.props.active_window
        if not win:
            return
        if win.is_visible() and win.is_active():
            win.set_visible(False)
        else:
            win.present()

    def do_activate(self):
        win = self.props.active_window or Window(self)
        first, self._activated = not self._activated, True
        if first and self.start_hidden and self.tray:
            log.info("started hidden; use the tray icon to open the window")
            return
        win.present()

    def do_shutdown(self):
        win = self.props.active_window
        if win:
            save_drafts(win.draft_text)
            win.audio.shutdown()
            win.worker.shutdown()
            win.phonebook.shutdown()
        Adw.Application.do_shutdown(self)


def run():
    debug = "--debug" in sys.argv or bool(os.environ.get("IMSG_DEBUG"))
    hidden = "--hidden" in sys.argv
    argv = [a for a in sys.argv if a not in ("--debug", "--hidden")]
    use_private_umask()  # everything the app saves is readable only by you
    harden(CACHE, os.path.dirname(default_path()))
    setup_logging(debug)
    return App(start_hidden=hidden).run(argv)
