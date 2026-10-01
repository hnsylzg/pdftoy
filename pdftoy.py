import argparse
import os
import sys
from collections import Counter
from contextlib import closing
try:
    import pymupdf as fitz  # PyMuPDF (pymupdf is the canonical name since 1.24+)
except ImportError:
    import fitz  # PyMuPDF (legacy fallback for older versions)

# =================== Global metadata & config flags ====================
__version__ = "1.0.1"

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
    for link in new_page.get_links():
        link_rect = fitz.Rect(link["from"])
        link["from"] = link_rect * m
        # (and the QuadPoints of text links) onto the new canvas. The link destination ("to")
        # scales with the same matrix, so an untransformed "to" lands off-target.
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
        page_matrices = None
        named_links_count = 0
        if method == "matrix":
            dst_doc.insert_pdf(
                src_doc, annots=False, links=True, widgets=False,
            )

            # reflow: per-page new_page + show_pdf_page, no pre-copy needed.
            page_matrices = [
                _compute_page_matrix(page, target_w, target_h, fit_width, i)
                for i, page in enumerate(src_doc)
            ]

            # Precompute the transform for every page (shared by content-stream wrapping and "to" mapping)
            # Rebuild NAMED links as GOTO links
            # insert_pdf(links=True) does not copy NAMED links -- they reference the /Names named targets
            # Update the NAMED links read from the source doc, re-inserting them as GOTO links.
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
    """CLI entry-point configuration"""
    parser = argparse.ArgumentParser(
        prog="pdftoy.exe",
        description=(
            "pdftoy -- PDF Page Size Unification Tool\n"
            "[Default]: matrix method (insert_pdf + cm wrap; zero drift, keeps hyperlinks)\n"
            "[Legacy]: pass -l / --legacy to use the show_pdf_page re-render method\n"
            "[Canvas]: fixed reference-page canvas by default; -w fits the width (height scales)"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument("input", help="Input PDF file path")
    parser.add_argument(
        "-o",
        "--output",
        help="Output PDF file path (default: a _fixed suffix is appended)",
        default=None,
    )
    parser.add_argument(
        "-w",
        "--fit-width",
        action="store_true",
        help="[Core switch] Enable fit-to-width mode (height scales proportionally).",
    )
    parser.add_argument(
        "-p",
        "--page",
        type=int,
        metavar="PAGE_NUM",
        help="Reference page number (1-based; auto-detected by default)",
    )
    parser.add_argument(
        "-l",
        "--legacy",
        action="store_true",
        help="Legacy show_pdf_page re-render (default: matrix, recommended).",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show the current version",
    )

    args = parser.parse_args()

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
            log("Error: the specified page number must be >= 1", Level.ERROR)
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


if __name__ == "__main__":
    main()
