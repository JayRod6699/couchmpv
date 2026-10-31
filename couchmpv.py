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
import hashlib
import locale
import subprocess
import threading
import math
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
# Layout numbers below are tuned for target-resolution (default 1366×768).
# At runtime they are multiplied by ui_scale = window / target.
POSTER_W = 300
POSTER_H = 450  # 2:3
COVER_CORNER_RADIUS = 12
# Banner fixed 379:70 aspect (width = full list/right column)
BANNER_ASPECT_W = 379
BANNER_ASPECT_H = 70
BANNER_MAX_W = 1600  # safety cap only; normally matches right-column width
# Default solid background
BG_COLOR = (0x17 / 255.0, 0x10 / 255.0, 0x1e / 255.0)  # #17101e

# Layout constants (POSTER_W etc.) are designed for this fixed baseline.
# target-resolution in config is your screen size → higher = larger UI.
_DESIGN_W = 1366
_DESIGN_H = 768
_TARGET_W = 1366
_TARGET_H = 768
_UI_SCALE = 1.0


def parse_resolution(text: str, default=(1366, 768)):
    """Parse '1920x1080' / '1920×1080' / '1920 1080' → (w, h)."""
    if not text:
        return default
    s = str(text).strip().lower().replace("×", "x").replace("*", "x")
    s = s.replace(",", "x").replace(" ", "x")
    parts = [p for p in s.split("x") if p.strip()]
    if len(parts) >= 2:
        try:
            w, h = int(parts[0]), int(parts[1])
            if w >= 320 and h >= 240:
                return w, h
        except ValueError:
            pass
    return default


def ui_scale() -> float:
    return max(0.5, min(4.0, float(_UI_SCALE)))


def S(px) -> int:
    """Scale a design pixel value for the current UI scale."""
    return max(1, int(round(float(px) * ui_scale())))


def pixbuf_crop_to_aspect(pixbuf, target_w, target_h):
    """Center-crop to target aspect ratio, then scale to target_w x target_h."""
    ow, oh = pixbuf.get_width(), pixbuf.get_height()
    if ow < 1 or oh < 1:
        return pixbuf
    target_aspect = target_w / float(target_h)
    src_aspect = ow / float(oh)
    if src_aspect > target_aspect:
        new_w = max(1, int(round(oh * target_aspect)))
        x = max(0, (ow - new_w) // 2)
        cropped = pixbuf.new_subpixbuf(x, 0, min(new_w, ow - x), oh)
    else:
        new_h = max(1, int(round(ow / target_aspect)))
        y = max(0, (oh - new_h) // 2)
        cropped = pixbuf.new_subpixbuf(0, y, ow, min(new_h, oh - y))
    return cropped.scale_simple(target_w, target_h, GdkPixbuf.InterpType.BILINEAR)


def _cairo_rounded_rect(cr, x, y, w, h, r):
    """Path for a rounded rectangle."""
    r = max(0.0, min(float(r), w / 2.0, h / 2.0))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -0.5 * 3.14159265, 0)
    cr.arc(x + w - r, y + h - r, r, 0, 0.5 * 3.14159265)
    cr.arc(x + r, y + h - r, r, 0.5 * 3.14159265, 3.14159265)
    cr.arc(x + r, y + r, r, 3.14159265, 1.5 * 3.14159265)
    cr.close_path()


def pixbuf_with_rounded_corners(pixbuf, radius=COVER_CORNER_RADIUS, border_px=0, border_rgba=(1, 1, 1, 0.35)):
    """Rounded corners (transparent outside) + optional border on the curve.

    Baking the border into the image avoids GTK3 CSS border-radius not clipping
    children, which left square image corners sticking past the outline.
    """
    w, h = pixbuf.get_width(), pixbuf.get_height()
    if w < 1 or h < 1:
        return pixbuf
    radius = max(0, min(int(radius), w // 2, h // 2))
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surface)
    cr.set_operator(cairo.OPERATOR_CLEAR)
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)
    _cairo_rounded_rect(cr, 0, 0, w, h, radius)
    cr.clip()
    Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
    cr.paint()
    cr.reset_clip()
    bp = max(0, int(border_px))
    if bp > 0:
        cr.set_line_width(float(bp))
        r, g, b, a = border_rgba
        cr.set_source_rgba(float(r), float(g), float(b), float(a))
        inset = bp / 2.0
        _cairo_rounded_rect(
            cr, inset, inset, max(1.0, w - bp), max(1.0, h - bp),
            max(0.0, radius - inset),
        )
        cr.stroke()
    return Gdk.pixbuf_get_from_surface(surface, 0, 0, w, h)


def pixbuf_fit_aspect(pixbuf, target_w, target_h, radius=0, border_px=0, border_rgba=(1, 1, 1, 0.35)):
    """Always center-crop to target aspect, scale, then round + border."""
    target_w = max(1, int(target_w))
    target_h = max(1, int(target_h))
    cropped = pixbuf_crop_to_aspect(pixbuf, target_w, target_h)
    if radius or border_px:
        return pixbuf_with_rounded_corners(
            cropped, radius=radius, border_px=border_px, border_rgba=border_rgba
        )
    return cropped


BLUR_CACHE_DIR = Path("/tmp/couchmpv-cache")


def pixbuf_blur_darken(pixbuf, darken=0.65, do_blur=True):
    """Smoother multi-pass downscale blur + black overlay (still cheap)."""
    w, h = pixbuf.get_width(), pixbuf.get_height()
    if w < 1 or h < 1:
        return pixbuf
    src = pixbuf
    if do_blur:
        # Two-step pyramid: less blocky than a single 1/14 scale
        # 1) mild shrink
        s1w = max(1, w // 30)
        s1h = max(1, h // 30)
        mid = pixbuf.scale_simple(s1w, s1h, GdkPixbuf.InterpType.BILINEAR)
        # 2) stronger shrink
        s2w = max(1, w // 50)
        s2h = max(1, h // 50)
        small = mid.scale_simple(s2w, s2h, GdkPixbuf.InterpType.BILINEAR)
        # 3) expand through mid size for softer result
        mid2 = small.scale_simple(s1w, s1h, GdkPixbuf.InterpType.BILINEAR)
        src = mid2.scale_simple(w, h, GdkPixbuf.InterpType.BILINEAR)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surface)
    Gdk.cairo_set_source_pixbuf(cr, src, 0, 0)
    cr.paint()
    d = max(0.0, min(1.0, float(darken)))
    cr.set_source_rgba(0.0, 0.0, 0.0, d)
    cr.paint()
    return Gdk.pixbuf_get_from_surface(surface, 0, 0, w, h)


def _blur_cache_key(src_path: str, darken: float, do_blur: bool, max_side: int) -> str:
    try:
        mtime = Path(src_path).stat().st_mtime_ns
    except Exception:
        mtime = 0
    raw = f"{src_path}|{mtime}|{darken:.3f}|{int(do_blur)}|{max_side}"
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()


def load_or_build_blurred_backdrop(src_path, src_pixbuf, darken=0.55, do_blur=True, max_side=640):
    """Build blurred backdrop once; reuse PNG from /tmp/couchmpv-cache."""
    try:
        BLUR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    key = _blur_cache_key(str(src_path), darken, do_blur, max_side)
    cache_file = BLUR_CACHE_DIR / f"{key}.png"
    if cache_file.is_file():
        try:
            return GdkPixbuf.Pixbuf.new_from_file(str(cache_file))
        except Exception:
            pass
    # Work at bounded size
    pixbuf = src_pixbuf
    w, h = pixbuf.get_width(), pixbuf.get_height()
    if max(w, h) > max_side:
        s = max_side / float(max(w, h))
        pixbuf = pixbuf.scale_simple(
            max(1, int(w * s)), max(1, int(h * s)),
            GdkPixbuf.InterpType.BILINEAR,
        )
    processed = pixbuf_blur_darken(pixbuf, darken=darken, do_blur=do_blur)
    try:
        processed.savev(str(cache_file), "png", [], [])
    except Exception as e:
        print("blur cache save error:", e)
    return processed


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
# UI SETTINGS
# =============================================================================
# target-resolution=1920x1080 # your monitor/TV size — higher = larger UI
#                             # (layout is designed for 1366x768; 1920 → ~1.4×)
# background=yes              # show blurred poster behind the browser
# background-blur=yes         # soft blur on the poster background
# background-darken=0.55      # 0.0 = none, 1.0 = fully black
# subdirectory-banner=yes     # seasons inherit show banner; episodes use it if no own

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
    directories, mpv_opts, ui_opts, section = [], {}, {}, None
    ui_defaults = {
        "target-resolution": "1366x768",
        "background": "yes",
        "background-blur": "yes",
        "background-darken": "0.55",
        "subdirectory-banner": "yes",
    }
    try:
        text = CONFIG_FILE.read_text(encoding="utf-8")
    except Exception as e:
        print("config read error:", e)
        return [os.path.expanduser("~/Videos")], {}, ui_defaults
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        upper = line.lstrip("#").strip().upper()
        if upper.startswith("DIRECTORIES"):
            section = "directories"
            continue
        if upper.startswith("UI SETTINGS") or upper == "UI":
            section = "ui"
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
        elif section == "ui":
            if "=" in line:
                key, _, val = line.partition("=")
                # "Subdirectory Banner" → "subdirectory-banner"
                key = key.strip().lower().replace(" ", "-")
                # Drop inline comments: "0.90  # almost black" → "0.90"
                val = val.split("#", 1)[0].strip().strip("\"'")
                if key:
                    ui_opts[key] = val
        elif section == "mpv":
            if "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.split("#", 1)[0].strip().strip("\"'")
                if key:
                    mpv_opts[key] = val
            elif line.strip():
                mpv_opts[line.strip().split("#", 1)[0].strip()] = "yes"
    if not directories:
        directories = [os.path.expanduser("~/"), os.path.expanduser("~/Videos")]
    for k, v in ui_defaults.items():
        ui_opts.setdefault(k, v)
    return directories, mpv_opts, ui_opts


DIRECTORIES, MPV_USER_OPTS, UI_OPTS = load_user_config()
_TARGET_W, _TARGET_H = parse_resolution(
    UI_OPTS.get("target-resolution", "1366x768"), (1366, 768)
)


def ui_bool(key, default=True):
    v = str(UI_OPTS.get(key, "yes" if default else "no")).strip().lower()
    return v in ("yes", "true", "on", "1")


def ui_float(key, default=0.55):
    try:
        return float(UI_OPTS.get(key, default))
    except (TypeError, ValueError):
        return default


def refresh_target_from_config():
    """Re-read target-resolution into globals (after config reload)."""
    global _TARGET_W, _TARGET_H
    _TARGET_W, _TARGET_H = parse_resolution(
        UI_OPTS.get("target-resolution", "1366x768"), (1366, 768)
    )

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".webm", ".mov", ".m4v", ".wmv",
    ".flv", ".ts", ".m2ts", ".mpg", ".mpeg", ".ogv", ".3gp",
    ".vob", ".rmvb", ".divx", ".xvid"
}

SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}

SKIP_SECONDS = 20
ANIM_MS = 160


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


def prettify_filename(stem: str) -> str:
    """Turn 'Pulp_Fiction.1994.1080p' / 'La.La.Land.2016...' into a readable title."""
    if not stem:
        return ""
    s = stem.replace("_", " ").replace(".", " ")
    # Drop common release tags from the display title
    s = re.sub(
        r"\b(1080p|720p|480p|2160p|4k|uhd|hdr|dv|bluray|blu ray|web[- ]?dl|"
        r"webrip|hdtv|x264|x265|h264|h265|hevc|av1|aac|dts|truehd|"
        r"remux|proper|repack|extended|directors cut|dc)\b",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"\s+", " ", s).strip(" -._")
    return s


def _xml_text(root, *tags):
    """First matching tag text under root (any depth)."""
    for tag in tags:
        for el in root.iter(tag):
            if el.text and el.text.strip():
                return el.text.strip()
    return ""


def _normalize_year(text: str) -> str:
    """Extract a 4-digit year from NFO fields (year, premiered, date)."""
    if not text:
        return ""
    m = re.search(r"(19|20)\d{2}", str(text))
    return m.group(0) if m else ""


def parse_nfo(nfo_path: Path) -> dict:
    """Parse Kodi-style NFO or plain-text → {title, plot, year}."""
    out = {"title": "", "plot": "", "year": ""}
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
            out["year"] = _normalize_year(
                _xml_text(root, "year", "premiered", "releasedate", "date")
            )
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


def find_poster(directory: Path, basename: str) -> Path | None:
    """Poster: Name_Poster.ext, then legacy Name.ext / poster.ext."""
    for ext in COVER_EXTS:
        cand = directory / f"{basename}_Poster{ext}"
        if cand.is_file():
            return cand
    for ext in COVER_EXTS:
        cand = directory / f"{basename}{ext}"
        if cand.is_file():
            return cand
    for name in ("poster", "folder", "cover", "coverart"):
        for ext in COVER_EXTS:
            cand = directory / f"{name}{ext}"
            if cand.is_file():
                return cand
    return None


def find_banner(directory: Path, basename: str) -> Path | None:
    """Banner for a folder: only {basename}_Banner.ext or generic banner/fanart.

    Never pick a random MovieName_Banner from a mixed folder (that caused
    Pulp Fiction's banner to stick on every other title).
    """
    # Exact {basename}_Banner.ext
    for ext in COVER_EXTS:
        cand = directory / f"{basename}_Banner{ext}"
        if cand.is_file():
            return cand
    # Case-insensitive match for the same basename only
    try:
        target = f"{basename}_banner".lower()
        for f in directory.iterdir():
            if not f.is_file():
                continue
            if f.stem.lower() == target and f.suffix.lower() in COVER_EXTS:
                return f
    except Exception:
        pass
    # Generic names only (show folders often use these)
    for name in ("banner", "fanart"):
        for ext in COVER_EXTS:
            cand = directory / f"{name}{ext}"
            if cand.is_file():
                return cand
    return None


def load_dir_meta(directory: Path, parent_meta: dict | None = None) -> dict:
    """Metadata for a folder: NFO / poster / banner, else inherit parent."""
    parent_meta = parent_meta or {}
    name = directory.name
    nfo = directory / f"{name}.nfo"
    if not nfo.is_file():
        for alt in ("tvshow.nfo", "movie.nfo", "season.nfo"):
            alt_p = directory / alt
            if alt_p.is_file():
                nfo = alt_p
                break
    parsed = parse_nfo(nfo) if nfo.is_file() else {"title": "", "plot": "", "year": ""}
    cover = find_poster(directory, name)
    banner = find_banner(directory, name)
    title = parsed["title"] or name
    plot = parsed["plot"] or parent_meta.get("plot") or ""
    year = parsed.get("year") or parent_meta.get("year") or ""
    if cover is None:
        cover = parent_meta.get("cover")
    # Subdir banner inheritance (Breaking Bad → Season 1) — toggle in config
    if banner is None and ui_bool("subdirectory-banner", True):
        banner = parent_meta.get("banner")
    return {
        "title": title,
        "plot": plot or "",
        "year": year or "",
        "cover": cover,
        "banner": banner,
        "has_own_nfo": bool(parsed["title"] or parsed["plot"] or parsed.get("year")),
        "has_own_cover": cover is not None and (
            parent_meta.get("cover") is None or cover != parent_meta.get("cover")
        ),
    }


def find_file_cover(video: Path) -> Path | None:
    """Video poster: Stem_Poster.ext (legacy: Stem.ext)."""
    directory = video.parent
    basename = video.stem
    for ext in COVER_EXTS:
        cand = directory / f"{basename}_Poster{ext}"
        if cand.is_file():
            return cand
    for ext in COVER_EXTS:
        cand = directory / f"{basename}{ext}"
        if cand.is_file():
            return cand
    return None


def find_file_banner(video: Path) -> Path | None:
    """Video banner: only {exact stem}_Banner.ext (case-insensitive)."""
    directory = video.parent
    basename = video.stem
    for ext in COVER_EXTS:
        cand = directory / f"{basename}_Banner{ext}"
        if cand.is_file():
            return cand
    try:
        target = f"{basename}_banner".lower()
        for f in directory.iterdir():
            if not f.is_file():
                continue
            if f.stem.lower() == target and f.suffix.lower() in COVER_EXTS:
                return f
    except Exception:
        pass
    return None


# In-process caches so browsing many movies stays smooth
_FILE_META_CACHE: dict = {}
_COVER_PIX_CACHE: dict = {}   # path → rounded 2:3 pixbuf
_BANNER_SRC_CACHE: dict = {}  # path → full source pixbuf


def load_file_meta(video: Path) -> dict:
    """Episode/movie-level NFO + poster + banner next to the video."""
    key = str(video)
    cached = _FILE_META_CACHE.get(key)
    if cached is not None:
        return cached
    nfo = video.with_suffix(".nfo")
    parsed = parse_nfo(nfo) if nfo.is_file() else {"title": "", "plot": "", "year": ""}
    out = {
        "title": parsed.get("title") or "",
        "plot": parsed.get("plot") or "",
        "year": parsed.get("year") or "",
        "cover": find_file_cover(video),
        "banner": find_file_banner(video),
    }
    _FILE_META_CACHE[key] = out
    return out


# ── Media stream tech (resolution / audio / video codec) via ffprobe ──────────
_MEDIA_TECH_CACHE: dict = {}


def _res_label_from_width(width: int) -> tuple[str, str]:
    """Return (plain_text, pango_markup) for resolution badge from horizontal size."""
    w = int(width or 0)
    if w >= 3840:
        return "4K UHD", "4K <b>UHD</b>"
    if w >= 2560:
        return "2K QHD", "2K <b>QHD</b>"
    if w >= 1920:
        return "FULL HD", "FULL <b>HD</b>"
    if w >= 1280:
        return "HD", "<b>HD</b>"
    if w > 0:
        return "SD", "<b>SD</b>"
    return "", ""


def _format_video_codec(name: str) -> str:
    n = (name or "").lower().strip()
    mapping = {
        "hevc": "H.265", "h265": "H.265", "hev1": "H.265", "hvc1": "H.265",
        "h264": "H.264", "avc": "H.264", "avc1": "H.264", "x264": "H.264",
        "av1": "AV1", "vp9": "VP9", "vp8": "VP8",
        "mpeg2video": "MPEG-2", "mpeg4": "MPEG-4", "xvid": "Xvid",
        "vc1": "VC-1", "wmv3": "WMV", "prores": "ProRes",
    }
    if n in mapping:
        return mapping[n]
    if n.startswith("h26"):
        return n.upper().replace("H26", "H.26")
    return (name or "").upper() if name else ""


def _format_audio_codec(codec: str, channels: int, channel_layout: str, profile: str = "") -> str:
    """Human-friendly audio label (Dolby Digital 5.1, Stereo, …)."""
    c = (codec or "").lower()
    p = (profile or "").lower()
    layout = (channel_layout or "").lower()
    ch = int(channels or 0)

    # Channel suffix
    ch_suffix = ""
    if ch >= 8 or "7.1" in layout:
        ch_suffix = " 7.1"
    elif ch >= 6 or "5.1" in layout:
        ch_suffix = " 5.1"
    elif ch == 2 or "stereo" in layout:
        ch_suffix = ""  # stereo handled below
    elif ch == 1 or "mono" in layout:
        return "Mono"

    # Codec family
    if "truehd" in c or "truehd" in p:
        base = "Dolby TrueHD"
        if "atmos" in p or "atmos" in layout:
            return "Dolby Atmos"
        return base + ch_suffix
    if c in ("eac3", "ec-3") or "eac3" in c:
        base = "Dolby Digital+"
        if "atmos" in p:
            return "Dolby Atmos"
        return base + ch_suffix
    if c in ("ac3", "ac-3"):
        return "Dolby Digital" + ch_suffix
    if "dts" in c:
        if "x" in p or "dts:x" in p:
            return "DTS:X"
        if "hd" in c or "ma" in p or "hra" in p:
            return "DTS-HD" + ch_suffix
        return "DTS" + ch_suffix
    if c in ("aac", "mp3", "opus", "flac", "vorbis", "pcm_s16le", "pcm_s24le"):
        if ch >= 6:
            return "Surround" + ch_suffix
        if ch == 1:
            return "Mono"
        if ch == 2:
            return "Stereo"
        return c.upper() + ch_suffix
    if ch >= 6:
        return "Surround" + ch_suffix
    if ch == 2:
        return "Stereo"
    if ch == 1:
        return "Mono"
    return (codec or "").upper()


_MEDIA_TECH_EMPTY = {"res": "", "res_markup": "", "audio": "", "vcodec": "", "width": 0}
_PROBE_INFLIGHT: set = set()


def _probe_media_tech_sync(path) -> dict:
    """Blocking ffprobe — call only from a worker thread or for cache fill."""
    key = str(path)
    hit = _MEDIA_TECH_CACHE.get(key)
    if hit is not None:
        return hit
    p = Path(path) if not isinstance(path, Path) else path
    if not p.is_file():
        _MEDIA_TECH_CACHE[key] = dict(_MEDIA_TECH_EMPTY)
        return _MEDIA_TECH_CACHE[key]
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                # Only need codec/width — skip heavy packet analysis
                "-show_entries", "stream=codec_type,codec_name,width,height,channels,channel_layout,profile",
                str(p),
            ],
            capture_output=True, text=True, timeout=6,
        )
        if r.returncode != 0 or not r.stdout:
            _MEDIA_TECH_CACHE[key] = dict(_MEDIA_TECH_EMPTY)
            return _MEDIA_TECH_CACHE[key]
        data = json.loads(r.stdout)
    except Exception as e:
        print("ffprobe error:", e)
        _MEDIA_TECH_CACHE[key] = dict(_MEDIA_TECH_EMPTY)
        return _MEDIA_TECH_CACHE[key]

    width = 0
    vcodec = ""
    audio = ""
    for s in data.get("streams") or []:
        if s.get("codec_type") == "video" and not width:
            try:
                width = int(s.get("width") or 0)
            except (TypeError, ValueError):
                width = 0
            vcodec = _format_video_codec(s.get("codec_name") or "")
        elif s.get("codec_type") == "audio" and not audio:
            try:
                ch = int(s.get("channels") or 0)
            except (TypeError, ValueError):
                ch = 0
            audio = _format_audio_codec(
                s.get("codec_name") or "",
                ch,
                s.get("channel_layout") or "",
                s.get("profile") or "",
            )

    res, res_markup = _res_label_from_width(width)
    out = {
        "res": res,
        "res_markup": res_markup,
        "audio": audio,
        "vcodec": vcodec,
        "width": width,
    }
    _MEDIA_TECH_CACHE[key] = out
    if len(_MEDIA_TECH_CACHE) > 256:
        try:
            del _MEDIA_TECH_CACHE[next(iter(_MEDIA_TECH_CACHE))]
        except Exception:
            pass
    return out


def probe_media_tech_cached(path) -> dict | None:
    """Return cached tech only (never blocks). None = not probed yet."""
    return _MEDIA_TECH_CACHE.get(str(path))


def probe_media_tech_async(path, callback=None) -> dict | None:
    """
    Return cache hit immediately. Otherwise start a background probe and
    invoke callback(path_str, tech_dict) on the GTK main loop when done.
    """
    key = str(path)
    hit = _MEDIA_TECH_CACHE.get(key)
    if hit is not None:
        return hit
    if key in _PROBE_INFLIGHT:
        return None
    _PROBE_INFLIGHT.add(key)

    def _worker():
        try:
            result = _probe_media_tech_sync(key)
        finally:
            _PROBE_INFLIGHT.discard(key)
        if callback is not None:
            GLib.idle_add(callback, key, result)
        return False

    threading.Thread(target=_worker, daemon=True).start()
    return None


def probe_media_tech(path) -> dict:
    """Sync probe (blocks). Prefer probe_media_tech_async while browsing."""
    return _probe_media_tech_sync(path)


def get_cover_pixbuf(cover_path, pw=None, ph=None, radius=None, border_px=None):
    """Load + crop to 2:3 + round + outline once; reuse from memory."""
    if not cover_path:
        return None
    path = str(cover_path)
    pw = int(pw if pw is not None else S(POSTER_W))
    ph = int(ph if ph is not None else S(POSTER_H))
    radius = int(radius if radius is not None else S(COVER_CORNER_RADIUS))
    border_px = int(border_px if border_px is not None else max(1, S(2)))
    cache_key = f"{path}|{pw}x{ph}|r{radius}|b{border_px}"
    hit = _COVER_PIX_CACHE.get(cache_key)
    if hit is not None:
        return hit
    if not Path(path).is_file():
        return None
    try:
        full = GdkPixbuf.Pixbuf.new_from_file(path)
        pix = pixbuf_fit_aspect(
            full, pw, ph,
            radius=radius,
            border_px=border_px,
            border_rgba=(1, 1, 1, 0.40),
        )
        _COVER_PIX_CACHE[cache_key] = pix
        if len(_COVER_PIX_CACHE) > 64:
            try:
                del _COVER_PIX_CACHE[next(iter(_COVER_PIX_CACHE))]
            except Exception:
                pass
        return pix
    except Exception as e:
        print("cover load error:", e)
        return None


def get_banner_src_pixbuf(banner_path):
    """Load banner source once; scaled copies still depend on size."""
    if not banner_path:
        return None
    path = str(banner_path)
    hit = _BANNER_SRC_CACHE.get(path)
    if hit is not None:
        return hit
    if not Path(path).is_file():
        return None
    try:
        full = GdkPixbuf.Pixbuf.new_from_file(path)
        _BANNER_SRC_CACHE[path] = full
        return full
    except Exception as e:
        print("banner load error:", e)
        return None


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


def history_badge(path: str) -> str:
    """End-of-row badge: ✓ watched, or ▶ time if in progress."""
    entry = history_get(path)
    if not entry:
        return ""
    if entry.get("watched"):
        return "✓"
    pos = float(entry.get("position") or 0)
    if pos >= RESUME_MIN_SECONDS:
        return f"▶ {format_time(pos)}"
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
class GradientBackground(Gtk.DrawingArea):
    """Solid #17101e, or optional blurred/darkened poster backdrop.

    Performance: scale the blurred source to the window ONCE and cache it.
    Each redraw only blits the cached pixbuf (no scale_simple per frame).
    """

    def __init__(self):
        super().__init__()
        self._bg_pixbuf = None       # source (~640px blurred)
        self._scaled = None          # full-window cover copy
        self._scaled_size = (0, 0)
        self.connect("draw", self._draw)
        self.connect("size-allocate", self._on_size_allocate)

    def set_backdrop(self, pixbuf):
        self._bg_pixbuf = pixbuf
        self._scaled = None
        self._scaled_size = (0, 0)
        self.queue_draw()

    def clear_backdrop(self):
        self._bg_pixbuf = None
        self._scaled = None
        self._scaled_size = (0, 0)
        self.queue_draw()

    def _on_size_allocate(self, widget, allocation):
        w, h = allocation.width, allocation.height
        if (w, h) != self._scaled_size:
            self._scaled = None

    def _ensure_scaled(self, w, h):
        if self._bg_pixbuf is None or w < 1 or h < 1:
            self._scaled = None
            self._scaled_size = (0, 0)
            return
        if self._scaled is not None and self._scaled_size == (w, h):
            return
        try:
            pw = self._bg_pixbuf.get_width()
            ph = self._bg_pixbuf.get_height()
            if pw < 1 or ph < 1:
                self._scaled = None
                return
            scale = max(w / float(pw), h / float(ph))
            dw = max(1, int(pw * scale))
            dh = max(1, int(ph * scale))
            self._scaled = self._bg_pixbuf.scale_simple(
                dw, dh, GdkPixbuf.InterpType.BILINEAR
            )
            self._scaled_size = (w, h)
        except Exception:
            self._scaled = None
            self._scaled_size = (0, 0)

    def _draw(self, widget, cr):
        alloc = widget.get_allocation()
        w, h = alloc.width, alloc.height
        if w < 1 or h < 1:
            return False
        cr.set_source_rgb(*BG_COLOR)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        if self._bg_pixbuf is not None and ui_bool("background", True):
            try:
                self._ensure_scaled(w, h)
                if self._scaled is not None:
                    dw = self._scaled.get_width()
                    dh = self._scaled.get_height()
                    ox = (w - dw) // 2
                    oy = (h - dh) // 2
                    Gdk.cairo_set_source_pixbuf(cr, self._scaled, ox, oy)
                    cr.paint()
            except Exception:
                pass
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
    """Full-width row: title + optional progress badge on the right."""

    def __init__(self, title: str, kind: str = "video", badge: str = ""):
        super().__init__()
        self.set_can_focus(True)
        self._path = None
        self._kind = kind  # "folder" | "video"

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_start(14)
        box.set_margin_end(8)
        box.set_margin_top(8)
        box.set_margin_bottom(8)

        lbl = Gtk.Label(label=title, xalign=0)
        lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        lbl.set_hexpand(True)
        lbl.get_style_context().add_class("list-row-title")
        box.pack_start(lbl, True, True, 0)

        self.badge_box = Gtk.EventBox()
        self.badge_box.get_style_context().add_class("list-badge")
        # Keep empty badges hidden when parent calls show_all()
        self.badge_box.set_no_show_all(True)
        self.badge_label = Gtk.Label(label="")
        self.badge_label.get_style_context().add_class("list-badge-text")
        self.badge_label.set_margin_start(10)
        self.badge_label.set_margin_end(10)
        self.badge_label.set_margin_top(4)
        self.badge_label.set_margin_bottom(4)
        self.badge_box.add(self.badge_label)
        box.pack_end(self.badge_box, False, False, 0)
        # Start hidden; set_badge uses show() (show_all is ignored with no_show_all)
        self.badge_box.hide()
        if badge:
            self.set_badge(badge)

        self.add(box)
        self.get_style_context().add_class("list-row")
        self.connect("focus-in-event",
                     lambda *a: self.get_style_context().add_class("list-row-focused") or False)
        self.connect("focus-out-event",
                     lambda *a: self.get_style_context().remove_class("list-row-focused") or False)

    def set_badge(self, text: str):
        # no_show_all=True blocks show_all() on this widget — use show()
        if text and str(text).strip():
            self.badge_label.set_text(str(text).strip())
            self.badge_label.show()
            self.badge_box.show()
        else:
            self.badge_label.set_text("")
            self.badge_box.hide()


class VerticalList(Gtk.ScrolledWindow):
    """Vertical list of ListRows – navigate with Up/Down + smooth scroll."""
    def __init__(self):
        super().__init__()
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_overlay_scrolling(False)
        self.get_style_context().add_class("vlist-scroll")

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.box.set_halign(Gtk.Align.FILL)
        self.box.set_valign(Gtk.Align.START)
        self.box.set_margin_start(4)
        self.box.set_margin_end(4)
        self.box.set_margin_top(4)
        self.box.set_margin_bottom(12)
        self.add(self.box)

        self.rows = []
        self.current_index = 0
        self._anim = None

    def clear(self):
        if self._anim:
            try:
                GLib.source_remove(self._anim)
            except Exception:
                pass
            self._anim = None
        for c in self.box.get_children():
            self.box.remove(c)
        self.rows.clear()
        self.current_index = 0

    def add_row(self, row: ListRow):
        self.box.pack_start(row, False, False, 0)
        self.rows.append(row)

    def focus_index(self, idx: int, animate: bool = True):
        if not self.rows:
            return
        idx = max(0, min(idx, len(self.rows) - 1))
        self.current_index = idx
        row = self.rows[idx]
        row.grab_focus()

        adj = self.get_vadjustment()
        if not adj:
            return
        alloc = row.get_allocation()
        page = adj.get_page_size()
        val = adj.get_value()
        # Prefer keeping the row near the middle of the viewport
        target = alloc.y - (page - alloc.height) / 2.0
        target = max(0.0, min(target, max(0.0, adj.get_upper() - page)))

        # Already fully visible and close — snap
        if alloc.y >= val and alloc.y + alloc.height <= val + page:
            if abs(target - val) < 8:
                return

        if not animate or abs(target - val) < 2:
            adj.set_value(target)
            return

        if self._anim:
            try:
                GLib.source_remove(self._anim)
            except Exception:
                pass
        start = adj.get_value()
        t0 = GLib.get_monotonic_time()
        duration = ANIM_MS * 1000  # µs

        def tick():
            t = min(1.0, (GLib.get_monotonic_time() - t0) / duration)
            # ease-out cubic
            ease = 1.0 - (1.0 - t) ** 3
            adj.set_value(start + (target - start) * ease)
            if t >= 1.0:
                self._anim = None
                return False
            return True

        self._anim = GLib.timeout_add(16, tick)


class SpinIcon(Gtk.DrawingArea):
    """Visible loading ring — Gtk.Spinner is often invisible on dark themes."""

    def __init__(self, size=72):
        super().__init__()
        self._size = max(48, int(size))
        self._angle = 0.0
        self._running = False
        self._tick_id = None
        self.set_size_request(self._size, self._size)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        # Ensure cairo draws on Wayland/GTK3 themes
        self.set_app_paintable(True)
        try:
            self.set_double_buffered(True)
        except Exception:
            pass
        self.connect("draw", self._on_draw)
        self.connect("size-allocate", self._on_size)

    def _on_size(self, widget, allocation):
        # Keep a real pixel size even if the parent briefly allocates 0
        if allocation.width < self._size or allocation.height < self._size:
            self.set_size_request(self._size, self._size)

    def start(self):
        self._running = True
        self.show()
        if self._tick_id is None:
            self._tick_id = GLib.timeout_add(33, self._tick)
        self.queue_draw()

    def stop(self):
        self._running = False
        if self._tick_id is not None:
            try:
                GLib.source_remove(self._tick_id)
            except Exception:
                pass
            self._tick_id = None
        self.queue_draw()

    def _tick(self):
        if not self._running:
            self._tick_id = None
            return False
        self._angle = (self._angle + 0.28) % (2.0 * math.pi)
        self.queue_draw()
        return True

    def _on_draw(self, widget, cr):
        alloc = widget.get_allocation()
        w = max(int(alloc.width), self._size)
        h = max(int(alloc.height), self._size)
        if w < 8 or h < 8:
            return False
        cx, cy = w / 2.0, h / 2.0
        radius = min(w, h) * 0.38
        line = max(4.0, radius * 0.16)
        cr.set_line_width(line)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        # Dim track
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.22)
        cr.arc(cx, cy, radius, 0, 2.0 * math.pi)
        cr.stroke()
        # Bright arc
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.95)
        cr.arc(cx, cy, radius, self._angle, self._angle + 1.7)
        cr.stroke()
        return False


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
        self._ui_scale_applied = 1.0
        self._css_provider = None

        self._install_css()

        self.overlay = Gtk.Overlay()
        self.add(self.overlay)
        self.connect("size-allocate", self._on_window_size_allocate)

        self.bg = GradientBackground()
        self.overlay.add(self.bg)

        # Loading screen (solid bg + custom spinner) — covers banner flicker / video wait
        self.loading = Gtk.EventBox()
        self.loading.set_halign(Gtk.Align.FILL)
        self.loading.set_valign(Gtk.Align.FILL)
        self.loading.set_hexpand(True)
        self.loading.set_vexpand(True)
        self.loading.get_style_context().add_class("loading-screen")
        try:
            self.loading.set_visible_window(True)
        except Exception:
            pass
        self.loading.set_app_paintable(True)
        load_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        load_inner.set_halign(Gtk.Align.FILL)
        load_inner.set_valign(Gtk.Align.FILL)
        load_inner.set_hexpand(True)
        load_inner.set_vexpand(True)
        load_center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        load_center.set_halign(Gtk.Align.CENTER)
        load_center.set_valign(Gtk.Align.CENTER)
        load_center.set_hexpand(True)
        load_center.set_vexpand(True)
        self.loading_spinner = SpinIcon(size=max(64, S(80)))
        load_center.pack_start(self.loading_spinner, True, False, 0)
        load_inner.pack_start(load_center, True, True, 0)
        self.loading.add(load_inner)
        self.overlay.add_overlay(self.loading)
        self.loading.set_no_show_all(True)
        self.loading.set_opacity(0.0)
        self.loading.hide()
        self._loading_fade_id = None
        self._loading_visible = False

        browser_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        browser_box.set_valign(Gtk.Align.FILL)

        self.title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=S(12))
        self.title_row.set_halign(Gtk.Align.START)
        self.title_row.set_valign(Gtk.Align.CENTER)
        self.title_row.set_margin_top(S(14))
        self.title_row.set_margin_bottom(S(8))
        self.title_row.set_margin_start(S(16))

        self.browser_title = Gtk.Label(label="Directories")
        self.browser_title.get_style_context().add_class("main-title")
        self.browser_title.set_halign(Gtk.Align.START)
        self.browser_title.set_valign(Gtk.Align.CENTER)
        # Don't baseline-align with the badge — treat title + badge as peers
        try:
            self.browser_title.set_yalign(0.5)
        except Exception:
            pass
        self.title_row.pack_start(self.browser_title, False, False, 0)

        # Box wrapper so CSS padding/background actually paint (Label ignores padding)
        self.year_badge_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.year_badge_box.get_style_context().add_class("year-badge")
        self.year_badge_box.set_valign(Gtk.Align.CENTER)
        self.year_badge_box.set_halign(Gtk.Align.START)
        self.year_badge_box.set_no_show_all(True)
        self.year_badge_box.hide()

        self.year_badge = Gtk.Label(label="")
        self.year_badge.get_style_context().add_class("year-badge-text")
        self.year_badge.set_halign(Gtk.Align.CENTER)
        self.year_badge.set_valign(Gtk.Align.CENTER)
        self.year_badge_box.pack_start(self.year_badge, True, True, 0)

        self.title_row.pack_start(self.year_badge_box, False, False, 0)

        # Tech badges: Resolution | Audio | Video codec
        def _make_tech_badge(box_class, text_class):
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            box.get_style_context().add_class(box_class)
            box.set_valign(Gtk.Align.CENTER)
            box.set_halign(Gtk.Align.START)
            box.set_no_show_all(True)
            box.hide()
            lbl = Gtk.Label(label="")
            lbl.get_style_context().add_class(text_class)
            lbl.set_use_markup(True)
            lbl.set_halign(Gtk.Align.CENTER)
            lbl.set_valign(Gtk.Align.CENTER)
            box.pack_start(lbl, True, True, 0)
            self.title_row.pack_start(box, False, False, 0)
            return box, lbl

        self.res_badge_box, self.res_badge = _make_tech_badge(
            "res-badge", "res-badge-text"
        )
        self.audio_badge_box, self.audio_badge = _make_tech_badge(
            "audio-badge", "audio-badge-text"
        )
        self.vcodec_badge_box, self.vcodec_badge = _make_tech_badge(
            "vcodec-badge", "vcodec-badge-text"
        )

        browser_box.pack_start(self.title_row, False, False, 0)

        self.browse_stack = Gtk.Stack()
        self.browse_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.browse_stack.set_transition_duration(150)

        # Root: horizontal cards
        root_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root_wrap.set_valign(Gtk.Align.CENTER)
        self.scroller = HorizontalScroller()
        root_wrap.pack_start(self.scroller, True, True, 0)
        self.browse_stack.add_named(root_wrap, "root")

        # Folder view: poster + plot | banner + list
        self.media_page = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=S(14))
        media_page = self.media_page
        media_page.set_margin_start(S(16))
        media_page.set_margin_end(S(16))
        media_page.set_margin_bottom(S(12))

        self.left_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=S(10))
        self.left_col.set_valign(Gtk.Align.START)
        self.left_col.set_halign(Gtk.Align.START)
        self.left_col.set_vexpand(False)
        self.left_col.set_hexpand(False)
        self.left_col.set_size_request(S(POSTER_W), -1)

        self.cover_frame = Gtk.Frame()
        self.cover_frame.set_shadow_type(Gtk.ShadowType.NONE)
        self.cover_frame.set_halign(Gtk.Align.CENTER)
        self.cover_frame.get_style_context().add_class("cover-frame")
        self.cover_frame.set_size_request(S(POSTER_W), S(POSTER_H))
        # Start empty — _set_cover installs image or "No Cover" placeholder
        self._cover_path = None
        self.left_col.pack_start(self.cover_frame, False, False, 0)

        self._desc_base_w = S(POSTER_W)
        self._desc_cover_h = S(POSTER_H)
        self.desc_frame = Gtk.Frame()
        self.desc_frame.set_shadow_type(Gtk.ShadowType.NONE)
        self.desc_frame.set_halign(Gtk.Align.FILL)
        self.desc_frame.set_valign(Gtk.Align.START)
        self.desc_frame.set_hexpand(False)
        self.desc_frame.set_vexpand(False)
        self.desc_frame.get_style_context().add_class("desc-frame")
        self.desc_label = Gtk.Label(label="No description.", xalign=0, yalign=0)
        self.desc_label.set_line_wrap(True)
        self.desc_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.desc_label.set_justify(Gtk.Justification.LEFT)
        self.desc_label.set_valign(Gtk.Align.START)
        self.desc_label.set_hexpand(False)
        self.desc_label.set_vexpand(False)
        self.desc_label.set_margin_start(S(10))
        self.desc_label.set_margin_end(S(10))
        self.desc_label.set_margin_top(S(8))
        self.desc_label.set_margin_bottom(S(8))
        self.desc_label.get_style_context().add_class("desc-text")
        self.desc_frame.add(self.desc_label)
        self.left_col.pack_start(self.desc_frame, False, False, 0)

        media_page.pack_start(self.left_col, False, False, 0)

        # Right: banner (optional) + episode list
        self.right_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=S(10))
        self.right_col.set_hexpand(True)
        self.right_col.set_vexpand(True)

        self.banner_frame = Gtk.Frame()
        self.banner_frame.set_shadow_type(Gtk.ShadowType.NONE)
        self.banner_frame.set_halign(Gtk.Align.FILL)
        # hexpand=False keeps the frame locked to the image size (no empty sides)
        self.banner_frame.set_hexpand(False)
        self.banner_frame.get_style_context().add_class("banner-frame")
        self.banner_frame.add(Gtk.Image())
        self._banner_path = None
        self._banner_applied_path = None
        self._banner_applied_size = None
        self._banner_did_refit = False
        self.right_col.pack_start(self.banner_frame, False, False, 0)
        self.banner_frame.hide()

        self.vlist = VerticalList()
        self.right_col.pack_start(self.vlist, True, True, 0)

        media_page.pack_start(self.right_col, True, True, 0)
        self.browse_stack.add_named(media_page, "list")

        browser_box.pack_start(self.browse_stack, True, True, 0)
        self.overlay.add_overlay(browser_box)

        self._show_root()
        self.add_events(Gdk.EventMask.KEY_PRESS_MASK)
        self.show_all()
        self.scroller.focus_index(0, animate=False)

    # ── CSS ─────────────────────────────────────────────────────────────────
    def _install_css(self):
        """CSS with font/radius sizes scaled to current ui_scale()."""
        s = ui_scale()

        def px(n):
            return max(1, int(round(n * s)))

        css = f"""
        .main-title {{
            font-size: {px(28)}px;
            font-weight: bold;
            color: #ffffff;
            text-shadow: 0 2px 8px rgba(0,0,0,0.65);
        }}
        .card {{
            background-color: rgba(255, 255, 255, 0.12);
            border-radius: {px(12)}px;
            border: {max(1, px(2))}px solid transparent;
            transition: background-color 140ms ease, border-color 140ms ease;
        }}
        .card-focused {{
            background-color: rgba(255, 255, 255, 0.28);
            border: {max(1, px(2))}px solid rgba(255, 255, 255, 0.55);
        }}
        .card-title {{
            font-size: {px(17)}px;
            font-weight: bold;
            color: #ffffff;
        }}
        .card-subtitle {{
            font-size: {px(12)}px;
            color: rgba(255, 255, 255, 0.65);
        }}
        .list-row {{
            background-color: rgba(255, 255, 255, 0.14);
            border-radius: {px(10)}px;
            border: {max(1, px(2))}px solid transparent;
            transition: background-color 120ms ease, border-color 120ms ease;
        }}
        .list-row-focused {{
            background-color: rgba(255, 255, 255, 0.32);
            border: {max(1, px(2))}px solid rgba(255, 255, 255, 0.55);
        }}
        .list-row-focused .list-row-title {{
            font-weight: bold;
            color: #000000;
            transition: color 120ms ease;
        }}
        .list-row-title {{
            font-size: {px(15)}px;
            color: #ffffff;
            transition: color 120ms ease;
        }}
        .list-badge {{
            background-color: rgba(255, 205, 59, 0.70);
            border-radius: {px(8)}px;
        }}
        .list-badge-text {{
            font-size: {px(13)}px;
            color: #000000;
            font-weight: bold;
        }}
        .year-badge {{
            background-color: rgba(255, 205, 59, 0.70);
            border-radius: {px(8)}px;
            border: none;
            padding: {px(4)}px {px(8)}px;
            min-height: 0;
        }}
        .year-badge-text {{
            color: #000000;
            font-weight: bold;
            font-size: {px(15)}px;
            padding: 0;
            margin: 0;
        }}
        .res-badge {{
            background-color: #000000;
            border: {max(2, px(2))}px solid #f5c518;
            border-radius: 0;
            padding: {px(2)}px {px(8)}px;
        }}
        .res-badge-text {{
            color: #f5c518;
            font-size: {px(12)}px;
            font-weight: normal;
        }}
        .audio-badge {{
            background-color: #000000;
            border: {max(2, px(2))}px solid #ffffff;
            border-radius: 0;
            padding: {px(2)}px {px(8)}px;
        }}
        .audio-badge-text {{
            color: #ffffff;
            font-size: {px(12)}px;
            font-weight: bold;
        }}
        .vcodec-badge {{
            background-color: rgba(255, 255, 255, 0.75);
            border: none;
            border-radius: 0;
            padding: {px(3)}px {px(8)}px;
        }}
        .vcodec-badge-text {{
            color: #1a9e3f;
            font-size: {px(12)}px;
            font-weight: bold;
        }}
        .cover-frame {{
            background-color: transparent;
            border: none;
            padding: 0;
            box-shadow: none;
        }}
        .banner-frame {{
            background-color: transparent;
            border: none;
            padding: 0;
            box-shadow: none;
        }}
        .desc-frame {{
            background-color: rgba(255, 255, 255, 0.14);
            border-radius: {px(10)}px;
            border: 1px solid rgba(255, 255, 255, 0.22);
        }}
        .desc-text {{
            font-size: {px(13)}px;
            color: rgba(255, 255, 255, 0.92);
        }}
        .no-cover-label {{
            font-size: {px(22)}px;
            font-weight: bold;
            color: rgba(255,255,255,0.40);
        }}
        .no-cover-box {{
            background-color: rgba(255, 255, 255, 0.08);
            border-radius: {px(10)}px;
        }}
        .vlist-scroll scrollbar slider {{
            background-color: rgba(255, 255, 255, 0.30);
            border-radius: {px(6)}px;
            min-width: {px(8)}px;
        }}
        .vlist-scroll scrollbar {{
            background-color: transparent;
        }}
        .loading-screen {{
            background-color: #17101e;
            background: #17101e;
        }}
        .loading-screen * {{
            background-color: transparent;
        }}
        """.encode("utf-8")

        # Replace previous provider so rescales don't stack
        if getattr(self, "_css_provider", None) is not None:
            try:
                Gtk.StyleContext.remove_provider_for_screen(
                    Gdk.Screen.get_default(), self._css_provider
                )
            except Exception:
                pass
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self._css_provider = provider
        self._ui_scale_applied = ui_scale()

    def _on_window_size_allocate(self, widget, allocation):
        """Recompute UI scale when the window size is known / changes.

        Layout numbers are tuned for 1366×768. target-resolution is your
        screen size: higher target → larger UI (clamped so it still fits
        the real window).
        """
        global _UI_SCALE
        w, h = allocation.width, allocation.height
        if w < 200 or h < 200:
            return
        tw = max(320, int(_TARGET_W))
        th = max(240, int(_TARGET_H))
        # Higher target-resolution → bigger UI
        scale = min(tw / float(_DESIGN_W), th / float(_DESIGN_H))
        # Never overflow the actual window
        scale = min(scale, w / float(_DESIGN_W), h / float(_DESIGN_H))
        scale = max(0.5, min(4.0, scale))
        if abs(scale - _UI_SCALE) < 0.03 and abs(scale - self._ui_scale_applied) < 0.03:
            return
        _UI_SCALE = scale
        if getattr(self, "_scale_idle", None):
            return
        self._scale_idle = GLib.idle_add(self._apply_ui_scale)

    def _apply_ui_scale(self):
        self._scale_idle = None
        if abs(ui_scale() - self._ui_scale_applied) < 0.03:
            return False
        print(f"UI scale: {ui_scale():.2f}× (target {_TARGET_W}x{_TARGET_H})")
        self._install_css()
        self._sync_poster_column_size()
        if getattr(self, "media_page", None):
            self.media_page.set_spacing(S(14))
            self.media_page.set_margin_start(S(16))
            self.media_page.set_margin_end(S(16))
            self.media_page.set_margin_bottom(S(12))
        self.left_col.set_spacing(S(10))
        self.right_col.set_spacing(S(10))
        self.title_row.set_spacing(S(10))
        self.title_row.set_margin_top(S(14))
        self.title_row.set_margin_bottom(S(8))
        self.title_row.set_margin_start(S(16))
        self.desc_label.set_margin_start(S(10))
        self.desc_label.set_margin_end(S(10))
        self.desc_label.set_margin_top(S(8))
        self.desc_label.set_margin_bottom(S(8))
        # Force poster/banner rebuild at new size
        prev_cover = getattr(self, "_cover_path", None)
        self._cover_path = None
        if prev_cover:
            self._set_cover(prev_cover)
        else:
            self._sync_poster_column_size()
        self._banner_applied_path = None
        self._banner_applied_size = None
        if getattr(self, "_banner_path", None):
            path = self._banner_path
            self._banner_path = None
            self._set_banner(path)
        self._fit_description()
        return False

    def _sync_poster_column_size(self):
        """Poster, description and title share the same left edge and width."""
        pw, ph = S(POSTER_W), S(POSTER_H)
        self._desc_base_w = pw
        self._desc_cover_h = ph
        # One width for the whole left column — no extra border padding
        self.left_col.set_size_request(pw, -1)
        self.left_col.set_hexpand(False)
        self.left_col.set_halign(Gtk.Align.START)
        self.cover_frame.set_size_request(pw, ph)
        self.cover_frame.set_halign(Gtk.Align.START)
        self.cover_frame.set_hexpand(False)
        self.cover_frame.set_vexpand(False)
        self.desc_frame.set_size_request(pw, -1)
        self.desc_frame.set_halign(Gtk.Align.START)
        self.desc_frame.set_hexpand(False)
        return pw, ph

    # ── Browser population ──────────────────────────────────────────────────
    def _set_cover(self, cover_path):
        """Poster fixed 2:3 — uses memory cache; drives blurred backdrop."""
        new_path = str(cover_path) if cover_path else None
        if new_path and not Path(new_path).is_file():
            new_path = None

        child = self.cover_frame.get_child()
        is_placeholder = bool(child is not None and getattr(child, "_is_no_cover", False))

        # Same real poster already shown — skip
        if new_path and new_path == getattr(self, "_cover_path", None):
            if child is not None and not is_placeholder:
                return
        # Already showing the No Cover placeholder — skip
        if not new_path and getattr(self, "_cover_path", None) is None and is_placeholder:
            return

        if child:
            self.cover_frame.remove(child)

        self._cover_path = new_path
        pw, ph = self._sync_poster_column_size()

        if new_path:
            radius = S(COVER_CORNER_RADIUS)
            border = max(1, S(2))
            pix = get_cover_pixbuf(new_path, pw, ph, radius, border)
            if pix is not None:
                img = Gtk.Image.new_from_pixbuf(pix)
                img.set_halign(Gtk.Align.START)
                img.set_valign(Gtk.Align.START)
                img.set_hexpand(False)
                img.set_vexpand(False)
                img.set_size_request(pw, ph)
                self.cover_frame.add(img)
                self.cover_frame.show_all()
                self._update_backdrop(pix, src_path=new_path)
                self._fit_description()
                return
            self._cover_path = None

        placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        placeholder.set_size_request(pw, ph)
        placeholder.set_valign(Gtk.Align.CENTER)
        placeholder.set_halign(Gtk.Align.CENTER)
        placeholder.set_hexpand(False)
        placeholder.set_vexpand(False)
        placeholder.get_style_context().add_class("no-cover-box")
        placeholder._is_no_cover = True
        lbl = Gtk.Label(label="No Cover")
        lbl.get_style_context().add_class("no-cover-label")
        lbl.set_halign(Gtk.Align.CENTER)
        lbl.set_valign(Gtk.Align.CENTER)
        placeholder.pack_start(lbl, True, True, 0)
        self.cover_frame.add(placeholder)
        self.cover_frame.show_all()
        self.bg.clear_backdrop()
        self._backdrop_src = None
        self._fit_description()

    def _window_pixel_width(self):
        """Best-effort real window/screen width (works even right after show)."""
        # 1) GTK allocation
        try:
            w = self.get_allocated_width() or 0
            if w >= 400:
                return w
        except Exception:
            pass
        # 2) Gdk window
        try:
            gdk_win = self.get_window()
            if gdk_win is not None:
                w = gdk_win.get_width()
                if w >= 400:
                    return w
        except Exception:
            pass
        # 3) Monitor geometry
        try:
            display = self.get_display()
            mon = None
            gdk_win = self.get_window()
            if gdk_win is not None:
                mon = display.get_monitor_at_window(gdk_win)
            if mon is None:
                mon = display.get_primary_monitor()
            if mon is not None:
                return mon.get_geometry().width
        except Exception:
            pass
        return 1920

    def _banner_target_size(self):
        """Stable size from real window width, accounting for all chrome.

        Must leave room for: page margins, column spacing, poster column,
        frame borders, and a small safety margin (avoids ~30px right clip).
        """
        win_w = self._window_pixel_width()
        border = max(1, S(2))
        chrome = (
            S(16) + S(16)          # media_page left/right margins
            + S(14)                # spacing between left_col and right_col
            + S(POSTER_W)          # poster content width
            + 2 * border           # poster frame border
            + 2 * border           # banner frame border
            + S(28)                # safety (scrollbar / scale rounding)
        )
        avail = max(S(320), win_w - chrome)
        tw = max(S(320), min(S(BANNER_MAX_W), int(avail)))
        th = max(S(36), int(round(tw * BANNER_ASPECT_H / float(BANNER_ASPECT_W))))
        return tw, th

    def _apply_banner_pixbuf(self, path):
        """Always center-crop to 379:70, scale, round + border (no feedback loop)."""
        tw, th = self._banner_target_size()
        radius = S(COVER_CORNER_RADIUS)
        border = max(1, S(2))
        if (
            getattr(self, "_banner_applied_path", None) == path
            and getattr(self, "_banner_applied_size", None) == (tw, th, radius, border)
            and self.banner_frame.get_child() is not None
        ):
            return tw, th
        full = get_banner_src_pixbuf(path)
        if full is None:
            raise FileNotFoundError(path)
        # Always re-crop from full source so aspect is exact for every file
        bkey = (path, tw, th, radius, border)
        cache = getattr(self, "_banner_scaled_cache", None)
        if cache is None:
            self._banner_scaled_cache = {}
            cache = self._banner_scaled_cache
        pix = cache.get(bkey)
        if pix is None:
            pix = pixbuf_fit_aspect(
                full, tw, th,
                radius=radius,
                border_px=border,
                border_rgba=(1, 1, 1, 0.32),
            )
            cache[bkey] = pix
            if len(cache) > 40:
                try:
                    del cache[next(iter(cache))]
                except Exception:
                    pass
        child = self.banner_frame.get_child()
        if child:
            self.banner_frame.remove(child)
        img = Gtk.Image.new_from_pixbuf(pix)
        img.set_halign(Gtk.Align.START)
        img.set_valign(Gtk.Align.START)
        img.set_hexpand(False)
        img.set_vexpand(False)
        img.set_size_request(tw, th)
        self.banner_frame.add(img)
        self.banner_frame.set_size_request(tw, th)
        self.banner_frame.set_hexpand(False)
        self.banner_frame.set_halign(Gtk.Align.START)
        self.banner_frame.show_all()
        self._banner_applied_path = path
        self._banner_applied_size = (tw, th, radius, border)
        return tw, th

    def _set_banner(self, banner_path):
        """Show banner at a stable size. Same path → no-op."""
        new_path = str(banner_path) if banner_path else None
        if new_path and not Path(new_path).is_file():
            new_path = None

        if new_path and new_path == getattr(self, "_banner_path", None):
            if self.banner_frame.get_child() is not None:
                return

        self._banner_path = new_path

        if not self._banner_path:
            child = self.banner_frame.get_child()
            if child:
                self.banner_frame.remove(child)
            self._banner_applied_path = None
            self._banner_applied_size = None
            self.banner_frame.set_size_request(-1, -1)
            self.banner_frame.hide()
            return

        try:
            tw, th = self._apply_banner_pixbuf(self._banner_path)
            print(f"Banner: {self._banner_path} → {tw}x{th}")
        except Exception as e:
            print("banner load error:", e)
            import traceback
            traceback.print_exc()
            self.banner_frame.hide()

    def _cancel_loading_fade(self):
        fid = getattr(self, "_loading_fade_id", None)
        if fid is not None:
            try:
                GLib.source_remove(fid)
            except Exception:
                pass
            self._loading_fade_id = None

    def _show_loading(self):
        """Fade in solid #17101e overlay + spinner above the browser."""
        try:
            self._cancel_loading_fade()
            self.loading_spinner.start()
            self.loading.show_all()
            self.loading.show()
            # Always on top of browser content
            try:
                self.overlay.reorder_overlay(self.loading, -1)
            except Exception:
                pass
            self._loading_visible = True
            # Fade in from current opacity (0 if freshly shown)
            start = float(self.loading.get_opacity() or 0.0)
            if start >= 0.99:
                self.loading.set_opacity(1.0)
                return
            steps = 8
            step_ms = 20  # ~160ms total
            i = [0]

            def _fade_in():
                i[0] += 1
                t = min(1.0, i[0] / float(steps))
                # ease-out
                e = 1.0 - (1.0 - t) ** 2
                self.loading.set_opacity(start + (1.0 - start) * e)
                if t >= 1.0:
                    self.loading.set_opacity(1.0)
                    self._loading_fade_id = None
                    return False
                return True

            self._loading_fade_id = GLib.timeout_add(step_ms, _fade_in)
        except Exception as e:
            print("show loading error:", e)

    def _hide_loading(self):
        """Fade out overlay, then hide + stop spinner."""
        try:
            self._cancel_loading_fade()
            if not self.loading.get_visible():
                self.loading_spinner.stop()
                self._loading_visible = False
                return False
            start = float(self.loading.get_opacity() or 1.0)
            steps = 6
            step_ms = 20  # ~120ms
            i = [0]

            def _fade_out():
                i[0] += 1
                t = min(1.0, i[0] / float(steps))
                e = t * t  # ease-in
                self.loading.set_opacity(start * (1.0 - e))
                if t >= 1.0:
                    self.loading.set_opacity(0.0)
                    self.loading_spinner.stop()
                    self.loading.hide()
                    self._loading_visible = False
                    self._loading_fade_id = None
                    return False
                return True

            self._loading_fade_id = GLib.timeout_add(step_ms, _fade_out)
        except Exception as e:
            print("hide loading error:", e)
            try:
                self.loading_spinner.stop()
                self.loading.hide()
            except Exception:
                pass
            self._loading_visible = False
        return False

    def _update_backdrop(self, pixbuf, src_path=None):
        """Optional blurred/darkened poster — disk + memory cache."""
        if not ui_bool("background", True) or pixbuf is None:
            self.bg.clear_backdrop()
            self._backdrop_src = None
            return
        path = str(src_path) if src_path else None
        try:
            darken = ui_float("background-darken", 0.55)
            do_blur = ui_bool("background-blur", True)
            max_side = 640
            # Memory key includes settings so changing darken/blur invalidates cache
            mem_key = f"{path}|{darken:.3f}|{int(do_blur)}" if path else None
            mem = getattr(self, "_backdrop_mem", None)
            if mem is None:
                self._backdrop_mem = {}
                mem = self._backdrop_mem
            if mem_key and mem_key in mem:
                self.bg.set_backdrop(mem[mem_key])
                self._backdrop_src = path
                return
            if path and Path(path).is_file():
                processed = load_or_build_blurred_backdrop(
                    path, pixbuf, darken=darken, do_blur=do_blur, max_side=max_side,
                )
            else:
                w, h = pixbuf.get_width(), pixbuf.get_height()
                work = pixbuf
                if max(w, h) > max_side:
                    s = max_side / float(max(w, h))
                    work = pixbuf.scale_simple(
                        max(1, int(w * s)), max(1, int(h * s)),
                        GdkPixbuf.InterpType.BILINEAR,
                    )
                processed = pixbuf_blur_darken(work, darken=darken, do_blur=do_blur)
            if mem_key:
                mem[mem_key] = processed
            self.bg.set_backdrop(processed)
            self._backdrop_src = path
        except Exception as e:
            print("backdrop error:", e)
            self.bg.clear_backdrop()
            self._backdrop_src = None

    def _fit_description(self):
        """Description locked to the same width as the poster."""
        pw, _ph = self._sync_poster_column_size()
        pad_x = S(20)
        inner = max(S(40), pw - pad_x)
        chars = max(18, inner // max(1, S(8)))
        self.desc_label.set_max_width_chars(chars)
        self.desc_label.set_width_chars(chars)
        self.desc_label.set_size_request(inner, -1)
        self.desc_label.set_halign(Gtk.Align.FILL)
        self.desc_label.set_hexpand(False)
        self.desc_frame.set_size_request(pw, -1)
        self.desc_frame.set_halign(Gtk.Align.START)
        self.desc_label.queue_resize()
        self.desc_frame.queue_resize()
        self.left_col.queue_resize()

    def _set_description(self, text: str):
        t = text.strip() if text and text.strip() else "No description."
        self.desc_label.set_text(t)
        self._fit_description()

    def _set_tech_badges(self, tech: dict | None = None):
        """Show/hide resolution, audio, video-codec badges."""
        tech = tech or {}

        def _set(box, lbl, text, markup=None):
            if text:
                if markup:
                    lbl.set_markup(markup)
                else:
                    lbl.set_text(text)
                lbl.show()
                box.show()
            else:
                lbl.set_text("")
                box.hide()

        _set(
            self.res_badge_box, self.res_badge,
            tech.get("res") or "",
            tech.get("res_markup") or None,
        )
        _set(self.audio_badge_box, self.audio_badge, tech.get("audio") or "")
        _set(self.vcodec_badge_box, self.vcodec_badge, tech.get("vcodec") or "")

    def _set_heading(self, title: str, year: str = "", tech: dict | None = None):
        """Title + optional year pill + optional tech badges."""
        self.browser_title.set_text(title or "")
        y = _normalize_year(year) if year else ""
        if y:
            self.year_badge.set_text(y)
            # no_show_all=True blocks show_all() on this widget — use show()
            self.year_badge.show()
            self.year_badge_box.show()
        else:
            self.year_badge.set_text("")
            self.year_badge_box.hide()
        self._set_tech_badges(tech)

    def _apply_folder_meta(self, meta: dict):
        self._folder_meta = meta
        self._set_heading(meta.get("title") or "Folder", meta.get("year") or "")
        # Force banner path check so a different folder banner can load,
        # but size stays stable (window-based formula).
        self._banner_applied_path = None
        self._banner_applied_size = None
        self._banner_path = None
        self._set_cover(meta.get("cover"))
        self._set_banner(meta.get("banner"))
        self._set_description(meta.get("plot") or "")

    def _on_row_focus(self, row, *_args):
        """Update poster / banner / plot — debounced so fast scrolling skips work."""
        self._pending_focus_row = row
        # Longer debounce: skip mid-scroll work; tech probe is async anyway
        fid = getattr(self, "_focus_idle_id", None)
        if fid is not None:
            try:
                GLib.source_remove(fid)
            except Exception:
                pass
            self._focus_idle_id = None

        def _apply():
            self._focus_idle_id = None
            r = getattr(self, "_pending_focus_row", None)
            if r is None:
                return False
            self._apply_row_info(r)
            self._prefetch_neighbors(r)
            return False

        self._focus_idle_id = GLib.timeout_add(90, _apply)
        return False

    def _on_tech_ready(self, path_str, tech):
        """Apply async ffprobe result only if that file is still focused."""
        try:
            row = getattr(self, "_pending_focus_row", None)
            if row is None or str(getattr(row, "_path", "")) != path_str:
                return False
            # Only update badges — don't rebuild cover/banner again
            self._set_tech_badges(tech)
        except Exception:
            pass
        return False

    def _prefetch_neighbors(self, row):
        """Warm poster/banner/meta/tech cache for nearby rows (non-blocking)."""
        try:
            rows = self.vlist.rows
            if not rows:
                return
            idx = self.vlist.current_index
            for j in (idx - 2, idx - 1, idx + 1, idx + 2):
                if j < 0 or j >= len(rows):
                    continue
                p = getattr(rows[j], "_path", None)
                if not p:
                    continue
                path = Path(p)
                if not (path.is_file() and is_video(path)):
                    continue
                meta = load_file_meta(path)
                c = meta.get("cover")
                if c:
                    get_cover_pixbuf(c)
                b = meta.get("banner")
                if b:
                    get_banner_src_pixbuf(b)
                # Warm tech cache in background (no UI callback needed)
                if probe_media_tech_cached(path) is None:
                    probe_media_tech_async(path, None)
        except Exception:
            pass

    def _apply_row_info(self, row):
        folder_title = self._folder_meta.get("title") or (
            folder_name(self.current_dir) if self.current_dir else "Folder"
        )
        folder_plot = self._folder_meta.get("plot") or ""
        folder_year = self._folder_meta.get("year") or ""
        folder_cover = self._folder_meta.get("cover")
        folder_banner = self._folder_meta.get("banner")

        if not row._path:
            self._set_heading(folder_title, folder_year, None)
            self._set_cover(folder_cover)
            self._set_banner(folder_banner)
            self._set_description(folder_plot)
            return

        p = Path(row._path)
        if p.is_file() and is_video(p):
            ep = load_file_meta(p)
            # Title: only switch when this file has its own NFO title.
            # Otherwise keep the folder name / folder NFO title.
            file_title = (ep.get("title") or "").strip()
            title = file_title if file_title else folder_title
            plot = ep.get("plot") or folder_plot
            year = ep.get("year") or folder_year
            cover = ep.get("cover") or folder_cover
            # Own file banner first. If subdirectory-banner=yes, fall back to
            # the current folder's banner (which may be inherited from the show).
            # Never scan the folder for unrelated MovieName_Banner files.
            banner = ep.get("banner")
            if banner is None and ui_bool("subdirectory-banner", True):
                banner = folder_banner
            # Never block the UI thread on ffprobe — use cache or async
            tech = probe_media_tech_cached(p)
            self._set_heading(title, year, tech)
            if tech is None:
                probe_media_tech_async(p, self._on_tech_ready)
            self._set_cover(cover)
            self._set_banner(banner)
            self._set_description(plot)
        else:
            # Subfolder selected: keep the *current* show/folder title & year.
            # (Season names live in the list — the heading stays on the show.)
            self._set_heading(folder_title, folder_year, None)
            self._set_cover(folder_cover)
            self._set_banner(folder_banner)
            self._set_description(folder_plot)

    def _show_root(self, focus_path: str = None):
        global DIRECTORIES, MPV_USER_OPTS, UI_OPTS
        DIRECTORIES, MPV_USER_OPTS, UI_OPTS = load_user_config()
        refresh_target_from_config()
        self.bg.clear_backdrop()
        self._backdrop_src = None
        self._cover_path = None

        self.path_stack = []
        self._meta_stack = []
        self._folder_meta = {}
        self.current_dir = None
        self._set_heading("Directories", "")
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

    def _folder_will_show_banner(self, directory: Path, parent_meta: dict | None = None) -> bool:
        """True if this folder contains any banner image assets.

        Triggers the loading overlay when entering folders like /Movies that
        hold many MovieName_Banner.jpg files (not only FolderName_Banner).
        Also true when the folder itself / parent inherits a banner path.
        """
        try:
            # Folder or inherited meta already points at a banner
            meta = load_dir_meta(directory, parent_meta=parent_meta or {})
            b = meta.get("banner")
            if b and Path(b).is_file():
                return True
            # Any *_Banner.* / banner.* / fanart.* image in this directory
            try:
                for f in directory.iterdir():
                    if not f.is_file():
                        continue
                    if f.suffix.lower() not in COVER_EXTS:
                        continue
                    stem = f.stem.lower()
                    if stem.endswith("_banner") or stem in ("banner", "fanart"):
                        return True
            except (PermissionError, OSError):
                pass
        except Exception:
            pass
        return False

    def _fill_vertical_list(self, path: str, focus_path: str = None, show_loading: bool = False):
        """Populate media list + cover/description for a folder."""
        p = Path(path)
        parent_meta = self._meta_stack[-1] if self._meta_stack else {}

        if show_loading:
            self._show_loading()
            # Let the loading overlay paint before heavy image work
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)

        self.vlist.clear()
        meta = load_dir_meta(p, parent_meta=parent_meta)
        self._apply_folder_meta(meta)

        try:
            entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except (PermissionError, OSError):
            entries = []

        folders = [e for e in entries if is_dir(e)]
        videos = [e for e in entries if is_video(e)]

        if not folders and not videos:
            row = ListRow("(empty folder)", kind="folder", badge="")
            self.vlist.add_row(row)
        else:
            for f in folders:
                row = ListRow(f.name, kind="folder", badge="")
                row._path = str(f)
                row.connect("button-press-event",
                            lambda w, e, pth=str(f): self._enter_path(pth))
                row.connect("focus-in-event", self._on_row_focus)
                self.vlist.add_row(row)
            for v in videos:
                badge = history_badge(str(v))
                row = ListRow(v.name, kind="video", badge=badge)
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
            # Focus first row under the loading overlay so its banner
            # is applied before we reveal the UI.
            self.vlist.focus_index(focus_idx, animate=False)
            # Apply row info immediately (skip debounce) while still covered
            if self.vlist.rows:
                try:
                    self._apply_row_info(self.vlist.rows[focus_idx])
                except Exception:
                    pass
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)

        if show_loading:
            # Keep overlay up long enough for banner scale + layout to settle
            gen = getattr(self, "_loading_gen", 0) + 1
            self._loading_gen = gen

            def _reveal(g=gen):
                if getattr(self, "_loading_gen", 0) != g:
                    return False
                while Gtk.events_pending():
                    Gtk.main_iteration_do(False)
                self._hide_loading()
                return False

            GLib.timeout_add(400, _reveal)

    def _enter_path(self, path: str):
        p = Path(path)
        if not p.is_dir():
            return
        parent_meta = dict(self._folder_meta) if self.current_dir is not None else {}
        need_loading = self._folder_will_show_banner(p, parent_meta)
        self.path_stack.append(self.current_dir)
        if self.current_dir is not None:
            self._meta_stack.append(dict(self._folder_meta))
        self.current_dir = path
        self._fill_vertical_list(path, show_loading=need_loading)

    def _rebuild_folder(self, path: str, focus_path: str = None):
        self.current_dir = path
        parent_meta = self._meta_stack[-1] if self._meta_stack else {}
        need_loading = self._folder_will_show_banner(Path(path), parent_meta)
        self._fill_vertical_list(path, focus_path=focus_path, show_loading=need_loading)

    # ── Playback (standalone fullscreen mpv – works on Wayland) ─────────────
    def _start_playback(self, path: str):
        global MPV_USER_OPTS, UI_OPTS
        _, MPV_USER_OPTS, UI_OPTS = load_user_config()

        self.video_path = path
        self.mode = "player"
        # Loading screen until mpv is up, then hide GTK window
        self._show_loading()
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

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
                #osd_border_color="#FF49007D",
                osd_shadow_offset=2,
                osd_bar_align_y=0.92,
                osd_duration=3000,
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
            # Video started — drop GTK loading screen / window
            self.hide()
            self._hide_loading()

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

    def _force_banner_reload(self):
        """Clear cache and reload current folder banner at real pixel size."""
        path = None
        if getattr(self, "_folder_meta", None):
            path = self._folder_meta.get("banner")
        if not path:
            path = getattr(self, "_banner_path", None)
        self._banner_path = None
        self._banner_applied_path = None
        self._banner_applied_size = None
        if path:
            self._set_banner(path)
        return False

    def _return_to_browser(self):
        if self._poll_id:
            try:
                GLib.source_remove(self._poll_id)
            except Exception:
                pass
            self._poll_id = None

        self._save_current_progress()
        resume_dir = self.current_dir
        resume_focus = self.video_path

        self.mode = "browser"
        self.show()
        self.present()
        self.fullscreen()
        # Cover banner resize with loading screen until layout settles
        self._show_loading()

        def _restore_ui():
            if resume_dir:
                self._rebuild_folder(resume_dir, focus_path=resume_focus)
            else:
                self._show_root()
            # Banner was often measured while still hidden (width=0).
            GLib.timeout_add(80, self._force_banner_reload)
            GLib.timeout_add(250, self._force_banner_reload)
            # Hide loading after banner has had time to settle
            GLib.timeout_add(320, self._hide_loading)
            return False

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

        GLib.idle_add(_restore_ui)
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
