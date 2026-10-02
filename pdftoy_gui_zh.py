#!/usr/bin/env python3
"""
pdftoy GUI — PDF 页面尺寸统一工具 · 图形界面

基于 pdftoy.py 核心模块，提供直观的图形操作界面。
零额外依赖（tkinter 随 Python 标准库附带）。

用法:
    python pdftoy_gui.py
"""

import os
import sys
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext

import pdftoy_zh
from pdftoy_zh import fix_pdf_scale_pro_module, Level, LEVEL_STYLE, __version__


class App:
    """主应用窗口"""

    # 日志颜色（深色终端风格，与命令行 ANSI 配色语义一致）
    LOG_COLORS = {
        Level.INFO: "#569cd6",    # 蓝
        Level.OK: "#6a9955",      # 绿
        Level.WARN: "#dcdcaa",    # 黄
        Level.ERROR: "#f44747",   # 红
        Level.DEBUG: "#808080",   # 灰
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("pdftoy — PDF 页面尺寸统一工具")

        self.log_queue: queue.Queue = queue.Queue()
        self.processing = False
        self.last_output_path: str | None = None

        self._setup_style()
        self._build_ui()
        self._install_log_hook()
        self._resize_to_content()
        self._poll_queue()

    # ================================================================
    #  样式
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
    #  构建 UI
    # ================================================================
    def _build_ui(self):
        # ---------- 标题 ----------
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=20, pady=(18, 6))
        ttk.Label(header, text="PDF 页面尺寸统一工具", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text=f"统一页面尺寸 · 保留书签与超链接 · 零漂移输出    v{__version__}",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        # ---------- 文件选择 ----------
        file_frame = ttk.LabelFrame(self.root, text="文件", padding=12)
        file_frame.pack(fill="x", padx=20, pady=(8, 6))

        # 输入行
        row1 = ttk.Frame(file_frame)
        row1.pack(fill="x", pady=(0, 8))
        ttk.Label(row1, text="输入 PDF", width=8).pack(side="left")
        self.input_var = tk.StringVar()
        self.input_entry = ttk.Entry(row1, textvariable=self.input_var)
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(row1, text="浏览…", command=self._browse_input).pack(side="left")

        # 输出行
        row2 = ttk.Frame(file_frame)
        row2.pack(fill="x")
        ttk.Label(row2, text="输出 PDF", width=8).pack(side="left")
        self.output_var = tk.StringVar()
        self.output_entry = ttk.Entry(row2, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(row2, text="浏览…", command=self._browse_output).pack(side="left")

        # 输入路径变化时自动推导输出路径
        self.input_var.trace_add("write", self._on_input_change)

        # ---------- 选项 ----------
        opts = ttk.Frame(self.root)
        opts.pack(fill="x", padx=20, pady=(0, 6))

        # 渲染方法
        method_frame = ttk.LabelFrame(opts, text="渲染方法", padding=12)
        method_frame.pack(fill="x", pady=(0, 6))
        self.method_var = tk.StringVar(value="matrix")
        ttk.Radiobutton(
            method_frame,
            text="Matrix 矩阵（推荐，保留超链接）",
            variable=self.method_var,
            value="matrix",
        ).pack(anchor="w", pady=2)
        ttk.Radiobutton(
            method_frame,
            text="Reflow 旧版重渲染（不保留超链接）",
            variable=self.method_var,
            value="reflow",
        ).pack(anchor="w", pady=2)

        # 画布模式
        canvas_frame = ttk.LabelFrame(opts, text="画布模式", padding=12)
        canvas_frame.pack(fill="x", pady=(0, 6))
        self.canvas_var = tk.StringVar(value="fixed")
        ttk.Radiobutton(
            canvas_frame,
            text="固定画布（等比缩放后居中）",
            variable=self.canvas_var,
            value="fixed",
        ).pack(anchor="w", pady=2)
        ttk.Radiobutton(
            canvas_frame,
            text="按宽度适配（高度等比例自适应）",
            variable=self.canvas_var,
            value="fit",
        ).pack(anchor="w", pady=2)

        # 参考页
        ref_frame = ttk.LabelFrame(opts, text="参考页", padding=12)
        ref_frame.pack(fill="x")
        self.ref_mode_var = tk.StringVar(value="auto")
        ttk.Radiobutton(
            ref_frame,
            text="自动探测",
            variable=self.ref_mode_var,
            value="auto",
            command=self._toggle_page_input,
        ).pack(anchor="w", pady=2)
        manual_row = ttk.Frame(ref_frame)
        manual_row.pack(fill="x", pady=2)
        ttk.Radiobutton(
            manual_row,
            text="手动指定",
            variable=self.ref_mode_var,
            value="manual",
            command=self._toggle_page_input,
        ).pack(side="left")
        self.page_var = tk.StringVar(value="1")
        self.page_spin = ttk.Spinbox(
            manual_row, from_=1, to=999999, width=8, textvariable=self.page_var,
        )
        self.page_spin.pack(side="left", padx=(8, 4))
        ttk.Label(manual_row, text="页（从 1 开始）").pack(side="left")
        self._toggle_page_input()  # 初始禁用

        # ---------- 处理按钮 ----------
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", padx=20, pady=(6, 4))
        self.process_btn = ttk.Button(
            btn_frame, text="开始处理", style="Accent.TButton", command=self._start_processing
        )
        self.process_btn.pack(fill="x")

        # 进度条（处理时显示）
        self.progress = ttk.Progressbar(self.root, mode="indeterminate")

        # ---------- 日志 ----------
        log_frame = ttk.LabelFrame(self.root, text="处理日志", padding=4)
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

        # 日志颜色标签
        for level, color in self.LOG_COLORS.items():
            self.log_text.tag_configure(level, foreground=color)

        # ---------- 底部操作栏 ----------
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=20, pady=(0, 12))
        self.open_file_btn = ttk.Button(
            bottom, text="打开输出文件", command=self._open_file, state="disabled"
        )
        self.open_file_btn.pack(side="right", padx=(6, 0))
        self.open_folder_btn = ttk.Button(
            bottom, text="打开输出目录", command=self._open_folder, state="disabled"
        )
        self.open_folder_btn.pack(side="right")

        # 状态栏
        self.status_var = tk.StringVar(value="就绪 — 选择 PDF 文件开始")
        ttk.Label(self.root, textvariable=self.status_var, style="Subtitle.TLabel").pack(
            anchor="w", padx=20, pady=(0, 10)
        )

    # ================================================================
    #  自适应尺寸
    # ================================================================
    def _resize_to_content(self):
        """按实际内容计算窗口尺寸，避免固定高度裁掉底部控件"""
        self.root.update_idletasks()

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        req_w = self.root.winfo_reqwidth()
        req_h = self.root.winfo_reqheight()

        # 留白：左右各 20px，底部给任务栏留 80px
        max_w = max(560, screen_w - 40)
        max_h = max(640, screen_h - 80)

        w = min(max(req_w, 560), max_w)
        h = min(max(req_h, 640), max_h)

        x = max(0, (screen_w - w) // 2)
        y = max(0, (screen_h - h) // 2)

        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(560, 640)

    # ================================================================
    #  日志接管
    # ================================================================
    def _install_log_hook(self):
        """接管 pdftoy 模块的 log 函数，转发到 GUI 队列"""

        def gui_log(msg, level=Level.INFO):
            prefix = LEVEL_STYLE.get(level, f"[{level}]")
            # 纯文字级别名长度不一，左补齐到 5 字符再接一个空格，保证消息列对齐
            self.log_queue.put((level, f"{prefix:<5} {msg}"))

        pdftoy_zh.log = gui_log

    # ================================================================
    #  事件处理
    # ================================================================
    def _browse_input(self):
        path = filedialog.askopenfilename(
            title="选择输入 PDF 文件",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if path:
            self.input_var.set(path)
            self._auto_output(path)

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            title="选择输出 PDF 路径",
            defaultextension=".pdf",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if path:
            self.output_var.set(path)

    def _on_input_change(self, *_args):
        path = self.input_var.get().strip()
        if path:
            self._auto_output(path)

    def _auto_output(self, input_path: str):
        """根据输入路径自动推导输出路径（加 _fixed 后缀）"""
        if input_path.lower().endswith(".pdf"):
            output = input_path[:-4] + "_fixed.pdf"
        else:
            output = input_path + "_fixed.pdf"
        # 只在用户未手动修改输出路径时自动填充
        self.output_var.set(output)

    def _toggle_page_input(self):
        if self.ref_mode_var.get() == "manual":
            self.page_spin.config(state="normal")
        else:
            self.page_spin.config(state="disabled")

    # ================================================================
    #  处理
    # ================================================================
    def _start_processing(self):
        if self.processing:
            return

        input_path = self.input_var.get().strip()
        output_path = self.output_var.get().strip()

        if not input_path:
            self._set_status("请先选择输入 PDF 文件", True)
            return
        if not os.path.isfile(input_path):
            self._set_status(f"文件不存在: {input_path}", True)
            return
        if not output_path:
            self._auto_output(input_path)
            output_path = self.output_var.get().strip()

        # 收集参数
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
                self._set_status("参考页码必须是 >= 1 的整数", True)
                return

        # 清空日志
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

        # 切换 UI 到处理中状态
        self.processing = True
        self.process_btn.config(state="disabled", text="处理中…")
        self.open_file_btn.config(state="disabled")
        self.open_folder_btn.config(state="disabled")
        self.progress.pack(fill="x", padx=20, pady=(0, 4))
        self.progress.start(12)
        self._set_status("正在处理…")

        # 启动后台线程
        t = threading.Thread(
            target=self._process_worker,
            args=(input_path, output_path, auto_ref, ref_page_index, fit_width, method),
            daemon=True,
        )
        t.start()

    def _process_worker(self, input_path, output_path, auto_ref, ref_page_index, fit_width, method):
        """后台处理线程"""
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
        """处理完成回调（主线程）"""
        self.processing = False
        self.progress.stop()
        self.progress.pack_forget()
        self.process_btn.config(state="normal", text="开始处理")

        self.last_output_path = result["output_path"]
        if self.last_output_path and os.path.isfile(self.last_output_path):
            self.open_file_btn.config(state="normal")
            self.open_folder_btn.config(state="normal")

        size_kb = os.path.getsize(self.last_output_path) / 1024 if self.last_output_path else 0
        self._set_status(
            f"完成 — {result['total_pages']} 页 · "
            f"画布 {result['target_size_pt'][0]:.0f}×{result['target_size_pt'][1]:.0f}pt · "
            f"书签 {result['toc_count']} 条 · "
            f"输出 {size_kb:.0f} KB"
        )

    def _on_error(self, error_msg: str):
        """处理失败回调（主线程）"""
        self.processing = False
        self.progress.stop()
        self.progress.pack_forget()
        self.process_btn.config(state="normal", text="开始处理")
        self._append_log(Level.ERROR, f"{LEVEL_STYLE.get(Level.ERROR, '[ERROR]'):<5} {error_msg}")
        self._set_status("处理失败，详见日志", True)

    # ================================================================
    #  队列轮询
    # ================================================================
    def _poll_queue(self):
        """主线程定期轮询日志队列"""
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
    #  辅助
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
    # 如果命令行传入了文件名或参数（如 pdftoy.exe input.pdf -w）
    if len(sys.argv) > 1:
        pdftoy_zh.main()  # 转向 CLI 执行逻辑
    else:
        root = tk.Tk()  # 无参数双击运行，打开 GUI
        App(root)
        root.mainloop()


if __name__ == "__main__":
    main()
