#!/usr/bin/env python3
"""
pdftoy GUI — PDF Page Size Unification Tool · graphical interface

Thin Tkinter front-end over the pdftoy core module.
No extra dependencies (tkinter ships with the Python standard library).

Usage:
    python pdftoy_gui.py
"""

import os
import sys
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext

import sv_ttk

import pdftoy
from pdftoy import fix_pdf_scale_pro_module, Level, LEVEL_STYLE, __version__


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
        try:
            sv_ttk.set_theme("light")
        except Exception as e:
            pass

        style = ttk.Style()
        font_family = "Microsoft YaHei UI"

        style.configure("Title.TLabel", font=(font_family, 18, "bold"))
        style.configure("Subtitle.TLabel", font=(font_family, 9))
        style.configure("TLabelframe", font=(font_family, 10, "bold"))
        style.configure("TButton", font=(font_family, 10), padding=(12, 6))
        style.configure("Accent.TButton", font=(font_family, 12, "bold"), padding=(20, 10))
        style.configure("TRadiobutton", font=(font_family, 10))
        style.configure("TEntry", font=(font_family, 10))
        style.configure("TSpinbox", font=(font_family, 10))

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
        ttk.Label(row1, text="Input PDF", width=11).pack(side="left")
        self.input_var = tk.StringVar()
        self.input_entry = ttk.Entry(row1, textvariable=self.input_var)
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(row1, text="Browse...", command=self._browse_input).pack(side="left")

        # Output row
        row2 = ttk.Frame(file_frame)
        row2.pack(fill="x")
        ttk.Label(row2, text="Output PDF", width=11).pack(side="left")
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

        # Progress bar (shown while processing)
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

        pdftoy.log = gui_log

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

        size_kb = (
            os.path.getsize(self.last_output_path) / 1024
            if self.last_output_path and os.path.isfile(self.last_output_path)
            else 0
        )
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


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()