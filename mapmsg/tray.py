"""System tray icon via the StatusNotifierItem protocol (what XFCE, KDE and most panels use).

GTK4 has no tray API, so this speaks the protocol directly over D-Bus: the icon itself
(org.kde.StatusNotifierItem) and its right-click menu (com.canonical.dbusmenu). The icon is drawn
here, so it does not depend on the icon theme, and shows a red dot while messages are unread.
Needs a GLib main loop, which the GTK app already runs.
"""
import logging
import os

import dbus
import dbus.mainloop.glib
import dbus.service

log = logging.getLogger("imsg.tray")

SNI_PATH, MENU_PATH = "/StatusNotifierItem", "/MenuBar"
SNI_IFACE, MENU_IFACE = "org.kde.StatusNotifierItem", "com.canonical.dbusmenu"
WATCHER = ("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher")
SIZES = (22, 24, 32, 48, 64)


# ---------------------------------------------------------------- icon drawing

def _in_round_rect(x, y, x0, y0, x1, y1, r):
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _in_triangle(x, y, a, b, c):
    def side(p, q, r):
        return (p[0] - r[0]) * (q[1] - r[1]) - (q[0] - r[0]) * (p[1] - r[1])
    d1, d2, d3 = side((x, y), a, b), side((x, y), b, c), side((x, y), c, a)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def _pixel(u, v, unread):
    """Colour (r, g, b, a) at unit-square point (u, v), or None if transparent."""
    if unread:
        d2 = (u - 0.76) ** 2 + (v - 0.24) ** 2
        if d2 <= 0.19 ** 2:
            return (255, 59, 48, 255)
        if d2 <= 0.25 ** 2:
            return (255, 255, 255, 255)  # ring so the dot stands out on any panel colour
    if _in_round_rect(u, v, 0.08, 0.14, 0.92, 0.70, 0.22) or _in_triangle(u, v, (0.20, 0.62), (0.18, 0.90), (0.46, 0.66)):
        if _in_round_rect(u, v, 0.24, 0.30, 0.76, 0.38, 0.04) or _in_round_rect(u, v, 0.24, 0.46, 0.58, 0.54, 0.04):
            return (255, 255, 255, 255)
        return (52, 199, 89, 255)
    return None


def _pixmap(size, unread):
    """(width, height, ARGB32 bytes) — 3x3 supersampled for smooth edges."""
    data = bytearray()
    for py in range(size):
        for px in range(size):
            r = g = b = a = 0
            for sy in range(3):
                for sx in range(3):
                    c = _pixel((px + (sx + 0.5) / 3) / size, (py + (sy + 0.5) / 3) / size, unread)
                    if c:
                        r, g, b, a = r + c[0], g + c[1], b + c[2], a + 255
            n = 9
            alpha = a // n
            data += bytes((alpha, r // n if a else 0, g // n if a else 0, b // n if a else 0))
    return size, size, bytes(data)


def _icons(unread):
    return dbus.Array([dbus.Struct((dbus.Int32(w), dbus.Int32(h), dbus.Array(data, signature="y")),
                                   signature="iiay") for w, h, data in (_pixmap(s, unread) for s in SIZES)],
                      signature="(iiay)")


# ------------------------------------------------------------------ the D-Bus objects

class _Item(dbus.service.Object):
    def __init__(self, bus, tray):
        super().__init__(bus, SNI_PATH)
        self.tray = tray
        self.pixmaps = {False: _icons(False), True: _icons(True)}

    def props(self):
        unread = self.tray.unread
        tip = f"{unread} unread conversation{'s' if unread != 1 else ''}" if unread else "No unread messages"
        return {"Category": "Communications", "Id": "imsg", "Title": "Messages",
                "Status": "NeedsAttention" if unread else "Active", "WindowId": dbus.UInt32(0),
                "IconName": "", "IconPixmap": self.pixmaps[bool(unread)],
                "AttentionIconName": "", "AttentionIconPixmap": self.pixmaps[True],
                "ToolTip": dbus.Struct(("", dbus.Array([], signature="(iiay)"), "Messages", tip),
                                       signature="sa(iiay)ss"),
                "ItemIsMenu": False, "Menu": dbus.ObjectPath(MENU_PATH)}

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return self.props()

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ss", out_signature="v")
    def Get(self, iface, name):
        return self.props()[name]

    @dbus.service.method(SNI_IFACE, in_signature="ii")
    def Activate(self, x, y):
        self.tray.on_activate()

    @dbus.service.method(SNI_IFACE, in_signature="ii")
    def SecondaryActivate(self, x, y):
        self.tray.on_activate()

    @dbus.service.method(SNI_IFACE, in_signature="ii")
    def ContextMenu(self, x, y):
        pass  # the panel shows the dbusmenu at Menu

    @dbus.service.method(SNI_IFACE, in_signature="is")
    def Scroll(self, delta, orientation):
        pass

    @dbus.service.signal(SNI_IFACE)
    def NewIcon(self): pass

    @dbus.service.signal(SNI_IFACE)
    def NewToolTip(self): pass

    @dbus.service.signal(SNI_IFACE, signature="s")
    def NewStatus(self, status): pass


class _Menu(dbus.service.Object):
    """Minimal com.canonical.dbusmenu: a Show/Hide entry and a Quit entry."""
    SHOW, QUIT = 1, 2

    def __init__(self, bus, tray):
        super().__init__(bus, MENU_PATH)
        self.tray = tray

    def items(self):
        return {self.SHOW: {"label": "Show / Hide Messages"}, self.QUIT: {"label": "Quit"}}

    def _layout(self):
        children = [dbus.Struct((dbus.Int32(i), dbus.Dictionary(p, signature="sv"), dbus.Array([], signature="v")),
                                signature="ia{sv}av", variant_level=1) for i, p in self.items().items()]
        return dbus.Struct((dbus.Int32(0), dbus.Dictionary({"children-display": "submenu"}, signature="sv"),
                            dbus.Array(children, signature="v")), signature="ia{sv}av")

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return {"Version": dbus.UInt32(3), "TextDirection": "ltr", "Status": "normal",
                "IconThemePath": dbus.Array([], signature="s")}

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ss", out_signature="v")
    def Get(self, iface, name):
        return self.GetAll(iface)[name]

    @dbus.service.method(MENU_IFACE, in_signature="iias", out_signature="u(ia{sv}av)")
    def GetLayout(self, parent, depth, names):
        return dbus.UInt32(1), self._layout()

    @dbus.service.method(MENU_IFACE, in_signature="aias", out_signature="a(ia{sv})")
    def GetGroupProperties(self, ids, names):
        return dbus.Array([dbus.Struct((dbus.Int32(i), dbus.Dictionary(p, signature="sv")), signature="ia{sv}")
                           for i, p in self.items().items() if not ids or i in ids], signature="(ia{sv})")

    @dbus.service.method(MENU_IFACE, in_signature="is", out_signature="v")
    def GetProperty(self, item, name):
        return self.items().get(item, {}).get({"label": "label"}.get(name, name), "")

    @dbus.service.method(MENU_IFACE, in_signature="isvu")
    def Event(self, item, event, data, timestamp):
        if event == "clicked":
            (self.tray.on_activate if item == self.SHOW else self.tray.on_quit)()

    @dbus.service.method(MENU_IFACE, in_signature="a(isvu)", out_signature="ai")
    def EventGroup(self, events):
        for item, event, data, timestamp in events:
            self.Event(item, event, data, timestamp)
        return dbus.Array([], signature="i")

    @dbus.service.method(MENU_IFACE, in_signature="i", out_signature="b")
    def AboutToShow(self, item):
        return False

    @dbus.service.method(MENU_IFACE, in_signature="ai", out_signature="aiai")
    def AboutToShowGroup(self, ids):
        return dbus.Array([], signature="i"), dbus.Array([], signature="i")

    @dbus.service.signal(MENU_IFACE, signature="a(ia{sv})a(ias)")
    def ItemsPropertiesUpdated(self, updated, removed): pass

    @dbus.service.signal(MENU_IFACE, signature="ui")
    def LayoutUpdated(self, revision, parent): pass

    @dbus.service.signal(MENU_IFACE, signature="iu")
    def ItemActivationRequested(self, item, timestamp): pass


class Tray:
    """on_activate: toggle the window. on_quit: exit the app."""

    def __init__(self, on_activate, on_quit):
        self.on_activate, self.on_quit = on_activate, on_quit
        self.unread = 0
        self.bus = dbus.SessionBus(mainloop=dbus.mainloop.glib.DBusGMainLoop(), private=True)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._name = dbus.service.BusName(self.name, self.bus)
        self.item, self.menu = _Item(self.bus, self), _Menu(self.bus, self)

    def start(self):
        """Register with the panel; returns False if there is no tray to show it in."""
        try:
            watcher = self.bus.get_object(WATCHER[0], WATCHER[1])
            props = dbus.Interface(watcher, "org.freedesktop.DBus.Properties")
            if not props.Get(WATCHER[2], "IsStatusNotifierHostRegistered"):
                log.info("tray: a StatusNotifierWatcher exists but no panel is hosting icons")
                return False
            dbus.Interface(watcher, WATCHER[2]).RegisterStatusNotifierItem(self.name)
        except dbus.DBusException as e:
            log.info("tray: no StatusNotifier service available (%s)", e.get_dbus_message())
            return False
        log.info("tray: icon registered as %s", self.name)
        return True

    def set_unread(self, count):
        if count == self.unread:
            return
        self.unread = count
        self.item.NewIcon()
        self.item.NewToolTip()
        self.item.NewStatus("NeedsAttention" if count else "Active")
