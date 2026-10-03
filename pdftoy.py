"""pdftoy — PDF page size unification tool (CLI / GUI in one)

Usage:
    pdftoy                 # launch the graphical interface
    pdftoy input.pdf       # process in command-line mode
    pdftoy -h              # show all command-line arguments
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from contextlib import closing

# ── Reattach CLI logs to the terminal under a windowed build (re-attach to the parent shell; fall back when no terminal) ──
_attached_shell_name = ""  # Records the shell name when AttachConsole succeeds; the exit hook uses it to decide whether to inject Enter


def _find_shell_ancestor_pid():
    """Walk the process tree upward to find the terminal-shell ancestor (cmd / powershell / pwsh / wt).

    Returns (PID, shell_name); returns (0, "") if not found.

    Note: PyInstaller's --onefile bootloader re-spawns the real python child process,
    so ATTACH_PARENT_PROCESS attaches to the console-less bootloader. We must explicitly
    locate the shell ancestor's PID and AttachConsole(pid) to it.
    """
    if os.name != "nt":
        return 0, ""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_ulong),
                ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_char * 260),
            ]

        shell_names = {"cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe"}
        h = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if h == -1:
            return 0, ""
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        parent_map = {}
        if kernel32.Process32First(h, ctypes.byref(entry)):
            while True:
                name = entry.szExeFile.decode("mbcs", "ignore").lower()
                parent_map[entry.th32ProcessID] = (entry.th32ParentProcessID, name)
                if not kernel32.Process32Next(h, ctypes.byref(entry)):
                    break
        kernel32.CloseHandle(h)

        pid = os.getpid()
        seen = set()
        while pid and pid in parent_map and pid not in seen:
            seen.add(pid)
            ppid, name = parent_map[pid]
            if name in shell_names:
                return pid, name
            pid = ppid
        return 0, ""
    except Exception:
        # Snapshot/walk failed: let the caller fall back to AllocConsole
        return 0, ""


class _ConsoleWriter:
    """Minimal text stream that writes Unicode directly via WriteConsoleW, independent of the console codepage (no mojibake)."""

    def __init__(self, kernel32, handle):
        import ctypes

        self._ct = ctypes
        self._write = kernel32.WriteConsoleW
        self._write.argtypes = (
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
        )
        self._handle = handle
        self.vt_enabled = False  # Whether VT sequences (line erase, etc.) are available; set by _make_console_stream

    def write(self, text):
        if text:
            # WriteConsoleW does not translate LF -> CRLF: a bare \n only moves to the next line without
            # returning to column 0, leaving the shell prompt mid-line until Enter is pressed. Normalize to \r\n.
            if "\n" in text:
                text = text.replace("\r\n", "\n").replace("\n", "\r\n")
            written = self._ct.c_ulong(0)
            self._write(self._handle, text, len(text), self._ct.byref(written), None)
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return True

    def writable(self):
        return True

    def readable(self):
        return False

    def seekable(self):
        return False

    def close(self):
        pass

    @property
    def closed(self):
        return False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _make_console_stream():
    """Open CONOUT$ and return a direct Unicode output stream; returns None on failure."""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.SetStdHandle.argtypes = (ctypes.c_int, ctypes.c_void_p)
    kernel32.SetStdHandle.restype = ctypes.c_int
    kernel32.CreateFileW.argtypes = (
        ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
    )
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.SetConsoleMode.argtypes = (ctypes.c_void_p, ctypes.c_ulong)

    # GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, OPEN_EXISTING
    handle = kernel32.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)
    if not handle or handle == 0xFFFFFFFFFFFFFFFF:
        return None
    kernel32.SetConsoleMode.restype = ctypes.c_int
    # Process output | line-end translation | virtual terminal (VT color / line erase); old systems reject the VT bit and fall back to non-VT mode
    if kernel32.SetConsoleMode(handle, 0x7):
        vt = True
    else:
        kernel32.SetConsoleMode(handle, 0x3)
        vt = False
    kernel32.SetStdHandle(-11, handle)  # STD_OUTPUT_HANDLE
    kernel32.SetStdHandle(-12, handle)  # STD_ERROR_HANDLE
    stream = _ConsoleWriter(kernel32, handle)
    stream.vt_enabled = vt
    return stream


def _ensure_cli_console():
    """Reattach CLI logs to the terminal (windowed build only; skipped when running from source where sys.stdout works).

    1) stdout already usable (redirected > file, or running from source in a terminal) -> leave it alone;
    2) a terminal shell in the ancestors -> AttachConsole(its PID); logs go to the original terminal, no new window;
    3) no terminal at all (PDF dragged onto the exe) -> AllocConsole opens a log window, closed when the process exits.

    Output goes through WriteConsoleW writing Unicode directly, independent of the console codepage (no Chinese mojibake).
    Note: cmd / PowerShell / WT do not wait for, nor redraw the prompt after, a GUI-subsystem exe
    (shells only wait for console-subsystem programs that share their console; the windowed exe does not share,
    see SO 1305257). Fix: on exit, inject a pair of Enter key events into the original terminal's input buffer,
    so the shell immediately redraws its prompt (_inject_enter_for_shell).
    """
    global _attached_shell_name
    if os.name != "nt" or sys.stdout is not None:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        pid, shell_name = _find_shell_ancestor_pid()
        if pid:
            if not kernel32.AttachConsole(pid):
                pid = 0
                kernel32.AllocConsole()
        else:
            kernel32.AllocConsole()
        _attached_shell_name = shell_name if pid else ""
        stream = _make_console_stream()
        if stream is None:
            return
        sys.stdout = stream
        sys.stderr = stream
        if _attached_shell_name:
            # The shell already drew its new prompt after launching this process (cursor sits right after it).
            # With VT: return to line start and erase the whole prompt line, so logs start on a clean line;
            # the Enter injected on exit makes the shell redraw its prompt.
            # Without VT: only return to line start (rare, old systems).
            stream.write("\r\x1b[2K" if stream.vt_enabled else "\r")
    except Exception:
        return
    # Re-evaluate the color switch after reattaching. This runs before _color_supported is defined (early at module load),
    # so skip then -- the later COLOR_ENABLED = _color_supported() re-evaluates against the reattached stream.
    if "_color_supported" in globals():
        global COLOR_ENABLED
        COLOR_ENABLED = _color_supported()
    # Shells (cmd/PS/WT) do not wait for the GUI-subsystem process to exit: register an exit hook that presses Enter for the user.
    if _attached_shell_name:
        import atexit

        atexit.register(_inject_enter_for_shell)


def _inject_enter_for_shell():
    """Inject a pair of Enter key events into the console input buffer so the shell redraws its prompt immediately.

    Background: after a windowed exe re-attaches to the terminal via AttachConsole, the shell (cmd/PowerShell/WT
    behave the same) drew its prompt at launch and does not wait for us to exit (SO 1305257).
    Injecting a VK_RETURN key event (WriteConsoleInputW writes CONIN$) is equivalent to the user pressing Enter,
    so the shell returns to its prompt at once. We write a down+up pair of events to mimic a real key press.
    Only injected when AttachConsole succeeded (a real terminal was re-attached).
    """
    if not _attached_shell_name:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # INPUT_RECORD: EventType=KEY_EVENT(1) + KEY_EVENT_RECORD(16 bytes)
        class _KEY_EVENT_RECORD(ctypes.Structure):
            _fields_ = [
                ("bKeyDown", ctypes.c_int),
                ("wRepeatCount", ctypes.c_ushort),
                ("wVirtualKeyCode", ctypes.c_ushort),
                ("wVirtualScanCode", ctypes.c_ushort),
                ("uChar", ctypes.c_wchar),
                ("dwControlKeyState", ctypes.c_ulong),
            ]

        class _INPUT_RECORD(ctypes.Structure):
            class _Event(ctypes.Union):
                _fields_ = [("KeyEvent", _KEY_EVENT_RECORD)]

            _fields_ = [("EventType", ctypes.c_ushort), ("Event", _Event)]

        # Open CONIN$ (needs read+write access for WriteConsoleInputW)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        h = kernel32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
        if not h or h == 0xFFFFFFFFFFFFFFFF:
            return
        try:
            records = (_INPUT_RECORD * 2)()
            # key down
            down = records[0]
            down.EventType = 1  # KEY_EVENT
            ev = down.Event.KeyEvent
            ev.bKeyDown = 1
            ev.wRepeatCount = 1
            ev.wVirtualKeyCode = 0x0D  # VK_RETURN
            ev.wVirtualScanCode = 0x1C
            ev.uChar = "\r"
            # key up
            up = records[1]
            up.EventType = 1
            ev = up.Event.KeyEvent
            ev.bKeyDown = 0
            ev.wRepeatCount = 1
            ev.wVirtualKeyCode = 0x0D
            ev.wVirtualScanCode = 0x1C
            ev.uChar = "\r"
            written = ctypes.c_ulong(0)
            kernel32.WriteConsoleInputW(
                h, ctypes.byref(records[0]), 2, ctypes.byref(written)
            )
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        pass


# windowed build: running with args means CLI -- reattach the terminal before pymupdf / tkinter is imported;
# double-click GUI (no args) is unaffected.
if os.name == "nt" and len(sys.argv) > 1:
    _ensure_cli_console()

try:
    import pymupdf as fitz  # PyMuPDF (pymupdf is the canonical name since 1.24+)
except ImportError:
    import fitz  # PyMuPDF (legacy fallback for older versions)

import queue
import threading
import subprocess

# tkinter ships with the Python standard library, needed only in GUI mode; CLI degrades gracefully if missing
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, scrolledtext
    _TK_AVAILABLE = True
except ImportError:
    tk = ttk = filedialog = scrolledtext = None
    _TK_AVAILABLE = False

# ================= Global metadata & config flags =================
__version__ = "1.2.1"

DEBUG_TOC = False  # True: dump the raw extracted bookmarks, handy for debugging odd PDFs
STRICT_TOC = False  # True: enable strict mode, raising the bookmark detection threshold

# Bookmark diagnostics & performance tuning constants
EARLY_STOP_THRESHOLD = 0.85  # Top-quality preset: stop early once this share of the scanned sample is reached

# Height safety bounds (unit: pt; only enforced in fit_width mode)
MIN_PAGE_HEIGHT = 100.0  # ~3.5 cm
MAX_PAGE_HEIGHT = 5000.0  # ~1.76 m (guards against runaway memory use)
# =======================================================


# =================== Logging system ====================
class Level:
    INFO = "INFO"
    OK = "OK"
    WARN = "WARN"
    ERROR = "ERROR"
    DEBUG = "DEBUG"


# Log prefix: plain-text level name (fixed-width ASCII; no transcoding needed on any terminal)
LEVEL_STYLE = {
    Level.INFO: "INFO",
    Level.OK: "OK",
    Level.WARN: "WARN",
    Level.ERROR: "ERROR",
    Level.DEBUG: "DEBUG",
}

# ANSI escape codes (control sequences only, never text content; safe in any encoding)
_ANSI_RESET = "\033[0m"
_LEVEL_COLOR = {
    Level.INFO: "\033[36m",   # cyan
    Level.OK: "\033[32m",     # green
    Level.WARN: "\033[33m",   # yellow
    Level.ERROR: "\033[31m",  # red
    Level.DEBUG: "\033[90m",  # gray
}


def _enable_ansi_on_windows():
    """Old Windows consoles disable ANSI by default; enable VT processing to show colors"""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            if not (mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING):
                kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        pass


def _color_supported():
    """Whether colored output is on: interactive terminals only, honoring NO_COLOR / FORCE_COLOR"""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False  # disabled for pipes / file redirection, keeping the log file clean
    if os.name == "nt":
        _enable_ansi_on_windows()
    return True


COLOR_ENABLED = _color_supported()


def log(msg: str, level: str = Level.INFO):
    """Unified log output: plain ASCII level prefix; colors applied on interactive terminals"""
    prefix = LEVEL_STYLE.get(level, f"[{level}]")
    if COLOR_ENABLED and level in _LEVEL_COLOR:
        print(f"{_LEVEL_COLOR[level]}{prefix}{_ANSI_RESET} {msg}")
    else:
        print(f"{prefix} {msg}")


# ============================================


def calculate_toc_health(toc):
    """Compute the bookmark health score (0 - 100) efficiently"""
    total = len(toc)
    if total == 0:
        return 0, 0.0

    meaningful_score = 0.0
    junk_keywords = (
        "page",
        "link",
        "img",
        "doc",
        "figure",
        "uncategorized",
        "anchor",
    )

    for idx, item in enumerate(toc):
        level = item[0]
        title = str(item[1]).strip()

        if len(title) < 2 or title.isdigit():
            continue

        t_lower = title.lower()
        if any(k in t_lower for k in junk_keywords):
            continue

        score = 1.0
        if level <= 2:
            score += 0.5
        elif level > 4:
            score -= 0.3

        meaningful_score += max(0.0, score)

        if idx > 50 and (idx % 20 == 0):
            scanned = idx + 1
            current_ratio = meaningful_score / scanned
            if current_ratio >= EARLY_STOP_THRESHOLD:
                return min(100, int(current_ratio * 100)), current_ratio

    ratio = meaningful_score / total
    health_score = min(100, int(ratio * 100))
    return health_score, ratio


def find_best_ref_page(doc, start=2, end=15, min_size_pt=400):
    """Pick the best reference page automatically"""
    total = len(doc)
    if total <= start:
        return 0

    scan_end = min(end, total)
    sizes = []

    for i in range(start, scan_end):
        box = doc[i].rect
        sizes.append((round(box.width, 1), round(box.height, 1)))

    if not sizes:
        return 0

    most_common_size = Counter(sizes).most_common(1)[0][0]

    if most_common_size[0] < min_size_pt or most_common_size[1] < min_size_pt:
        log(
            f"Detected the most frequent size {most_common_size} "
            f"below the safety threshold ({min_size_pt}pt) -- abrupt-change guard tripped, falling back to page 1",
            Level.WARN,
        )
        return 0

    for i in range(start, scan_end):
        box = doc[i].rect
        if (round(box.width, 1), round(box.height, 1)) == most_common_size:
            return i

    return min(4, total - 1)


# ============================================================
# Two page-standardization implementations (per-page handling)
#   method="matrix" : insert_pdf whole-doc copy + cm affine matrix wrapping of the content stream
#   method="reflow" : per-page new_page + show_pdf_page(clip=...) re-render
# Both give zero drift for raster / text PDFs (matrix additionally keeps hyperlinks)
# ============================================================

def _compute_page_matrix(page, target_w, target_h, fit_width, page_index=None):
    """Compute a single page's transform matrix and target height (pure math, no doc mutation)

    Returns (fitz.Matrix, new_h). page_index identifies the page in logs.
    """
    src_box = page.rect
    orig_w = src_box.width
    orig_h = src_box.height

    if fit_width:
        if orig_w <= 0:
            orig_w = target_w

        scale = target_w / orig_w
        calculated_h = orig_h * scale
        new_h = max(MIN_PAGE_HEIGHT, min(calculated_h, MAX_PAGE_HEIGHT))

        if calculated_h > MAX_PAGE_HEIGHT or calculated_h < MIN_PAGE_HEIGHT:
            idx_str = f"Page {page_index + 1}" if page_index is not None else "(some page)"
            log(
                f"{idx_str} scaled height ({calculated_h:.1f}pt) hit the safety limit,"
                f"corrected to: {new_h:.1f}pt",
                Level.WARN,
            )

        s = scale
        dx = 0.0
        dy = 0.0
    else:
        scale_w = target_w / orig_w if orig_w > 0 else 1.0
        scale_h = target_h / orig_h if orig_h > 0 else 1.0
        s = min(scale_w, scale_h)

        dx = (target_w - orig_w * s) / 2.0
        dy = (target_h - orig_h * s) / 2.0
        new_h = target_h

    # A source page may carry a cropbox origin offset; it must be folded into the translation, otherwise the whole content drifts
    cb = page.cropbox
    tx = dx - cb.x0 * s
    ty = dy - cb.y0 * s
    m = fitz.Matrix(s, 0, 0, s, tx, ty)
    return m, new_h


def _rebuild_named_links(src_doc, dst_doc):
    """Convert NAMED links to GOTO links and rebuild them into dst_doc

    insert_pdf(links=True) does not copy NAMED-type link annotations -- they reference the PDF's
    /Names dictionary, which insert_pdf never copies, so NAMED links get dropped.
    This function reads links page by page from the source doc (get_links already resolves
    NAMED to page+to) and re-inserts them as GOTO links into the destination doc.
    """
    rebuilt = 0
    for i in range(len(src_doc)):
        src_links = src_doc[i].get_links()
        dst_page = dst_doc[i]
        for link in src_links:
            if link.get("kind") != fitz.LINK_NAMED:
                continue
            # get_links() already resolved the target page and coordinates of NAMED links
            target_page = link.get("page")
            if target_page is None or target_page < 0 or target_page >= len(dst_doc):
                continue
            new_link = {
                "kind": fitz.LINK_GOTO,
                "from": fitz.Rect(link["from"]),
                "page": target_page,
                "to": fitz.Point(link["to"]) if link.get("to") else None,
                "zoom": link.get("zoom", 0.0),
            }
            if link.get("quad"):
                new_link["quad"] = list(link["quad"])
            dst_page.insert_link(new_link)
            rebuilt += 1
    if rebuilt:
        log(f"Rebuilt {rebuilt} NAMED hyperlinks as GOTO type", Level.OK)
    return rebuilt


def _apply_matrix_page(dst_doc, page, i, target_w, target_h, fit_width, page_matrices=None):
    """Matrix method: wrap page i of the fully insert_pdf'd doc with a cm matrix"""
    src_box = page.rect
    orig_w = src_box.width
    orig_h = src_box.height

    if page_matrices:
        m, new_h = page_matrices[i]
    else:
        m, new_h = _compute_page_matrix(page, target_w, target_h, fit_width, i)

    new_page = dst_doc[i]

    # Wrap the raw content stream with the cm (concat matrix) operator and clip to the source mediabox,
    # exactly reproducing show_pdf_page(clip=page.rect) (no overflow / drift)
    old_content = new_page.read_contents()
    mat_cm = f"{m.a} {m.b} {m.c} {m.d} {m.e} {m.f} cm"
    clip_path = f"0 0 {orig_w} {orig_h} re W n"
    wrapped = (
        b"q\n"
        + mat_cm.encode("latin-1")
        + b"\n"
        + clip_path.encode("latin-1")
        + b"\n"
        + old_content
        + b"\nQ\n"
    )

    # Every page MUST get its OWN content-stream xref; wrap only this page's private copy.
    # Source PDFs often share one thin-shell content stream (e.g. q /fzFrm0 Do Q) across many pages.
    # Updating that shared xref via update_stream would re-wrap it on later pages,
    # nesting the cm matrix over and over (hundreds of levels in practice) and badly drifting the content.
    new_xref = dst_doc.get_new_xref()
    dst_doc.update_object(new_xref, "<<>>")
    dst_doc.update_stream(new_xref, wrapped, compress=True)
    new_page.set_contents(new_xref)

    # Unified canvas size (CropBox aligned too, so a source offset cannot clip or shift the visible area)
    unified_box = fitz.Rect(0, 0, target_w, new_h)
    new_page.set_mediabox(unified_box)
    # Key: the cropbox must come from the WRITTEN mediabox, not the raw unified_box.
    # set_mediabox rounds to a string, so the value read back into self.mediabox can differ
    # slightly (~1e-5) from the full-precision unified_box when new_h (orig_h * s) differs;
    # passing the raw unified_box to set_cropbox makes _set_pagebox compute
    # rect.y0 = mb.y1 - ub.y1 as a tiny negative -> bogus "CropBox not in MediaBox"
    # (fit-width only; a fixed canvas with new_h = target_h round-trips cleanly).
    new_page.set_cropbox(new_page.mediabox)

    # Transform hyperlink coordinates: a link rect is an independent annotation, so cm wrapping
    # of the content stream does not move it -- the same matrix m must map every link rect
    # (and the QuadPoints of text links) onto the new canvas. The link destination ("to")
    # scales with the same matrix, so an untransformed "to" lands off-target.
    for link in new_page.get_links():
        link_rect = fitz.Rect(link["from"])
        link["from"] = link_rect * m
        to_point = link.get("to")
        target_pg = link.get("page")
        if to_point is not None and target_pg is not None and page_matrices:
            if 0 <= target_pg < len(page_matrices):
                target_m = page_matrices[target_pg][0]
                link["to"] = fitz.Point(to_point) * target_m
        if link.get("quad"):
            link["quad"] = [fitz.Point(p) * m for p in link["quad"]]
        new_page.update_link(link)


def _apply_reflow_page(dst_doc, src_doc, page, i, target_w, target_h, fit_width):
    """Reflow method (legacy show_pdf_page): per-page new_page + show_pdf_page re-render"""
    src_box = page.rect
    orig_w = src_box.width
    orig_h = src_box.height

    if fit_width:
        if orig_w <= 0:
            orig_w = target_w

        scale = target_w / orig_w
        calculated_h = orig_h * scale
        new_h = max(MIN_PAGE_HEIGHT, min(calculated_h, MAX_PAGE_HEIGHT))

        if calculated_h > MAX_PAGE_HEIGHT or calculated_h < MIN_PAGE_HEIGHT:
            log(
                f"Page {i + 1} scaled height ({calculated_h:.1f}pt) hit the safety limit,"
                f"corrected to: {new_h:.1f}pt",
                Level.WARN,
            )

        new_page = dst_doc.new_page(width=target_w, height=new_h)
        fit_rect = fitz.Rect(0, 0, target_w, new_h)
        new_page.show_pdf_page(fit_rect, src_doc, i, clip=src_box)

        unified_box = fitz.Rect(0, 0, target_w, new_h)
        new_page.set_mediabox(unified_box)
    else:
        new_page = dst_doc.new_page(width=target_w, height=target_h)

        scale_w = target_w / orig_w if orig_w > 0 else 1.0
        scale_h = target_h / orig_h if orig_h > 0 else 1.0
        s = min(scale_w, scale_h)

        scaled_w = orig_w * s
        scaled_h = orig_h * s

        dx = (target_w - scaled_w) / 2.0
        dy = (target_h - scaled_h) / 2.0

        fit_rect = fitz.Rect(dx, dy, dx + scaled_w, dy + scaled_h)
        new_page.show_pdf_page(fit_rect, src_doc, i, clip=src_box)

        unified_box = fitz.Rect(0, 0, target_w, target_h)
        new_page.set_mediabox(unified_box)


def fix_pdf_scale_pro_module(
    input_pdf_path: str,
    output_pdf_path: str,
    auto_ref: bool = True,
    ref_page_index: int | None = None,
    fit_width: bool = False,
    method: str = "matrix",
) -> dict:
    """Main entry: full-featured, programmable, industrial-grade PDF page standardization

    method:
        "matrix" (default) -> insert_pdf whole-doc copy + cm matrix wrap; keeps text/images/vectors/hyperlinks
        "reflow"        -> per-page new_page + show_pdf_page re-render (legacy method)
    """
    if method not in ("matrix", "reflow"):
        raise ValueError(f"Unknown render method: {method!r} (expected 'matrix' or 'reflow')")

    with (
        closing(fitz.open(input_pdf_path)) as src_doc,
        closing(fitz.open()) as dst_doc,
    ):
        total_pages = len(src_doc)
        if total_pages == 0:
            raise ValueError("The input PDF has no pages!")

        if auto_ref:
            chosen_ref_index = find_best_ref_page(src_doc)
            log(
                f"[Page analysis] Auto-detected the best reference page: {chosen_ref_index + 1}",
                Level.INFO,
            )
        else:
            if ref_page_index is None:
                ref_page_index = 0
            chosen_ref_index = min(max(0, ref_page_index), total_pages - 1)
            log(
                f"[Page analysis] Using the manually specified reference page: {chosen_ref_index + 1}",
                Level.INFO,
            )

        ref_page = src_doc[chosen_ref_index]
        ref_box = ref_page.rect  # use rect instead of cropbox to avoid wrong cropping caused by offsets
        target_w = ref_box.width
        target_h = ref_box.height

        if fit_width:
            log(
                f"[Canvas] Mode: fit width | target width: {target_w:.2f} pt (height adapts dynamically)",
                Level.INFO,
            )
        else:
            log(
                f"[Canvas] Mode: fixed canvas | base size: {target_w:.2f} x {target_h:.2f} pt",
                Level.INFO,
            )

        log(
            f"[Render method] {'matrix (recommended, keeps hyperlinks)' if method == 'matrix' else 'reflow legacy show_pdf_page'}",
            Level.INFO,
        )

        # matrix: insert_pdf the whole book once (shared resources kept once, avoiding per-page
        # duplication that would bloat the file / hang on large inputs), then wrap each page's
        # content stream with a cm affine matrix for uniform scaling + centering.
        # reflow: per-page new_page + show_pdf_page, no pre-copy needed.
        page_matrices = None
        named_links_count = 0
        if method == "matrix":
            dst_doc.insert_pdf(
                src_doc, annots=False, links=True, widgets=False,
            )

            # Pre-compute transformation matrices for all pages (shared by content-stream wrapping and link "to" coordinate transform).
            page_matrices = [
                _compute_page_matrix(page, target_w, target_h, fit_width, i)
                for i, page in enumerate(src_doc)
            ]

            # Rebuild NAMED links as GOTO links
            # insert_pdf(links=True) does not copy NAMED links -- they reference the /Names named-target
            # dictionary, which insert_pdf never copies, so all TOC hyperlinks get dropped.
            # Read the resolved NAMED links from the source doc and re-insert them as GOTO links.
            named_links_count = _rebuild_named_links(src_doc, dst_doc)

        # Standardize pages one by one
        for i, page in enumerate(src_doc):
            if method == "matrix":
                _apply_matrix_page(dst_doc, page, i, target_w, target_h, fit_width, page_matrices)
            else:
                _apply_reflow_page(dst_doc, src_doc, page, i, target_w, target_h, fit_width)

        # Bookmark identification & filtering
        log("Start bookmark tree extraction and health check", Level.INFO)
        threshold = 0.8 if STRICT_TOC else 0.6

        toc_to_use = None
        final_health_score = 0
        used_mode = "none"

        toc_simple = src_doc.get_toc(simple=True)
        health_score_simple, ratio_simple = calculate_toc_health(toc_simple)

        if toc_simple and ratio_simple > threshold:
            toc_to_use = toc_simple
            final_health_score = health_score_simple
            used_mode = "simple"
            log(
                f"[Bookmark] Native bookmarks validated! Health score: {final_health_score}/100",
                Level.OK,
            )
        else:
            toc_full = src_doc.get_toc(simple=False)
            health_score_full, ratio_full = calculate_toc_health(toc_full)

            if toc_full and ratio_full > threshold:
                toc_to_use = toc_full
                final_health_score = health_score_full
                used_mode = "full"
                log(
                    "[Bookmark] Full mode (simple=False) restored! Health score: "
                    f"{final_health_score}/100",
                    Level.OK,
                )
            else:
                log(
                    "[Bookmark] No valid bookmarks found (TOC is empty or all entries are auto-generated placeholders); skipping bookmark restoration",
                    Level.WARN,
                )

        injected_count = 0
        if toc_to_use:
            valid_toc = [item for item in toc_to_use if 1 <= item[2] <= total_pages]
            filtered_count = len(toc_to_use) - len(valid_toc)

            if filtered_count > 0:
                log(
                    f"[Bookmark] Auto-dropped {filtered_count} invalid bookmarks pointing to nonexistent pages",
                    Level.WARN,
                )

            dst_doc.set_toc(valid_toc)
            injected_count = len(valid_toc)

        # Save strategy:
        # matrix -> garbage=1 (whole-book insert_pdf already shares resources; drop unused objects only)
        # reflow -> garbage=3 (per-page re-render; needs dedup + space compaction).
        garbage_level = 1 if method == "matrix" else 3
        dst_doc.save(
            output_pdf_path,
            use_objstms=True,
            deflate=True,
            garbage=garbage_level,
        )
        log(f"Processing complete. Saved to: {output_pdf_path}", Level.OK)

        return {
            "output_path": output_pdf_path,
            "total_pages": total_pages,
            "ref_page_index": chosen_ref_index,
            "method": method,
            "fit_width": fit_width,
            "target_size_pt": (round(target_w, 2), round(target_h, 2)),
            "toc_injected": bool(injected_count > 0),
            "toc_mode": used_mode,
            "toc_count": injected_count,
            "toc_health_score": final_health_score,
            "named_links_rebuilt": named_links_count if method == "matrix" else 0,
        }


def main():
    """Single entry point: no input file launches the GUI; an input PDF path switches to command-line mode."""
    # windowed build: running with args means CLI -- reattach logs to the terminal first (double-click GUI has no args, unaffected)
    if len(sys.argv) > 1:
        _ensure_cli_console()

    parser = argparse.ArgumentParser(
        prog="pdftoy",
        description=(
            "PDF page size unification tool\n"
            "[Default mode]: matrix method (insert_pdf + cm wrapping, zero drift, keeps hyperlinks)\n"
            "[Legacy mode]: add -l / --legacy for the show_pdf_page re-render method\n"
            "[Canvas mode]: canvas fixed to the reference page size by default; -w fits by width (height scales proportionally)\n"
            "\n"
            "Run with no arguments to launch the GUI; pass an input PDF path for command-line mode."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "input",
        nargs="?",
        default=None,
        help="path to the input PDF file (omit to launch the GUI)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="output PDF file path (default: appends the _fixed suffix automatically)",
        default=None,
    )
    parser.add_argument(
        "-w",
        "--fit-width",
        action="store_true",
        help="[Core switch] enable fit-by-width mode (height scales proportionally).",
    )
    parser.add_argument(
        "-p",
        "--page",
        type=int,
        metavar="PAGE_NUM",
        help="reference page number (1-based; auto-detected by default)",
    )
    parser.add_argument(
        "-l",
        "--legacy",
        action="store_true",
        help="use the legacy show_pdf_page re-render method (default: matrix method, recommended).",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="show the tool's current version",
    )

    args = parser.parse_args()

    # Mode decision: no input file -> GUI; otherwise command-line processing
    if args.input is None:
        # windowed build: GUI mode has no console window, so no hiding is needed
        launch_gui()
        return

    input_path = args.input
    if args.output:
        output_path = args.output
    else:
        if input_path.lower().endswith(".pdf"):
            output_path = input_path[:-4] + "_fixed.pdf"
        else:
            output_path = input_path + "_fixed.pdf"

    if args.page is not None:
        if args.page < 1:
            log("Error: the page number must be >= 1", Level.ERROR)
            sys.exit(1)
        auto_ref = False
        ref_index = args.page - 1
    else:
        auto_ref = True
        ref_index = None

    method = "reflow" if args.legacy else "matrix"

    try:
        fix_pdf_scale_pro_module(
            input_pdf_path=input_path,
            output_pdf_path=output_path,
            auto_ref=auto_ref,
            ref_page_index=ref_index,
            fit_width=args.fit_width,
            method=method,
        )
    except Exception as e:
        log(f"{e}", Level.ERROR)
        sys.exit(1)


def launch_gui():
    """Launch the tkinter GUI (tkinter is always available in GUI mode; this is just a fallback)"""
    if not _TK_AVAILABLE:
        print("Error: tkinter is not installed in this Python environment; cannot launch the GUI.", file=sys.stderr)
        print("Use command-line mode instead: pdftoy <input.pdf> [-o output.pdf] [-w] [-l] [-p page]", file=sys.stderr)
        sys.exit(1)
    root = tk.Tk()
    App(root)
    root.mainloop()


class App:
    """Main application window"""

    # Log colors (dark-terminal style, matching the CLI ANSI palette)
    LOG_COLORS = {
        Level.INFO: "#569cd6",    # blue
        Level.OK: "#6a9955",      # green
        Level.WARN: "#dcdcaa",    # yellow
        Level.ERROR: "#f44747",   # red
        Level.DEBUG: "#808080",   # gray
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("pdftoy — PDF Page Size Unification Tool")

        self.log_queue: queue.Queue = queue.Queue()
        self.processing = False
        self.last_output_path: str | None = None

        self._setup_style()
        self._build_ui()
        self._install_log_hook()
        self._resize_to_content()
        self._poll_queue()

    # ================================================================
    #   Styling
    # ================================================================
    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        bg = "#ffffff"
        card_bg = "#ffffff"
        accent = "#2563eb"
        accent_hover = "#1d4ed8"

        self.root.configure(bg=bg)

        font_family = "Microsoft YaHei UI"

        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground="#1f2937", font=(font_family, 10))
        style.configure("Title.TLabel", background=bg, foreground="#111827", font=(font_family, 18, "bold"))
        style.configure("Subtitle.TLabel", background=bg, foreground="#6b7280", font=(font_family, 9))
        style.configure("TLabelframe", background=card_bg, foreground="#374151", font=(font_family, 10, "bold"))
        style.configure("TLabelframe.Label", background=card_bg, foreground="#374151")

        style.configure("TButton", font=(font_family, 10), padding=(12, 6), borderwidth=0)
        style.configure("Accent.TButton", font=(font_family, 12, "bold"), padding=(20, 10),
                        background=accent, foreground="white", borderwidth=0)
        style.map("Accent.TButton",
                  background=[("active", accent_hover), ("pressed", accent_hover), ("disabled", "#9ca3af")],
                  foreground=[("active", "white"), ("disabled", "#e5e7eb")])

        style.configure("TRadiobutton", background=card_bg, foreground="#1f2937", font=(font_family, 10))
        style.map("TRadiobutton", background=[("active", card_bg)])

        style.configure("TEntry", font=(font_family, 10))
        style.configure("TSpinbox", font=(font_family, 10), fieldbackground="white",
                        background="#f5f5f5", arrowsize=11, lightcolor="#f5f5f5", darkcolor="#f5f5f5", )
        style.map("TSpinbox", fieldbackground=[("disabled", "#f5f5f5"), ],
                  background=[("active", "#e5e7eb"), ("pressed", "#d1d5db"), ], lightcolor=[("active", "#e5e7eb"), (
                "pressed", "#d1d5db"), ], darkcolor=[("active", "#e5e7eb"), ("pressed", "#d1d5db"), ],
                  arrowcolor=[("active", "#2563eb"), ("pressed", "#1d4ed8"), ("disabled", "#9ca3af"), ], )

        style.configure("Horizontal.TProgressbar", thickness=4, background=accent, troughcolor="#e5e7eb")

    # ================================================================
    #   Build UI
    # ================================================================
    def _build_ui(self):
        # ---------- Header ----------
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=20, pady=(18, 6))
        ttk.Label(header, text="PDF Page Size Unification Tool", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text=f"Unified page size · bookmarks & hyperlinks preserved · zero drift    v{__version__}",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        # ---------- File selection ----------
        file_frame = ttk.LabelFrame(self.root, text="File", padding=12)
        file_frame.pack(fill="x", padx=20, pady=(8, 6))

        # Input row
        row1 = ttk.Frame(file_frame)
        row1.pack(fill="x", pady=(0, 8))
        ttk.Label(row1, text="Input PDF", width=10).pack(side="left")
        self.input_var = tk.StringVar()
        self.input_entry = ttk.Entry(row1, textvariable=self.input_var)
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(row1, text="Browse...", command=self._browse_input).pack(side="left")

        # Output row
        row2 = ttk.Frame(file_frame)
        row2.pack(fill="x")
        ttk.Label(row2, text="Output PDF", width=10).pack(side="left")
        self.output_var = tk.StringVar()
        self.output_entry = ttk.Entry(row2, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(row2, text="Browse...", command=self._browse_output).pack(side="left")

        # Derive the output path automatically when the input path changes
        self.input_var.trace_add("write", self._on_input_change)

        # ---------- Options ----------
        opts = ttk.Frame(self.root)
        opts.pack(fill="x", padx=20, pady=(0, 6))

        # Render method
        method_frame = ttk.LabelFrame(opts, text="Render method", padding=12)
        method_frame.pack(fill="x", pady=(0, 6))
        self.method_var = tk.StringVar(value="matrix")
        ttk.Radiobutton(
            method_frame,
            text="Matrix (recommended, keeps hyperlinks)",
            variable=self.method_var,
            value="matrix",
        ).pack(anchor="w", pady=2)
        ttk.Radiobutton(
            method_frame,
            text="Reflow legacy re-render (hyperlinks lost)",
            variable=self.method_var,
            value="reflow",
        ).pack(anchor="w", pady=2)

        # Canvas mode
        canvas_frame = ttk.LabelFrame(opts, text="Canvas mode", padding=12)
        canvas_frame.pack(fill="x", pady=(0, 6))
        self.canvas_var = tk.StringVar(value="fixed")
        ttk.Radiobutton(
            canvas_frame,
            text="Fixed canvas (uniform scale, centered)",
            variable=self.canvas_var,
            value="fixed",
        ).pack(anchor="w", pady=2)
        ttk.Radiobutton(
            canvas_frame,
            text="Fit to width (height scales proportionally)",
            variable=self.canvas_var,
            value="fit",
        ).pack(anchor="w", pady=2)

        # Reference page
        ref_frame = ttk.LabelFrame(opts, text="Reference page", padding=12)
        ref_frame.pack(fill="x")
        self.ref_mode_var = tk.StringVar(value="auto")
        ttk.Radiobutton(
            ref_frame,
            text="Auto-detect",
            variable=self.ref_mode_var,
            value="auto",
            command=self._toggle_page_input,
        ).pack(anchor="w", pady=2)
        manual_row = ttk.Frame(ref_frame)
        manual_row.pack(fill="x", pady=2)
        ttk.Radiobutton(
            manual_row,
            text="Manual",
            variable=self.ref_mode_var,
            value="manual",
            command=self._toggle_page_input,
        ).pack(side="left")
        self.page_var = tk.StringVar(value="1")
        self.page_spin = ttk.Spinbox(
            manual_row, from_=1, to=999999, width=8, textvariable=self.page_var,
        )
        self.page_spin.pack(side="left", padx=(8, 4))
        ttk.Label(manual_row, text="page (1-based)").pack(side="left")
        self._toggle_page_input()  # disabled initially

        # ---------- Process button ----------
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", padx=20, pady=(6, 4))
        self.process_btn = ttk.Button(
            btn_frame, text="Start", style="Accent.TButton", command=self._start_processing
        )
        self.process_btn.pack(fill="x")

        # Progress bar (shown while processing)
        self.progress = ttk.Progressbar(self.root, mode="indeterminate")

        # ---------- Processing log ----------
        log_frame = ttk.LabelFrame(self.root, text="Processing log", padding=4)
        log_frame.pack(fill="both", expand=True, padx=20, pady=(6, 6))

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=10,
            wrap="word",
            state="disabled",
            font=("Cascadia Mono", 9),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="#d4d4d4",
            selectbackground="#264f78",
            relief="flat",
            padx=10,
            pady=8,
            borderwidth=0,
        )
        self.log_text.pack(fill="both", expand=True)

        # Log color tag
        for level, color in self.LOG_COLORS.items():
            self.log_text.tag_configure(level, foreground=color)

        # ---------- Bottom action bar ----------
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=20, pady=(0, 12))
        self.open_file_btn = ttk.Button(
            bottom, text="Open output file", command=self._open_file, state="disabled"
        )
        self.open_file_btn.pack(side="right", padx=(6, 0))
        self.open_folder_btn = ttk.Button(
            bottom, text="Open output folder", command=self._open_folder, state="disabled"
        )
        self.open_folder_btn.pack(side="right")

        # Status bar
        self.status_var = tk.StringVar(value="Ready — pick a PDF file to begin")
        ttk.Label(self.root, textvariable=self.status_var, style="Subtitle.TLabel").pack(
            anchor="w", padx=20, pady=(0, 10)
        )

    # ================================================================
    #   Responsive sizing
    # ================================================================
    def _resize_to_content(self):
        """Size the window from the real content so the bottom controls are never cut off"""
        self.root.update_idletasks()

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        req_w = self.root.winfo_reqwidth()
        req_h = self.root.winfo_reqheight()

        # Margins: 20px left/right, 80px reserved for the taskbar at the bottom
        max_w = max(560, screen_w - 40)
        max_h = max(640, screen_h - 80)

        w = min(max(req_w, 560), max_w)
        h = min(max(req_h, 640), max_h)

        x = max(0, (screen_w - w) // 2)
        y = max(0, (screen_h - h) // 2)

        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(560, 640)

    # ================================================================
    #   Log interception
    # ================================================================
    def _install_log_hook(self):
        """Take over the pdftoy log function and forward messages to the GUI queue"""

        def gui_log(msg, level=Level.INFO):
            prefix = LEVEL_STYLE.get(level, f"[{level}]")
            # Level names differ in length; pad them to 5 chars so message columns line up
            self.log_queue.put((level, f"{prefix:<5} {msg}"))

        global log
        log = gui_log

    # ================================================================
    #   Event handlers
    # ================================================================
    def _browse_input(self):
        path = filedialog.askopenfilename(
            title="Select input PDF file",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if path:
            self.input_var.set(path)
            self._auto_output(path)

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            title="Select output PDF path",
            defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if path:
            self.output_var.set(path)

    def _on_input_change(self, *_args):
        path = self.input_var.get().strip()
        if path:
            self._auto_output(path)

    def _auto_output(self, input_path: str):
        """Derive the output path from the input path (appends a _fixed suffix)"""
        if input_path.lower().endswith(".pdf"):
            output = input_path[:-4] + "_fixed.pdf"
        else:
            output = input_path + "_fixed.pdf"
        # Only auto-fill while the user has not touched the output path
        self.output_var.set(output)

    def _toggle_page_input(self):
        if self.ref_mode_var.get() == "manual":
            self.page_spin.config(state="normal")
        else:
            self.page_spin.config(state="disabled")

    # ================================================================
    #   Processing
    # ================================================================
    def _start_processing(self):
        if self.processing:
            return

        input_path = self.input_var.get().strip()
        output_path = self.output_var.get().strip()

        if not input_path:
            self._set_status("Select an input PDF file first", True)
            return
        if not os.path.isfile(input_path):
            self._set_status(f"File not found: {input_path}", True)
            return
        if not output_path:
            self._auto_output(input_path)
            output_path = self.output_var.get().strip()

        # Collect the arguments
        method = self.method_var.get()
        fit_width = self.canvas_var.get() == "fit"
        auto_ref = self.ref_mode_var.get() == "auto"
        ref_page_index = None
        if not auto_ref:
            try:
                ref_page_index = int(self.page_var.get()) - 1
                if ref_page_index < 0:
                    raise ValueError
            except ValueError:
                self._set_status("Reference page must be an integer >= 1", True)
                return

        # Clear the log
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

        # Switch the UI into the processing state
        self.processing = True
        self.process_btn.config(state="disabled", text="Processing...")
        self.open_file_btn.config(state="disabled")
        self.open_folder_btn.config(state="disabled")
        self.progress.pack(fill="x", padx=20, pady=(0, 4))
        self.progress.start(12)
        self._set_status("Processing...")

        # Start the worker thread
        t = threading.Thread(
            target=self._process_worker,
            args=(input_path, output_path, auto_ref, ref_page_index, fit_width, method),
            daemon=True,
        )
        t.start()

    def _process_worker(self, input_path, output_path, auto_ref, ref_page_index, fit_width, method):
        """Background worker thread"""
        try:
            result = fix_pdf_scale_pro_module(
                input_pdf_path=input_path,
                output_pdf_path=output_path,
                auto_ref=auto_ref,
                ref_page_index=ref_page_index,
                fit_width=fit_width,
                method=method,
            )
            self.log_queue.put(("__DONE__", result))
        except Exception as e:
            self.log_queue.put(("__ERROR__", str(e)))

    def _on_done(self, result: dict):
        """Processing finished callback (main thread)"""
        self.processing = False
        self.progress.stop()
        self.progress.pack_forget()
        self.process_btn.config(state="normal", text="Start")

        self.last_output_path = result["output_path"]
        if self.last_output_path and os.path.isfile(self.last_output_path):
            self.open_file_btn.config(state="normal")
            self.open_folder_btn.config(state="normal")

        size_kb = os.path.getsize(self.last_output_path) / 1024 if self.last_output_path else 0
        self._set_status(
            f"Done — {result['total_pages']} pages · "
            f"canvas {result['target_size_pt'][0]:.0f}x{result['target_size_pt'][1]:.0f}pt · "
            f"{result['toc_count']} bookmarks · "
            f"output {size_kb:.0f} KB"
        )

    def _on_error(self, error_msg: str):
        """Processing failed callback (main thread)"""
        self.processing = False
        self.progress.stop()
        self.progress.pack_forget()
        self.process_btn.config(state="normal", text="Start")
        self._append_log(Level.ERROR, f"{LEVEL_STYLE.get(Level.ERROR, '[ERROR]'):<5} {error_msg}")
        self._set_status("Processing failed, see the log", True)

    # ================================================================
    #   Queue polling
    # ================================================================
    def _poll_queue(self):
        """Main thread polls the log queue periodically"""
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, tuple) and len(item) == 2 and item[0] in ("__DONE__", "__ERROR__"):
                    if item[0] == "__DONE__":
                        self._on_done(item[1])
                    else:
                        self._on_error(item[1])
                else:
                    level, text = item
                    self._append_log(level, text)
        except queue.Empty:
            pass
        finally:
            self.root.after(80, self._poll_queue)

    def _append_log(self, level: str, text: str):
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n", level)
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ================================================================
    #   Helpers
    # ================================================================
    def _set_status(self, msg: str, is_error: bool = False):
        self.status_var.set(msg)

    def _open_file(self):
        if self.last_output_path and os.path.isfile(self.last_output_path):
            if sys.platform == "win32":
                os.startfile(self.last_output_path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self.last_output_path])
            else:
                subprocess.Popen(["xdg-open", self.last_output_path])

    def _open_folder(self):
        if self.last_output_path:
            folder = os.path.dirname(self.last_output_path) or "."
            if sys.platform == "win32":
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])



if __name__ == "__main__":
    main()
