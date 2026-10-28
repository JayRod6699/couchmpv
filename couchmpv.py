#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lightweight GTK3 + libmpv video player optimized for TV remote navigation.
Works on Wayland and X11 (no window embedding – mpv runs fullscreen).

Features:
  - Recursive folder browsing (Videos → TVShow → Season01 → episodes)
  - Universal Back key support for remotes / controllers
  - Exit card on the main screen
  - ±20 s seek, play/pause, audio/subtitle selection

Dependencies (Arch Linux):
  sudo pacman -S python-gobject gtk3 mpv python-mpv python-cairo

Run (Wayland or X11 – no special flags needed):
  python tv_mpv_player.py
"""

import os
import re
import json
import time
import locale
import xml.etree.ElementTree as ET
from pathlib import Path

# Required by libmpv – must be set before creating any MPV instance
locale.setlocale(locale.LC_NUMERIC, "C")

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GLib, Pango, Gio, GdkPixbuf

import cairo
import mpv

# Watch history + user config
CONFIG_DIR = Path(os.path.expanduser("~/.config/couchmpv"))
HISTORY_DIR = CONFIG_DIR
HISTORY_FILE = HISTORY_DIR / "history.json"
CONFIG_FILE = CONFIG_DIR / "config"
WATCHED_THRESHOLD = 0.90
RESUME_MIN_SECONDS = 5

COVER_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
# Max cover size (aspect ratio preserved; frame hugs the image)
COVER_MAX_W = 340
COVER_MAX_H = 480
COVER_FALLBACK_W = 280
COVER_FALLBACK_H = 400
COVER_CORNER_RADIUS = 10


def pixbuf_with_rounded_corners(pixbuf, radius=COVER_CORNER_RADIUS):
    """Return a new pixbuf with transparent rounded corners (matches frame border)."""
    w, h = pixbuf.get_width(), pixbuf.get_height()
    if w < 1 or h < 1:
        return pixbuf
    radius = max(0, min(int(radius), w // 2, h // 2))
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surface)
    r = float(radius)
    cr.new_sub_path()
    cr.arc(w - r, r, r, -0.5 * 3.14159265, 0)
    cr.arc(w - r, h - r, r, 0, 0.5 * 3.14159265)
    cr.arc(r, h - r, r, 0.5 * 3.14159265, 3.14159265)
    cr.arc(r, r, r, 3.14159265, 1.5 * 3.14159265)
    cr.close_path()
    cr.clip()
    Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
    cr.paint()
    return Gdk.pixbuf_get_from_surface(surface, 0, 0, w, h)


DEFAULT_CONFIG = """# CouchMPV configuration
# Lines starting with # are comments.

# =============================================================================
# DIRECTORIES
# One path per line. ~ expands to your home directory.
# =============================================================================
~/
~/Videos
# ~/Movies
# /media

# =============================================================================
# MPV SETTINGS
# Same syntax as mpv.conf (option=value).
# https://mpv.io/manual/stable/#options
# =============================================================================
# sub-font-size=52
# sub-font=sans-serif
# alang=en,eng
# slang=en,eng
# hwdec=auto
"""


def _ensure_config_file() -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_FILE.is_file():
            CONFIG_FILE.write_text(DEFAULT_CONFIG, encoding="utf-8")
            print(f"Created default config: {CONFIG_FILE}")
    except Exception as e:
        print("config create error:", e)


def load_user_config():
    _ensure_config_file()
    directories, mpv_opts, section = [], {}, None
    try:
        text = CONFIG_FILE.read_text(encoding="utf-8")
    except Exception as e:
        print("config read error:", e)
        return [os.path.expanduser("~/Videos")], {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        upper = line.lstrip("#").strip().upper()
        if upper.startswith("DIRECTORIES"):
            section = "directories"
            continue
        if upper.startswith("MPV SETTINGS") or upper == "MPV":
            section = "mpv"
            continue
        if line.startswith("#"):
            continue
        if section == "directories":
            path = line.strip().strip("\"'")
            if path:
                directories.append(os.path.expanduser(path))
        elif section == "mpv":
            if "=" in line:
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip("\"'")
                if key:
                    mpv_opts[key] = val
            elif line.strip():
                mpv_opts[line.strip()] = "yes"
    if not directories:
        directories = [os.path.expanduser("~/"), os.path.expanduser("~/Videos")]
    return directories, mpv_opts


DIRECTORIES, MPV_USER_OPTS = load_user_config()

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".webm", ".mov", ".m4v", ".wmv",
    ".flv", ".ts", ".m2ts", ".mpg", ".mpeg", ".ogv", ".3gp",
    ".vob", ".rmvb", ".divx", ".xvid"
}

SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}

SKIP_SECONDS = 20
ANIM_MS = 160

GRAD_TOP    = (0.20, 0.07, 0.32)
GRAD_BOTTOM = (0.06, 0.03, 0.14)

# Universal Back keys
BACK_KEYVALS = {Gdk.KEY_Escape, Gdk.KEY_BackSpace}
for _name in ("KEY_Back", "KEY_XF86Back", "KEY_GoBack"):
    _val = getattr(Gdk, _name, None)
    if _val is not None:
        BACK_KEYVALS.add(_val)

BACK_KEYCODES = {158, 166}

# Universal Home keys (Android TV / media remotes / BrowserHome)
HOME_KEYVALS = set()
for _name in (
    "KEY_HomePage", "KEY_XF86HomePage", "KEY_XF86Go",
    "KEY_Go", "KEY_Home",  # KEY_Home = keyboard Home; often OK on TV remotes
):
    _val = getattr(Gdk, _name, None)
    if _val is not None:
        HOME_KEYVALS.add(_val)


def is_video(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS


def is_dir(path: Path) -> bool:
    return path.is_dir() and not path.name.startswith(".")


def format_time(seconds) -> str:
    if seconds is None or seconds < 0:
        return "00:00"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def folder_name(path: str) -> str:
    p = Path(path)
    return p.name or str(p)


def _xml_text(root, *tags):
    """First matching tag text under root (any depth)."""
    for tag in tags:
        for el in root.iter(tag):
            if el.text and el.text.strip():
                return el.text.strip()
    return ""


def parse_nfo(nfo_path: Path) -> dict:
    """Parse Kodi-style NFO or plain-text → {title, plot}."""
    out = {"title": "", "plot": ""}
    if not nfo_path or not nfo_path.is_file():
        return out
    try:
        raw = nfo_path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return out
    if not raw:
        return out
    # XML NFO
    if raw.startswith("<") or raw.startswith("\ufeff<"):
        try:
            root = ET.fromstring(raw)
            out["title"] = _xml_text(root, "title", "name", "showtitle")
            out["plot"] = _xml_text(root, "plot", "description", "outline", "summary")
            return out
        except ET.ParseError:
            pass
    # Plain text: first line title, rest plot — or all plot
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) == 1:
        out["plot"] = lines[0]
    elif len(lines) > 1:
        out["title"] = lines[0]
        out["plot"] = "\n".join(lines[1:])
    return out


def find_cover(directory: Path, basename: str) -> Path | None:
    """Cover image matching basename, or common poster names."""
    for ext in COVER_EXTS:
        cand = directory / f"{basename}{ext}"
        if cand.is_file():
            return cand
    for name in ("poster", "folder", "cover", "coverart", "fanart"):
        for ext in COVER_EXTS:
            cand = directory / f"{name}{ext}"
            if cand.is_file():
                return cand
    return None


def load_dir_meta(directory: Path, parent_meta: dict | None = None) -> dict:
    """
    Metadata for a folder: matching .nfo / cover, else inherit parent.
    """
    parent_meta = parent_meta or {}
    name = directory.name
    nfo = directory / f"{name}.nfo"
    # also try tvshow.nfo / movie.nfo
    if not nfo.is_file():
        for alt in ("tvshow.nfo", "movie.nfo", "season.nfo"):
            alt_p = directory / alt
            if alt_p.is_file():
                nfo = alt_p
                break
    parsed = parse_nfo(nfo) if nfo.is_file() else {"title": "", "plot": ""}
    cover = find_cover(directory, name)
    title = parsed["title"] or name
    plot = parsed["plot"] or parent_meta.get("plot") or ""
    if cover is None:
        cover = parent_meta.get("cover")
    return {
        "title": title,
        "plot": plot or "",
        "cover": cover,
        "has_own_nfo": bool(parsed["title"] or parsed["plot"]),
        "has_own_cover": cover is not None and (
            parent_meta.get("cover") is None or cover != parent_meta.get("cover")
        ),
    }


def find_file_cover(video: Path) -> Path | None:
    """Cover image matching the video basename (e.g. Pulp_Fiction.jpg)."""
    directory = video.parent
    basename = video.stem
    for ext in COVER_EXTS:
        cand = directory / f"{basename}{ext}"
        if cand.is_file():
            return cand
    return None


def load_file_meta(video: Path) -> dict:
    """Episode/movie-level NFO + cover next to the video file."""
    nfo = video.with_suffix(".nfo")
    parsed = parse_nfo(nfo) if nfo.is_file() else {"title": "", "plot": ""}
    return {
        "title": parsed.get("title") or "",
        "plot": parsed.get("plot") or "",
        "cover": find_file_cover(video),
    }


def _load_history() -> dict:
    try:
        if HISTORY_FILE.is_file():
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        print("history load error:", e)
    return {}


def _save_history(data: dict) -> None:
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print("history save error:", e)


def history_get(path: str) -> dict:
    return _load_history().get(str(path), {})


def history_update(path: str, position: float, duration: float) -> None:
    """Save resume point; mark watched near the end."""
    path = str(path)
    data = _load_history()
    pos = max(0.0, float(position or 0))
    dur = max(0.0, float(duration or 0))
    watched = bool(dur > 0 and pos >= dur * WATCHED_THRESHOLD)
    # If finished, store position at 0 so next open starts from beginning
    if watched:
        pos = 0.0
    entry = data.get(path, {})
    entry.update({
        "position": pos,
        "duration": dur,
        "watched": watched or bool(entry.get("watched")),
        "updated": int(time.time()),
    })
    # Fully finished → always watched
    if watched:
        entry["watched"] = True
    data[path] = entry
    _save_history(data)


def history_resume_position(path: str) -> float:
    """Seconds to seek to, or 0 if none / watched / too small."""
    entry = history_get(path)
    if entry.get("watched"):
        return 0.0
    pos = float(entry.get("position") or 0)
    if pos < RESUME_MIN_SECONDS:
        return 0.0
    return pos


def history_label_prefix(path: str) -> str:
    """Prefix for list rows: ✓ watched, or remaining time if in progress."""
    entry = history_get(path)
    if not entry:
        return ""
    if entry.get("watched"):
        return "✓ "
    pos = float(entry.get("position") or 0)
    if pos >= RESUME_MIN_SECONDS:
        return f"▶ {format_time(pos)} "
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
class GradientBackground(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.connect("draw", self._draw)

    def _draw(self, widget, cr):
        alloc = widget.get_allocation()
        w, h = alloc.width, alloc.height
        if w < 1 or h < 1:
            return False
        pat = cairo.LinearGradient(0, 0, 0, h)
        pat.add_color_stop_rgb(0.0, *GRAD_TOP)
        pat.add_color_stop_rgb(1.0, *GRAD_BOTTOM)
        cr.set_source(pat)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        return False


# ═══════════════════════════════════════════════════════════════════════════════
class Card(Gtk.EventBox):
    def __init__(self, title: str, subtitle: str = "", icon: str = "folder"):
        super().__init__()
        self.set_can_focus(True)
        self.set_size_request(270, 150)
        self._path = None
        self._action = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        box.set_margin_start(14)
        box.set_margin_end(14)
        box.set_margin_top(18)
        box.set_margin_bottom(18)

        if icon == "exit":
            icon_name = "application-exit-symbolic"
        elif icon == "folder":
            icon_name = "folder-symbolic"
        else:
            icon_name = "video-x-generic-symbolic"

        img = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.DIALOG)
        img.set_pixel_size(42)
        box.pack_start(img, False, False, 0)

        lbl = Gtk.Label(label=title)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        lbl.set_max_width_chars(20)
        lbl.set_justify(Gtk.Justification.CENTER)
        lbl.get_style_context().add_class("card-title")
        box.pack_start(lbl, False, False, 0)

        if subtitle:
            sub = Gtk.Label(label=subtitle)
            sub.set_ellipsize(Pango.EllipsizeMode.END)
            sub.set_max_width_chars(22)
            sub.get_style_context().add_class("card-subtitle")
            box.pack_start(sub, False, False, 0)

        self.add(box)
        self.get_style_context().add_class("card")
        self.connect("focus-in-event",
                     lambda *a: self.get_style_context().add_class("card-focused") or False)
        self.connect("focus-out-event",
                     lambda *a: self.get_style_context().remove_class("card-focused") or False)


# ═══════════════════════════════════════════════════════════════════════════════
class HorizontalScroller(Gtk.ScrolledWindow):
    def __init__(self):
        super().__init__()
        self.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.set_overlay_scrolling(False)

        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=22)
        self.box.set_halign(Gtk.Align.CENTER)
        self.box.set_valign(Gtk.Align.CENTER)
        self.box.set_margin_start(36)
        self.box.set_margin_end(36)
        self.box.set_margin_top(16)
        self.box.set_margin_bottom(16)
        self.add(self.box)

        self.cards = []
        self.current_index = 0
        self._anim = None

    def clear(self):
        for c in self.box.get_children():
            self.box.remove(c)
        self.cards.clear()
        self.current_index = 0

    def add_card(self, card: Card):
        self.box.pack_start(card, False, False, 0)
        self.cards.append(card)

    def focus_index(self, idx: int, animate: bool = True):
        if not self.cards:
            return
        idx = max(0, min(idx, len(self.cards) - 1))
        self.current_index = idx
        card = self.cards[idx]
        card.grab_focus()

        hadj = self.get_hadjustment()
        if not hadj:
            return
        alloc = card.get_allocation()
        target = alloc.x - (self.get_allocated_width() - alloc.width) / 2
        target = max(0, min(target, hadj.get_upper() - hadj.get_page_size()))

        if not animate or abs(target - hadj.get_value()) < 3:
            hadj.set_value(target)
            return

        if self._anim:
            GLib.source_remove(self._anim)
        start = hadj.get_value()
        t0 = GLib.get_monotonic_time()

        def tick():
            t = min(1.0, (GLib.get_monotonic_time() - t0) / (ANIM_MS * 1000))
            ease = 1.0 - (1.0 - t) ** 3
            hadj.set_value(start + (target - start) * ease)
            if t >= 1.0:
                self._anim = None
                return False
            return True

        self._anim = GLib.timeout_add(16, tick)


# ═══════════════════════════════════════════════════════════════════════════════
class ListRow(Gtk.EventBox):
    """One full-width row in the vertical folder list (full filename visible)."""
    def __init__(self, title: str, kind: str = "video"):
        super().__init__()
        self.set_can_focus(True)
        self._path = None
        self._kind = kind  # "folder" | "video"

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(10)
        box.set_margin_bottom(10)

        icon_name = ("folder-symbolic" if kind == "folder"
                     else "video-x-generic-symbolic")
        img = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.LARGE_TOOLBAR)
        img.set_pixel_size(28)
        box.pack_start(img, False, False, 0)

        lbl = Gtk.Label(label=title, xalign=0)
        lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        lbl.set_hexpand(True)
        lbl.get_style_context().add_class("list-row-title")
        box.pack_start(lbl, True, True, 0)

        self.add(box)
        self.get_style_context().add_class("list-row")
        self.connect("focus-in-event",
                     lambda *a: self.get_style_context().add_class("list-row-focused") or False)
        self.connect("focus-out-event",
                     lambda *a: self.get_style_context().remove_class("list-row-focused") or False)


class VerticalList(Gtk.ScrolledWindow):
    """Vertical list of ListRows – navigate with Up/Down."""
    def __init__(self):
        super().__init__()
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_overlay_scrolling(False)

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.box.set_halign(Gtk.Align.FILL)
        self.box.set_valign(Gtk.Align.START)
        self.box.set_margin_start(48)
        self.box.set_margin_end(48)
        self.box.set_margin_top(8)
        self.box.set_margin_bottom(24)
        self.add(self.box)

        self.rows = []
        self.current_index = 0

    def clear(self):
        for c in self.box.get_children():
            self.box.remove(c)
        self.rows.clear()
        self.current_index = 0

    def add_row(self, row: ListRow):
        self.box.pack_start(row, False, False, 0)
        self.rows.append(row)

    def focus_index(self, idx: int):
        if not self.rows:
            return
        idx = max(0, min(idx, len(self.rows) - 1))
        self.current_index = idx
        row = self.rows[idx]
        row.grab_focus()
        # Keep the focused row visible
        adj = self.get_vadjustment()
        if adj:
            alloc = row.get_allocation()
            page = adj.get_page_size()
            val = adj.get_value()
            if alloc.y < val:
                adj.set_value(alloc.y)
            elif alloc.y + alloc.height > val + page:
                adj.set_value(alloc.y + alloc.height - page)


# ═══════════════════════════════════════════════════════════════════════════════
class TVPlayer(Gtk.Window):
    def __init__(self, application=None):
        super().__init__(title="CouchMPV", application=application)
        self.set_default_size(1280, 720)
        self.fullscreen()
        self.connect("delete-event", self._on_delete)
        self.connect("key-press-event", self._on_key)

        self.mode = "browser"
        self.path_stack = []
        self.current_dir = None
        self.video_path = None
        self.player = None
        self.paused = False
        self._poll_id = None
        self._background = False
        self._resume_video_on_restore = False
        self._folder_meta = {}
        self._meta_stack = []
        # Hold-to-seek (arrow keys)
        self._seek_hold_id = None
        self._seek_dir = 0

        self._install_css()

        self.overlay = Gtk.Overlay()
        self.add(self.overlay)

        self.bg = GradientBackground()
        self.overlay.add(self.bg)

        browser_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        browser_box.set_valign(Gtk.Align.FILL)

        self.browser_title = Gtk.Label(label="Directories")
        self.browser_title.get_style_context().add_class("main-title")
        self.browser_title.set_margin_top(24)
        self.browser_title.set_margin_bottom(12)
        self.browser_title.set_margin_start(24)
        self.browser_title.set_halign(Gtk.Align.START)
        browser_box.pack_start(self.browser_title, False, False, 0)

        self.browse_stack = Gtk.Stack()
        self.browse_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.browse_stack.set_transition_duration(150)

        # Root: horizontal cards
        root_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root_wrap.set_valign(Gtk.Align.CENTER)
        self.scroller = HorizontalScroller()
        root_wrap.pack_start(self.scroller, True, True, 0)
        self.browse_stack.add_named(root_wrap, "root")

        # Folder view: cover + description | episode list
        media_page = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        media_page.set_margin_start(24)
        media_page.set_margin_end(24)
        media_page.set_margin_bottom(20)

        self.left_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.left_col.set_valign(Gtk.Align.FILL)
        self.left_col.set_halign(Gtk.Align.START)
        self.left_col.set_vexpand(True)

        self.cover_frame = Gtk.Frame()
        self.cover_frame.set_shadow_type(Gtk.ShadowType.NONE)
        self.cover_frame.set_halign(Gtk.Align.CENTER)
        self.cover_frame.get_style_context().add_class("cover-frame")
        # Placeholder until first meta load
        self.cover_frame.add(Gtk.Image())
        self.left_col.pack_start(self.cover_frame, False, False, 0)

        self.desc_scroll = Gtk.ScrolledWindow()
        self.desc_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.desc_scroll.set_min_content_height(90)
        # No max height — use all vertical space under the cover
        self.desc_scroll.set_propagate_natural_height(True)
        self.desc_scroll.set_vexpand(True)
        self.desc_scroll.get_style_context().add_class("desc-frame")
        self.desc_label = Gtk.Label(label="No description.", xalign=0, yalign=0)
        self.desc_label.set_line_wrap(True)
        self.desc_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.desc_label.set_max_width_chars(40)
        self.desc_label.set_valign(Gtk.Align.START)
        self.desc_label.set_margin_start(10)
        self.desc_label.set_margin_end(10)
        self.desc_label.set_margin_top(10)
        self.desc_label.set_margin_bottom(10)
        self.desc_label.get_style_context().add_class("desc-text")
        self.desc_scroll.add(self.desc_label)
        self.left_col.pack_start(self.desc_scroll, True, True, 0)

        media_page.pack_start(self.left_col, False, True, 0)

        self.vlist = VerticalList()
        media_page.pack_start(self.vlist, True, True, 0)
        self.browse_stack.add_named(media_page, "list")

        browser_box.pack_start(self.browse_stack, True, True, 0)
        self.overlay.add_overlay(browser_box)

        self._show_root()
        self.add_events(Gdk.EventMask.KEY_PRESS_MASK)
        self.show_all()
        self.scroller.focus_index(0, animate=False)

    # ── CSS ─────────────────────────────────────────────────────────────────
    def _install_css(self):
        css = b"""
        .main-title {
            font-size: 30px;
            font-weight: bold;
            color: #e8d8ff;
            text-shadow: 0 2px 10px rgba(0,0,0,0.55);
        }
        .card {
            background-color: rgba(55, 25, 85, 0.78);
            border-radius: 14px;
            border: 2px solid transparent;
        }
        .card-focused {
            background-color: rgba(115, 55, 175, 0.92);
            border: 3px solid #c48fff;
        }
        .card-title {
            font-size: 18px;
            font-weight: bold;
            color: #f2eaff;
        }
        .card-subtitle {
            font-size: 13px;
            color: #b0a0c8;
        }
        .list-row {
            background-color: rgba(40, 18, 70, 0.55);
            border-radius: 10px;
            border: 2px solid transparent;
        }
        .list-row-focused {
            background-color: rgba(115, 55, 175, 0.92);
            border: 2px solid #c48fff;
        }
        .list-row-title {
            font-size: 16px;
            color: #f2eaff;
        }
        .cover-frame {
            background-color: transparent;
            border: 3px solid #c48fff;
            border-radius: 12px;
            padding: 0;
        }
        .desc-frame {
            background-color: rgba(115, 55, 175, 0.75);
            border-radius: 12px;
            border: 2px solid rgba(196, 143, 255, 0.45);
        }
        .desc-text {
            font-size: 14px;
            color: #f2eaff;
        }
        .no-cover-label {
            font-size: 26px;
            font-weight: bold;
            color: rgba(255,255,255,0.45);
        }
        .no-cover-box {
            background-color: rgba(90, 50, 160, 0.85);
            border-radius: 10px;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    # ── Browser population ──────────────────────────────────────────────────
    def _set_cover(self, cover_path):
        """Show cover preserving aspect ratio; frame is a tight border around it."""
        child = self.cover_frame.get_child()
        if child:
            self.cover_frame.remove(child)

        disp_w, disp_h = COVER_FALLBACK_W, COVER_FALLBACK_H

        if cover_path and Path(cover_path).is_file():
            try:
                # Load full size first to know aspect, then scale to max box
                full = GdkPixbuf.Pixbuf.new_from_file(str(cover_path))
                ow, oh = full.get_width(), full.get_height()
                if ow < 1 or oh < 1:
                    raise ValueError("empty image")
                scale = min(COVER_MAX_W / ow, COVER_MAX_H / oh, 1.0)
                # Always fit within max; upscale small covers slightly for TV distance
                if scale < 1.0 or (ow < COVER_MAX_W * 0.6 and oh < COVER_MAX_H * 0.6):
                    scale = min(COVER_MAX_W / ow, COVER_MAX_H / oh)
                disp_w = max(1, int(round(ow * scale)))
                disp_h = max(1, int(round(oh * scale)))
                pix = full.scale_simple(
                    disp_w, disp_h, GdkPixbuf.InterpType.BILINEAR
                )
                pix = pixbuf_with_rounded_corners(pix, COVER_CORNER_RADIUS)
                img = Gtk.Image.new_from_pixbuf(pix)
                self.cover_frame.add(img)
                self.cover_frame.show_all()
                self.desc_scroll.set_size_request(disp_w, -1)
                self.desc_label.set_max_width_chars(max(20, disp_w // 8))
                return
            except Exception as e:
                print("cover load error:", e)

        placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        placeholder.set_size_request(COVER_FALLBACK_W, COVER_FALLBACK_H)
        placeholder.set_valign(Gtk.Align.CENTER)
        placeholder.set_halign(Gtk.Align.CENTER)
        placeholder.get_style_context().add_class("no-cover-box")
        lbl = Gtk.Label(label="No Cover")
        lbl.get_style_context().add_class("no-cover-label")
        placeholder.pack_start(lbl, True, True, 0)
        self.cover_frame.add(placeholder)
        self.cover_frame.show_all()
        self.desc_scroll.set_size_request(COVER_FALLBACK_W, -1)
        self.desc_label.set_max_width_chars(32)

    def _set_description(self, text: str):
        t = text.strip() if text and text.strip() else "No description."
        self.desc_label.set_text(t)
        # Recompute height so long plots use the expanded area
        self.desc_label.queue_resize()
        self.desc_scroll.queue_resize()

    def _apply_folder_meta(self, meta: dict):
        self._folder_meta = meta
        self.browser_title.set_text(meta.get("title") or "Folder")
        self._set_cover(meta.get("cover"))
        self._set_description(meta.get("plot") or "")

    def _on_row_focus(self, row, *_args):
        """Update cover + description for the focused row (file overrides folder)."""
        folder_title = self._folder_meta.get("title") or (
            folder_name(self.current_dir) if self.current_dir else "Folder"
        )
        folder_plot = self._folder_meta.get("plot") or ""
        folder_cover = self._folder_meta.get("cover")

        if not row._path:
            self.browser_title.set_text(folder_title)
            self._set_cover(folder_cover)
            self._set_description(folder_plot)
            return False

        p = Path(row._path)
        if p.is_file() and is_video(p):
            ep = load_file_meta(p)
            # Per-file cover/nfo win over folder; otherwise fall back
            title = ep.get("title") or folder_title
            plot = ep.get("plot") or folder_plot
            cover = ep.get("cover") or folder_cover
            self.browser_title.set_text(title)
            self._set_cover(cover)
            self._set_description(plot)
        else:
            # Subfolder row: show parent folder meta (own meta loads on enter)
            self.browser_title.set_text(folder_title)
            self._set_cover(folder_cover)
            self._set_description(folder_plot)
        return False

    def _show_root(self, focus_path: str = None):
        global DIRECTORIES, MPV_USER_OPTS
        DIRECTORIES, MPV_USER_OPTS = load_user_config()

        self.path_stack = []
        self._meta_stack = []
        self._folder_meta = {}
        self.current_dir = None
        self.browser_title.set_text("Directories")
        self.scroller.clear()
        self.vlist.clear()

        for d in DIRECTORIES:
            card = Card(folder_name(d), d, icon="folder")
            card._path = d
            card.connect("button-press-event",
                         lambda w, e, p=d: self._enter_path(p))
            self.scroller.add_card(card)

        exit_card = Card("Exit", "Close the player", icon="exit")
        exit_card._action = "exit"
        exit_card.connect("button-press-event", lambda w, e: self._quit())
        self.scroller.add_card(exit_card)

        self.scroller.show_all()
        self.browse_stack.set_visible_child_name("root")
        self.mode = "browser"
        if self.scroller.cards:
            focus_idx = 0
            if focus_path:
                for i, card in enumerate(self.scroller.cards):
                    if getattr(card, "_path", None) == focus_path:
                        focus_idx = i
                        break
            GLib.idle_add(lambda idx=focus_idx: self.scroller.focus_index(idx, animate=False))

    def _fill_vertical_list(self, path: str, focus_path: str = None):
        """Populate media list + cover/description for a folder."""
        self.vlist.clear()
        p = Path(path)

        parent_meta = self._meta_stack[-1] if self._meta_stack else {}
        meta = load_dir_meta(p, parent_meta=parent_meta)
        self._apply_folder_meta(meta)

        try:
            entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except (PermissionError, OSError):
            entries = []

        folders = [e for e in entries if is_dir(e)]
        videos = [e for e in entries if is_video(e)]

        if not folders and not videos:
            row = ListRow("(empty folder)", kind="folder")
            self.vlist.add_row(row)
        else:
            for f in folders:
                row = ListRow(f.name, kind="folder")
                row._path = str(f)
                row.connect("button-press-event",
                            lambda w, e, pth=str(f): self._enter_path(pth))
                row.connect("focus-in-event", self._on_row_focus)
                self.vlist.add_row(row)
            for v in videos:
                prefix = history_label_prefix(str(v))
                row = ListRow(prefix + v.name, kind="video")
                row._path = str(v)
                row.connect("button-press-event",
                            lambda w, e, pth=str(v): self._start_playback(pth))
                row.connect("focus-in-event", self._on_row_focus)
                self.vlist.add_row(row)

        self.vlist.show_all()
        self.browse_stack.set_visible_child_name("list")
        self.mode = "browser"

        focus_idx = 0
        if focus_path:
            for i, row in enumerate(self.vlist.rows):
                if row._path == focus_path:
                    focus_idx = i
                    break
        if self.vlist.rows:
            GLib.idle_add(lambda idx=focus_idx: self.vlist.focus_index(idx))

    def _enter_path(self, path: str):
        p = Path(path)
        if not p.is_dir():
            return
        self.path_stack.append(self.current_dir)
        if self.current_dir is not None:
            self._meta_stack.append(dict(self._folder_meta))
        self.current_dir = path
        self._fill_vertical_list(path)

    def _rebuild_folder(self, path: str, focus_path: str = None):
        self.current_dir = path
        self._fill_vertical_list(path, focus_path=focus_path)

    # ── Playback (standalone fullscreen mpv – works on Wayland) ─────────────
    def _start_playback(self, path: str):
        global MPV_USER_OPTS
        _, MPV_USER_OPTS = load_user_config()

        self.video_path = path
        self.mode = "player"
        self.hide()

        try:
            if self.player is not None:
                try:
                    self.player.terminate()
                except Exception:
                    pass
                self.player = None

            mpv_kwargs = dict(
                fullscreen=True,
                keep_open="yes",
                stop_screensaver=True,
                input_default_bindings=False,
                input_vo_keyboard=True,
                input_media_keys=True,
                # Key-repeat while holding arrows (ms delay, repeats/sec)
                input_ar_delay=300,
                input_ar_rate=6,
                osc=False,
                osd_level=1,
                osd_font_size=32,
                osd_color="#D9FFFFFF",
                osd_border_color="#FF49007D",
                osd_shadow_offset=2,
                osd_bar_align_y=0.92,
                osd_duration=5000,
                osd_status_msg="${time-pos} / ${duration} | ${filename/no-ext}\n ${width}x${height}${?video-params/interlaced:i} | ${video-format} | ${container-fps}fps\n ${audio-codec-name:disabled} ${audio-params/hr-channels: } | ${current-tracks/audio/lang:disabled}\n ${current-tracks/sub/codec:disabled} | ${current-tracks/sub/lang:disabled}",
                cursor_autohide="always",
                terminal=False,
                msg_level="all=error",
                hwdec="auto",
                force_window="immediate",
            )
            for key, val in MPV_USER_OPTS.items():
                kw = key.replace("-", "_")
                if val.lower() in ("yes", "true", "on"):
                    mpv_kwargs[kw] = True
                elif val.lower() in ("no", "false", "off"):
                    mpv_kwargs[kw] = False
                else:
                    try:
                        mpv_kwargs[kw] = float(val) if "." in val else int(val)
                    except ValueError:
                        mpv_kwargs[kw] = val

            self.player = mpv.MPV(**mpv_kwargs)
            p = self.player

            # ── Pause observer → rich OSD ────────────────────────────────
            @p.property_observer("pause")
            def on_pause(_name, value):
                self.paused = bool(value)
                if value:
                    GLib.idle_add(self._show_pause_osd)

            # ── Seek: mpv native "repeatable" so hold keeps seeking ──────
            try:
                p.command(
                    "define-section",
                    "couchseek",
                    (
                        f"LEFT repeatable seek -{SKIP_SECONDS}\n"
                        f"RIGHT repeatable seek {SKIP_SECONDS}\n"
                        f"h repeatable seek -{SKIP_SECONDS}\n"
                        f"l repeatable seek {SKIP_SECONDS}\n"
                    ),
                    "force",
                )
                p.command("enable-section", "couchseek")
            except Exception as e:
                print("seek section error:", e)
                @p.on_key_press("LEFT")
                def _left():
                    self._mpv_seek(-SKIP_SECONDS)

                @p.on_key_press("RIGHT")
                def _right():
                    self._mpv_seek(SKIP_SECONDS)

                @p.on_key_press("h")
                def _h():
                    self._mpv_seek(-SKIP_SECONDS)

                @p.on_key_press("l")
                def _l():
                    self._mpv_seek(SKIP_SECONDS)

            @p.property_observer("seeking")
            def on_seeking(_name, value):
                if value is False and self.player:
                    try:
                        self.player.command("show-progress")
                    except Exception:
                        pass

            # ── Play / Pause ─────────────────────────────────────────────
            @p.on_key_press("ENTER")
            def _enter():
                self._mpv_cycle_pause()

            @p.on_key_press("KP_ENTER")
            def _kp_enter():
                self._mpv_cycle_pause()

            @p.on_key_press("SPACE")
            def _space():
                self._mpv_cycle_pause()

            @p.on_key_press("p")
            def _p():
                self._mpv_cycle_pause()

            # ── Back / Stop (same universal set as the browser) ───────────
            def _bind_stop(keyname):
                try:
                    p.on_key_press(keyname)(lambda: self._mpv_stop())
                except Exception:
                    pass

            for _bk in (
                "ESC", "BS", "BACKSPACE",
                "BROWSER_BACK", "XF86Back", "GO_BACK", "BACK",
                "STOP", "EXIT", "CLOSE",
            ):
                _bind_stop(_bk)

            @p.on_key_press("q")
            def _q():
                self._mpv_stop()

            # ── Home → background (same as Android TV Home) ───────────────
            def _bind_home(keyname):
                try:
                    p.on_key_press(keyname)(lambda: self._mpv_home())
                except Exception:
                    pass

            for _hk in (
                "HOMEPAGE", "XF86HomePage", "BROWSER_HOME",
                "HOME", "XF86Go", "GO",
            ):
                _bind_home(_hk)

            # ── Tracks ───────────────────────────────────────────────────
            @p.on_key_press("DOWN")
            def _down():
                self._mpv_cycle_audio()

            @p.on_key_press("UP")
            def _up():
                self._mpv_cycle_sub()

            @p.on_key_press("a")
            def _a():
                self._mpv_cycle_audio()

            @p.on_key_press("s")
            def _s():
                self._mpv_cycle_sub()

            # ── Info OSD ─────────────────────────────────────────────────
            @p.on_key_press("i")
            def _i():
                self._show_pause_osd()

            @p.on_key_press("m")
            def _m():
                self._show_pause_osd()

            print(f"Playing: {path}")
            p.play(path)
            self.paused = False

            def _apply_prefs():
                if not self.player:
                    return False
                try:
                    # PLAYER SETTINGS
                    # DELAY (seconds):
                    #self.player['audio-delay'] = 0.15
                    self.player['sub-delay'] = 0
                    # SUBTITLE:
                    #self.player['sub-scale'] = 1.8
                    #self.player['sub-pos'] = 95
                    # PICTURE
                    #self.player['brightness'] = 5
                    #self.player['contrast'] = 5
                    #self.player['gamma'] = 0
                    #self.player['saturation'] = 0
                    # example downscale (optional)
                    # self.player.vf = 'scale=1280:-2'
                except Exception as e:
                    print('prefs error:', e)
                return False

            def _resume_if_needed():
                if not self.player or not self.video_path:
                    return False
                try:
                    pos = history_resume_position(self.video_path)
                    if pos > 0:
                        self.player.seek(pos, reference="absolute")
                        self.player.command(
                            "show-text",
                            f"Resumed at {format_time(pos)}",
                            2500,
                        )
                        print(f"Resumed at {pos:.1f}s")
                except Exception as e:
                    print("resume error:", e)
                return False

            GLib.timeout_add(800, _apply_prefs)
            GLib.timeout_add(1200, _resume_if_needed)
            GLib.timeout_add(600, self._autoload_subs)

            if self._poll_id:
                GLib.source_remove(self._poll_id)
            self._poll_id = GLib.timeout_add(500, self._poll_playback)

        except Exception as e:
            print("MPV start error:", e)
            import traceback
            traceback.print_exc()
            self._return_to_browser()

    def _show_pause_osd(self):
        """Show title, resolution, codec, time, remaining and progress bar."""
        if not self.player:
            return False
        try:
            p = self.player

            # Build plain text (more reliable than ASS on some Wayland VOs)
            title = None
            try:
                title = p.media_title
            except Exception:
                pass
            if not title or title in ("No title", ""):
                title = Path(self.video_path).name if self.video_path else "Unknown"

            parts = []
            try:
                w, h = p.width, p.height
                if w and h:
                    parts.append(f"{w}x{h}")
            except Exception:
                pass
            try:
                fmt = p.file_format
                if fmt:
                    parts.append(str(fmt).upper())
            except Exception:
                if self.video_path:
                    parts.append(Path(self.video_path).suffix[1:].upper())
            try:
                codec = p.video_codec or p.video_format
                if codec:
                    parts.append(str(codec).upper())
            except Exception:
                pass
            meta = " | ".join(parts)

            pos = p.time_pos or 0
            dur = p.duration or 0
            remaining = max(0, (dur or 0) - (pos or 0))
            time_line = (
                f"{format_time(pos)} / {format_time(dur)}"
                f"  (-{format_time(remaining)})"
            )

            audio_info = ""
            sub_info = ""
            try:
                for t in (p.track_list or []):
                    if t.get("type") == "audio" and t.get("selected"):
                        lang = t.get("lang") or ""
                        tt = t.get("title") or ""
                        audio_info = f"Audio [{t.get('id')}] {lang} {tt}".strip()
                    if t.get("type") == "sub" and t.get("selected"):
                        lang = t.get("lang") or ""
                        tt = t.get("title") or ""
                        sub_info = f"Sub [{t.get('id')}] {lang} {tt}".strip()
            except Exception:
                pass

            # Use mpv property expansion where possible + our computed strings
            # Duration for show-text is in milliseconds
            lines = [str(title)]
            if meta:
                lines.append(meta)
            lines.append(time_line)
            if audio_info:
                lines.append(audio_info)
            if sub_info:
                lines.append(sub_info)
            lines.append("LEFT/RIGHT seek | OK pause | UP sub | DOWN audio | BACK quit")

            text = "\n".join(lines)

            # Primary: multi-line OSD
            try:
                p.command("show-text", text, 5000)
            except Exception:
                # Fallback: single line (some VOs dislike newlines)
                p.command("show-text", "  |  ".join(lines[:4]), 5000)

            p.command("show-progress")
        except Exception as e:
            print("OSD error:", e)
        return False

    def _mpv_seek(self, seconds):
        if self.player:
            try:
                self.player.seek(seconds, reference="relative")
                self.player.command("show-progress")
            except Exception:
                pass
        return False

    def _seek_hold_start(self, direction):
        """First seek on press; timer continues until release (or timeout safety)."""
        self._seek_hold_stop()
        self._seek_dir = direction
        self._mpv_seek(direction * SKIP_SECONDS)

        # Safety: if key-up never arrives, stop after ~2s of timer seeks
        self._seek_hold_ticks = 0

        def _repeat():
            if not self.player or self._seek_dir == 0 or self.mode != "player":
                self._seek_hold_id = None
                return False
            self._seek_hold_ticks = getattr(self, "_seek_hold_ticks", 0) + 1
            if self._seek_hold_ticks > 12:  # ~2s at 160ms
                self._seek_hold_stop()
                return False
            self._mpv_seek(self._seek_dir * SKIP_SECONDS)
            return True

        # Backup repeat if mpv does not emit 'r' events (common on some builds)
        self._seek_hold_id = GLib.timeout_add(160, _repeat)
        return False

    def _seek_hold_stop(self):
        """Stop continuous seek on key release."""
        self._seek_dir = 0
        self._seek_hold_ticks = 0
        if self._seek_hold_id is not None:
            try:
                GLib.source_remove(self._seek_hold_id)
            except Exception:
                pass
            self._seek_hold_id = None
        return False

    def _mpv_cycle_pause(self):
        if self.player:
            try:
                self.player.command("cycle", "pause")
            except Exception:
                pass

    def _mpv_cycle_audio(self):
        if not self.player:
            return
        try:
            self.player.command("cycle", "audio")
            # Build a short description of the new track
            tracks = self.player.track_list or []
            msg = "Audio: (none)"
            for t in tracks:
                if t.get("type") == "audio" and t.get("selected"):
                    lang = t.get("lang") or ""
                    title = t.get("title") or ""
                    msg = f"Audio: [{t.get('id')}] {lang} {title}".strip()
                    break
            self.player.command("show-text", msg, 2000)
        except Exception:
            pass

    def _mpv_cycle_sub(self):
        if not self.player:
            return
        try:
            self.player.command("cycle", "sub")
            tracks = self.player.track_list or []
            msg = "Subtitles: Off"
            for t in tracks:
                if t.get("type") == "sub" and t.get("selected"):
                    lang = t.get("lang") or ""
                    title = t.get("title") or ""
                    msg = f"Sub: [{t.get('id')}] {lang} {title}".strip()
                    break
            self.player.command("show-text", msg, 2000)
        except Exception:
            pass

    def _mpv_stop(self):
        """Called from mpv key binding – schedule back to GTK main loop."""
        GLib.idle_add(self._seek_hold_stop)
        GLib.idle_add(self._return_to_browser)

    def _mpv_home(self):
        """Home while video has focus – background the app."""
        GLib.idle_add(self._go_home)

    def _poll_playback(self):
        """Return to browser when the file ends (keep-open leaves it paused at end)."""
        if self.player is None or self.mode != "player":
            self._poll_id = None
            return False
        try:
            # eof-reached becomes True when the file ends
            if self.player.eof_reached:
                # Treat natural end as fully watched
                try:
                    dur = float(self.player.duration or 0)
                    history_update(self.video_path, dur, dur)
                except Exception:
                    pass
                self._return_to_browser()
                return False
        except Exception:
            pass
        return True

    def _autoload_subs(self):
        if not self.player or not self.video_path:
            return False
        folder = Path(self.video_path).parent
        stem = Path(self.video_path).stem.lower()
        try:
            candidates = []
            for f in folder.iterdir():
                if f.suffix.lower() in SUBTITLE_EXTENSIONS:
                    if f.stem.lower() == stem:
                        candidates.insert(0, f)
                    else:
                        candidates.append(f)
            if candidates:
                self.player.sub_add(str(candidates[0]))
        except Exception:
            pass
        return False

    def _save_current_progress(self):
        """Write resume / watched state for the current video."""
        if not self.player or not self.video_path:
            return
        try:
            pos = float(self.player.time_pos or 0)
            dur = float(self.player.duration or 0)
            history_update(self.video_path, pos, dur)
            print(f"Saved progress: {self.video_path} @ {pos:.1f}s / {dur:.1f}s")
        except Exception as e:
            print("save progress error:", e)

    def _return_to_browser(self):
        if self._poll_id:
            try:
                GLib.source_remove(self._poll_id)
            except Exception:
                pass
            self._poll_id = None

        self._save_current_progress()

        # Show browser first to avoid desktop flash
        self.mode = "browser"
        if self.current_dir:
            self._rebuild_folder(self.current_dir, focus_path=self.video_path)
        else:
            self._show_root()
        self.show()
        self.present()
        self.fullscreen()

        def _teardown_mpv():
            if self.player:
                try:
                    self.player.stop()
                except Exception:
                    pass
                try:
                    self.player.terminate()
                except Exception:
                    pass
                self.player = None
            return False

        GLib.idle_add(_teardown_mpv)
        return False

    # ── Key handling (browser only – player keys are handled by mpv) ────────
    def _on_key(self, widget, event):
        if self.mode == "player":
            # Keys should be going to the mpv window; ignore here
            return False

        keyval = event.keyval
        keycode = event.hardware_keycode

        if keyval in HOME_KEYVALS:
            self._go_home()
            return True

        if keyval in BACK_KEYVALS or keycode in BACK_KEYCODES:
            self._go_back()
            return True

        return self._keys_browser(keyval)

    def _keys_browser(self, keyval):
        # Root view: horizontal cards → Left / Right
        if self.current_dir is None:
            sc = self.scroller
            if keyval in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
                sc.focus_index(sc.current_index - 1)
                return True
            if keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right):
                sc.focus_index(sc.current_index + 1)
                return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
                if sc.cards:
                    card = sc.cards[sc.current_index]
                    if getattr(card, "_action", None) == "exit":
                        self._quit()
                    elif card._path:
                        p = Path(card._path)
                        if p.is_dir():
                            self._enter_path(card._path)
                        elif is_video(p):
                            self._start_playback(card._path)
                return True
            return False

        # Inside a folder: vertical list → Up / Down
        vl = self.vlist
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            vl.focus_index(vl.current_index - 1)
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            vl.focus_index(vl.current_index + 1)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            if vl.rows:
                row = vl.rows[vl.current_index]
                if row._path:
                    p = Path(row._path)
                    if p.is_dir():
                        self._enter_path(row._path)
                    elif is_video(p):
                        self._start_playback(row._path)
            return True
        return False

    def _go_back(self):
        if self.mode == "player":
            self._return_to_browser()
            return
        if self.path_stack:
            # Remember the folder we're leaving so selection stays on it
            left_path = self.current_dir
            parent = self.path_stack.pop()
            if self._meta_stack:
                self._meta_stack.pop()
            if parent is None:
                self._show_root(focus_path=left_path)
            else:
                self._rebuild_folder(parent, focus_path=left_path)

    def _on_delete(self, *args):
        """Window manager close → behave like Home (stay in background)."""
        self._go_home()
        return True  # cancel destroy

    def _go_home(self):
        """
        Android-style Home: keep process alive, hide UI.
        If a video was playing, save progress and resume it on next activate.
        """
        if self._background:
            return False

        was_playing = (self.mode == "player" and self.video_path)
        self._resume_video_on_restore = bool(was_playing)

        if was_playing:
            self._save_current_progress()
            if self._poll_id:
                try:
                    GLib.source_remove(self._poll_id)
                except Exception:
                    pass
                self._poll_id = None
            if self.player:
                try:
                    self.player.stop()
                except Exception:
                    pass
                try:
                    self.player.terminate()
                except Exception:
                    pass
                self.player = None
            self.mode = "browser"

        self._background = True
        self.hide()
        print("Home: app in background"
              + (" (will resume video)" if was_playing else ""))
        return False

    def _restore_from_home(self):
        """Called when launcher opens the player again (single-instance activate)."""
        self._background = False
        self.show()
        self.present()
        self.fullscreen()

        resume_path = None
        if self._resume_video_on_restore and self.video_path:
            resume_path = self.video_path
            self._resume_video_on_restore = False

        if resume_path and Path(resume_path).is_file():
            # Resume playback (seek comes from history)
            GLib.idle_add(lambda: self._start_playback(resume_path) or False)
        elif self.current_dir:
            self._rebuild_folder(self.current_dir, focus_path=self.video_path)
        else:
            self._show_root()
        return False

    def _quit(self, *args):
        """Full quit (Exit card) – terminate process."""
        if self.player:
            self._save_current_progress()
            try:
                self.player.terminate()
            except Exception:
                pass
            self.player = None
        app = self.get_application()
        if app is not None:
            app.quit()
        else:
            Gtk.main_quit()


# ═══════════════════════════════════════════════════════════════════════════════
class TVApp(Gtk.Application):
    """Single-instance application – second launch reactivates the same process."""

    def __init__(self):
        super().__init__(
            application_id="org.couchmpv.player",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.win = None

    def do_activate(self):
        if self.win is None:
            self.win = TVPlayer(application=self)
            self.win.show_all()
            if self.win.scroller.cards:
                self.win.scroller.focus_index(0, animate=False)
        else:
            # Already running → bring back from Home / background
            self.win._restore_from_home()


if __name__ == "__main__":
    TVApp().run(None)
