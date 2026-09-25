"""Small pure helpers for message text: links, search snippets, truncation, quiet hours."""
import html
import re

_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"]+", re.I)
_PHONE = re.compile(r"(?<![\w+])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
CHAT_SCHEME = "imsg-chat:"


def _trim_url(url):
    """Drop punctuation that ends a sentence rather than the link, and unmatched closing brackets."""
    while url and (url[-1] in ".,;:!?'\"" or (url[-1] in ")]}" and url.count(url[-1]) > url.count({")": "(", "]": "[", "}": "{"}[url[-1]]))):
        url = url[:-1]
    return url


def linkify(text):
    """-> (pango markup, found_links). URLs become web links, phone numbers open a conversation."""
    out, pos, found = [], 0, False
    spans = []
    for m in _URL.finditer(text):
        url = _trim_url(m.group())
        if url:
            spans.append((m.start(), m.start() + len(url), url if url.lower().startswith("http") else "https://" + url))
    taken = [(a, b) for a, b, _ in spans]
    for m in _PHONE.finditer(text):
        if not any(a < m.end() and m.start() < b for a, b in taken):
            digits = re.sub(r"\D", "", m.group())
            spans.append((m.start(), m.end(), CHAT_SCHEME + ("+" + digits if len(digits) == 11 else "+1" + digits)))
    for start, end, href in sorted(spans):
        out.append(html.escape(text[pos:start], quote=False))
        out.append(f'<a href="{html.escape(href, quote=True)}">{html.escape(text[start:end], quote=False)}</a>')
        pos, found = end, True
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out), found


def snippet(text, query, width=60):
    """Part of `text` around the first match of `query` (case-insensitive), with ellipses."""
    flat = " ".join(text.split())
    i = flat.lower().find(query.lower())
    if i < 0:
        return None
    start = max(0, i - width // 3)
    end = min(len(flat), start + width)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def is_truncated(text, size):
    """The phone cuts message previews at ~255 bytes; `size` is the full length it reports.
    A small gap is just stripped whitespace, so only long previews with a real gap count."""
    n = len(text.encode("utf-8"))
    return bool(size) and n >= 200 and size > n + 2


def body_of(m):
    return m.get("full") or m["text"]


def in_quiet_hours(hour, start, end):
    """True if `hour` (0-23) is inside [start, end), where the window may wrap past midnight."""
    if start == end:
        return False
    return start <= hour < end if start < end else hour >= start or hour < end
