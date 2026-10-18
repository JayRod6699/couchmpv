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
import json
import time
import locale
from pathlib import Path

# Required by libmpv – must be set before creating any MPV instance
locale.setlocale(locale.LC_NUMERIC, "C")

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango, Gio

import cairo
import mpv

# Watch history (resume + watched marks)
HISTORY_DIR = Path(os.path.expanduser("~/.config/couchmpv"))
HISTORY_FILE = HISTORY_DIR / "history.json"
WATCHED_THRESHOLD = 0.90   # mark watched at >= 90% of duration
RESUME_MIN_SECONDS = 5     # ignore tiny positions

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION – add as many root directories as you like
# ═══════════════════════════════════════════════════════════════════════════════
DIRECTORIES = [
    os.path.expanduser("~/"),
    os.path.expanduser("~/Videos"),
    
    # Examples:
    # os.path.expanduser("~/Movies"),
    # "/mnt/hdd/films",
    # "/media/usb/videos",
]

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
        super().__init__(title="TV MPV Player", application=application)
        self.set_default_size(1280, 720)
        self.fullscreen()
        # Hide on close request from WM; real quit via Exit card / SIGTERM
        self.connect("delete-event", self._on_delete)
        self.connect("key-press-event", self._on_key)

        self.mode = "browser"          # browser | player
        self.path_stack = []
        self.current_dir = None
        self.video_path = None
        self.player = None
        self.paused = False
        self._poll_id = None
        # Home / background state
        self._background = False
        self._resume_video_on_restore = False

        self._install_css()

        self.overlay = Gtk.Overlay()
        self.add(self.overlay)

        self.bg = GradientBackground()
        self.overlay.add(self.bg)

        # Browser UI: title + either horizontal root cards OR vertical folder list
        browser_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        browser_box.set_valign(Gtk.Align.FILL)

        self.browser_title = Gtk.Label(label="Directories")
        self.browser_title.get_style_context().add_class("main-title")
        self.browser_title.set_margin_top(36)
        self.browser_title.set_margin_bottom(20)
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

        # Inside a folder: vertical full-name list
        self.vlist = VerticalList()
        self.browse_stack.add_named(self.vlist, "list")

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
            font-size: 18px;
            color: #f2eaff;
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
    def _show_root(self):
        self.path_stack = []
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
            GLib.idle_add(lambda: self.scroller.focus_index(0, animate=False))

    def _fill_vertical_list(self, path: str, focus_path: str = None):
        """Populate the vertical list for a folder (full filenames)."""
        self.vlist.clear()
        p = Path(path)
        try:
            entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except (PermissionError, OSError):
            entries = []

        folders = [e for e in entries if is_dir(e)]
        videos  = [e for e in entries if is_video(e)]

        if not folders and not videos:
            row = ListRow("(empty folder)", kind="folder")
            self.vlist.add_row(row)
        else:
            for f in folders:
                row = ListRow(f.name, kind="folder")
                row._path = str(f)
                row.connect("button-press-event",
                            lambda w, e, pth=str(f): self._enter_path(pth))
                self.vlist.add_row(row)
            for v in videos:
                prefix = history_label_prefix(str(v))
                row = ListRow(prefix + v.name, kind="video")
                row._path = str(v)
                row.connect("button-press-event",
                            lambda w, e, pth=str(v): self._start_playback(pth))
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
        self.current_dir = path
        self.browser_title.set_text(folder_name(path))
        self._fill_vertical_list(path)

    def _rebuild_folder(self, path: str, focus_path: str = None):
        self.current_dir = path
        self.browser_title.set_text(folder_name(path))
        self._fill_vertical_list(path, focus_path=focus_path)

    # ── Playback (standalone fullscreen mpv – works on Wayland) ─────────────
    def _start_playback(self, path: str):
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

            self.player = mpv.MPV(
                fullscreen=True,
                keep_open="yes",
                stop_screensaver=True,
                input_default_bindings=False,
                input_vo_keyboard=True,
                input_media_keys=True,
                osc=False,
                osd_level=1,
                osd_font_size=32,
                osd_color="#D9FFFFFF",
                osd_border_color="#FF49007D",
                osd_shadow_offset=2,
                osd_bar_align_y=0.92,
                osd_duration=5000,
                osd_status_msg="${time-pos} / ${duration} | ${filename/no-ext}\n ${width}x${height}${?video-params/interlaced:i} | ${video-format} | ${container-fps}fps\n ${audio-codec-name:disabled} ${audio-params/hr-channels: } | ${current-tracks/audio/lang:disabled}\n ${current-tracks/sub/codec:disabled} | ${current-tracks/sub/lang:disabled}",
                #save_position_on_quit=True,
                # optional explicit folder:
                #watch_later_directory=os.path.expanduser("~/.config/mpv/watch_later"),
                cursor_autohide="always",
                terminal=False,
                msg_level="all=error",
                hwdec="auto",
                force_window="immediate",
            )

            p = self.player

            # ── Pause observer → rich OSD ────────────────────────────────
            @p.property_observer("pause")
            def on_pause(_name, value):
                self.paused = bool(value)
                if value:
                    GLib.idle_add(self._show_pause_osd)

            # ── Seek ─────────────────────────────────────────────────────
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
                # Brief progress bar after seek
                self.player.command("show-progress")
            except Exception:
                pass

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

        # Save position before tearing down mpv
        self._save_current_progress()

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
        self.show()
        self.present()
        self.fullscreen()

        if self.current_dir:
            # Keep the video that was playing selected in the list
            self._rebuild_folder(self.current_dir, focus_path=self.video_path)
        else:
            self._show_root()
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
            parent = self.path_stack.pop()
            if parent is None:
                self._show_root()
            else:
                self._rebuild_folder(parent)

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
            application_id="org.tv.mpvplayer",
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
