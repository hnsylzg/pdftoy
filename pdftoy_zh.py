"""pdftoy — PDF 页面尺寸统一工具（命令行 / 图形界面一体）

用法:
    pdftoy                 # 启动图形界面
    pdftoy input.pdf       # 命令行模式处理
    pdftoy -h              # 查看全部命令行参数
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from contextlib import closing

# ── windowed 打包下 CLI 模式的日志接回：挂回原终端，无终端时兜底 ──
_attached_shell_name = ""  # AttachConsole 成功时记录外壳名，退出钩子据此决定是否注入回车


def _find_shell_ancestor_pid():
    """沿进程树向上找终端外壳祖先（cmd / powershell / pwsh / wt）。

    返回 (PID, 外壳名)；找不到返回 (0, "")。

    说明：PyInstaller --onefile 的 bootloader 会再拉起真正的 python 子进程，
    ATTACH_PARENT_PROCESS 挂到的是没有控制台的 bootloader，因此必须显式找到
    外壳祖先的 PID 再 AttachConsole(pid)。
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
        # 快照/遍历失败：交由调用方走 AllocConsole 兜底
        return 0, ""


class _ConsoleWriter:
    """WriteConsoleW 直写 Unicode 的极简文本流，不依赖控制台代码页，杜绝乱码。"""

    def __init__(self, kernel32, handle):
        import ctypes

        self._ct = ctypes
        self._write = kernel32.WriteConsoleW
        self._write.argtypes = (
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
        )
        self._handle = handle
        self.vt_enabled = False  # VT 序列（擦行等）是否可用，由 _make_console_stream 设置

    def write(self, text):
        if text:
            # WriteConsoleW 不做 LF→CRLF 翻译：裸 \n 只换行不回列，会让 shell
            # 的提示符接在行中，需要按回车才归位。这里统一补成 \r\n。
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
    """打开 CONOUT$ 并返回直写 Unicode 的输出流；失败返回 None。"""
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
    # 处理输出 | 行尾换行 | 虚拟终端(VT 颜色/擦行)；老系统不识别 VT 位会失败，退回无 VT 模式
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
    """CLI 模式把日志接回终端（windowed 打包专用；源码在终端里跑时 sys.stdout 可用，直接跳过）。

    1) stdout 已可用（重定向 > file，或源码在终端里跑）→ 不动；
    2) 祖先里有终端外壳 → AttachConsole(其 PID)，日志打进原终端，不弹新窗；
    3) 完全没有终端（拖拽 PDF 到 exe）→ AllocConsole 开一个日志窗口，进程退出即关。

    输出走 WriteConsoleW 直写 Unicode，与控制台代码页无关，中文不乱码。
    注意：cmd / PowerShell / WT 对 GUI 子系统 exe 都不等待、也不重绘提示符
    （shell 只对共享自己控制台的 console 子系统程序等待；windowed exe 不共享，
    见 SO 1305257）。对策：退出时向原终端的输入缓冲注入一对回车键事件，
    shell 收到立即重画提示符（_inject_enter_for_shell）。
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
            # shell 启动本进程后已画完新提示符（光标停在其后）。
            # 支持 VT：回行首并整行擦除该提示符，日志从干净行首开始，
            # 退出时注入的回车会让 shell 重新画提示符。
            # 不支持 VT：只回行首（罕见，老系统）。
            stream.write("\r\x1b[2K" if stream.vt_enabled else "\r")
    except Exception:
        return
    # 接回后重估颜色开关。本函数会在 _color_supported 定义之前（模块早期）被调用，
    # 那时先跳过——模块后面的 COLOR_ENABLED = _color_supported() 会用接回后的流重新求值。
    if "_color_supported" in globals():
        global COLOR_ENABLED
        COLOR_ENABLED = _color_supported()
    # shell（cmd/PS/WT）都不等待 GUI 子系统进程退出：注册退出钩子，替用户“按一下回车”。
    if _attached_shell_name:
        import atexit

        atexit.register(_inject_enter_for_shell)


def _inject_enter_for_shell():
    """向控制台输入缓冲注入一对回车键事件，让 shell 立即重画提示符。

    背景：windowed exe 经 AttachConsole 挂回终端后，shell（cmd/PowerShell/WT
    表现一致）早在启动瞬间就画完提示符且不等待我们退出（SO 1305257）。
    注入 VK_RETURN 键事件（WriteConsoleInputW 写 CONIN$）等效于用户敲一下
    回车，shell 即刻归位。写 down+up 一对事件，更接近真实按键。
    仅在 AttachConsole 成功（挂回了真实终端）时才注入。
    """
    if not _attached_shell_name:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # INPUT_RECORD: EventType=KEY_EVENT(1) + KEY_EVENT_RECORD(16 字节)
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

        # 打开 CONIN$（要读写权限才能 WriteConsoleInputW）
        kernel32.CreateFileW.restype = ctypes.c_void_p
        h = kernel32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
        if not h or h == 0xFFFFFFFFFFFFFFFF:
            return
        try:
            records = (_INPUT_RECORD * 2)()
            # 按下
            down = records[0]
            down.EventType = 1  # KEY_EVENT
            ev = down.Event.KeyEvent
            ev.bKeyDown = 1
            ev.wRepeatCount = 1
            ev.wVirtualKeyCode = 0x0D  # VK_RETURN
            ev.wVirtualScanCode = 0x1C
            ev.uChar = "\r"
            # 松开
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


# windowed 打包：带参数运行即 CLI，赶在 pymupdf / tkinter 重导入之前挂回终端；
# 双击 GUI（无参数）不受影响。
if os.name == "nt" and len(sys.argv) > 1:
    _ensure_cli_console()

try:
    import pymupdf as fitz  # PyMuPDF (pymupdf is the canonical name since 1.24+)
except ImportError:
    import fitz  # PyMuPDF (legacy fallback for older versions)

import queue
import threading
import subprocess

# tkinter 为 Python 标准库，仅 GUI 模式需要；CLI 模式下若缺失则优雅降级
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, scrolledtext
    _TK_AVAILABLE = True
except ImportError:
    tk = ttk = filedialog = scrolledtext = None
    _TK_AVAILABLE = False

# ================= 全局元数据与配置开关 =================
__version__ = "1.2.1"

DEBUG_TOC = False  # True: 打印提取到的原始书签，方便调试奇葩 PDF
STRICT_TOC = False  # True: 开启严格模式，提高书签判定门槛

# 书签诊断与性能优化常量
EARLY_STOP_THRESHOLD = 0.85  # 极高品质标准：已扫描样本达到此比例时直接触发提前终止

# 高度安全边界保护（单位：pt，仅在 fit_width 为 True 时生效）
MIN_PAGE_HEIGHT = 100.0  # 约 3.5 cm
MAX_PAGE_HEIGHT = 5000.0  # 约 1.76 米（防御天量内存爆破）
# =======================================================

# ================= 日志系统 =================
class Level:
    INFO = "INFO"
    OK = "OK"
    WARN = "WARN"
    ERROR = "ERROR"
    DEBUG = "DEBUG"


# 日志前缀：纯文字级别名（ASCII 定宽，UTF-8 / GBK 等任意终端都无需转码）
LEVEL_STYLE = {
    Level.INFO: "INFO",
    Level.OK: "OK",
    Level.WARN: "WARN",
    Level.ERROR: "ERROR",
    Level.DEBUG: "DEBUG",
}

# ANSI 转义码（仅控制序列，非文本内容，UTF-8 / GBK 等任意编码都安全）
_ANSI_RESET = "\033[0m"
_LEVEL_COLOR = {
    Level.INFO: "\033[36m",   # 青
    Level.OK: "\033[32m",     # 绿
    Level.WARN: "\033[33m",   # 黄
    Level.ERROR: "\033[31m",  # 红
    Level.DEBUG: "\033[90m",  # 灰
}


def _enable_ansi_on_windows():
    """Windows 旧版控制台默认关闭 ANSI，需开启虚拟终端处理才能显示颜色"""
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
    """是否应输出带色日志：仅交互式终端，且尊重 NO_COLOR / FORCE_COLOR 环境变量"""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False  # 管道 / 文件重定向时关闭，避免污染日志文件
    if os.name == "nt":
        _enable_ansi_on_windows()
    return True


COLOR_ENABLED = _color_supported()


def log(msg: str, level: str = Level.INFO):
    """统一日志输出：前缀为纯文字级别名，ASCII 文本安全；交互终端自动按级别上色"""
    prefix = LEVEL_STYLE.get(level, f"[{level}]")
    if COLOR_ENABLED and level in _LEVEL_COLOR:
        print(f"{_LEVEL_COLOR[level]}{prefix}{_ANSI_RESET} {msg}")
    else:
        print(f"{prefix} {msg}")


# ============================================


def calculate_toc_health(toc):
    """高效计算书签健康度得分 (0 - 100)"""
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
    """自动选择最优参考页"""
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
            f"检测到频次最高尺寸 {most_common_size} "
            f"低于安全阈值({min_size_pt}pt)，触发突变保护，退回首页",
            Level.WARN,
        )
        return 0

    for i in range(start, scan_end):
        box = doc[i].rect
        if (round(box.width, 1), round(box.height, 1)) == most_common_size:
            return i

    return min(4, total - 1)


# ============================================================
# 两种页面标准化实现（单页处理）
#   method="matrix" : insert_pdf 整本复制 + cm 仿射矩阵包裹内容流
#   method="reflow" : 逐页 new_page + show_pdf_page(clip=...) 重渲染
# 两者均保证图片型 / 文字型 PDF 零飘移（matrix 额外保留超链接）
# ============================================================

def _compute_page_matrix(page, target_w, target_h, fit_width, page_index=None):
    """计算单页的变换矩阵与目标高度（纯计算，不修改文档）

    返回 (fitz.Matrix, new_h)。page_index 用于日志中标识页码。
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
            idx_str = f"第 {page_index + 1} 页" if page_index is not None else "某页"
            log(
                f"{idx_str}等比高度 ({calculated_h:.1f}pt) 触发安全限制，"
                f"修正为: {new_h:.1f}pt",
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

    # 源页面若存在 cropbox 原点偏移，必须计入平移量，否则内容会整体飘移
    cb = page.cropbox
    tx = dx - cb.x0 * s
    ty = dy - cb.y0 * s
    m = fitz.Matrix(s, 0, 0, s, tx, ty)
    return m, new_h


def _rebuild_named_links(src_doc, dst_doc):
    """将 NAMED 链接转为 GOTO 链接重建到 dst_doc

    insert_pdf(links=True) 不复制 NAMED 类型链接注释——这类链接引用 PDF 的
    /Names 命名目标字典，而 insert_pdf 不会复制该字典，导致 NAMED 链接被丢弃。
    此函数从源文档逐页读取链接（get_links 已将 NAMED 解析为 page+to），
    以 GOTO 类型重新插入到目标文档，保留完整的目录超链接。
    """
    rebuilt = 0
    for i in range(len(src_doc)):
        src_links = src_doc[i].get_links()
        dst_page = dst_doc[i]
        for link in src_links:
            if link.get("kind") != fitz.LINK_NAMED:
                continue
            # NAMED 链接的目标页和坐标已被 get_links() 解析
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
        log(f"已重建 {rebuilt} 条 NAMED 超链接为 GOTO 类型", Level.OK)
    return rebuilt


def _apply_matrix_page(dst_doc, page, i, target_w, target_h, fit_width, page_matrices=None):
    """Matrix 方法：对已整本 insert_pdf 复制的第 i 页做 cm 矩阵包裹"""
    src_box = page.rect
    orig_w = src_box.width
    orig_h = src_box.height

    if page_matrices:
        m, new_h = page_matrices[i]
    else:
        m, new_h = _compute_page_matrix(page, target_w, target_h, fit_width, i)

    new_page = dst_doc[i]

    # 用 cm (concat matrix) 算子包裹原始内容流，并裁剪到源 mediabox，
    # 精确复刻 show_pdf_page(clip=page.rect) 的渲染结果（防止溢出/飘移）
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

    # 必须为每一页分配[独立]的内容流 xref，只在本页私有副本上包裹一次。
    # 源 PDF 常把「薄壳」内容流（如 q /fzFrm0 Do Q）在数百页间共享同一 xref。
    # 若直接 update_stream 改写该共享 xref，后续页面会再次包裹，导致 cm 矩阵
    # 被反复嵌套复合（实测可达数百层），内容严重飘移。
    new_xref = dst_doc.get_new_xref()
    dst_doc.update_object(new_xref, "<<>>")
    dst_doc.update_stream(new_xref, wrapped, compress=True)
    new_page.set_contents(new_xref)

    # 统一画布尺寸（同时对齐 CropBox，避免源 cropbox 偏移导致显示区域被裁剪/错位）
    unified_box = fitz.Rect(0, 0, target_w, new_h)
    new_page.set_mediabox(unified_box)
    # 关键：cropbox 必须用「写入后的 mediabox」而非原始 unified_box。
    # 因 set_mediabox 按字符串格式四舍五入存储，self.mediabox 读回值与
    # full-precision 的 unified_box 在新高度(new_h 为 orig_h*s 计算值)场景下
    # 可能存在 ~1e-5 差异，若用原始 unified_box 传 set_cropbox，_set_pagebox
    # 内 rect.y0 = mb.y1 - ub.y1 会算出微小负值 -> 误报 "CropBox not in MediaBox"
    # （fit-width 模式特有，固定画布 new_h=target_h 能干净往返故未暴露）。
    new_page.set_cropbox(new_page.mediabox)

    # 同步变换超链接坐标：链接矩形是独立注释对象，cm 包裹内容流不会带动它，
    # 必须用同一矩阵 m 把每条链接的矩形（及文本链接的 QuadPoints）搬到新画布位置。
    for link in new_page.get_links():
        link_rect = fitz.Rect(link["from"])
        link["from"] = link_rect * m
        # 变换跳转目标点（"to"）：目标页的内容也被同一矩阵缩放了，
        # 若不变换 to 坐标，点击链接跳转后定位会偏移。
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
    """Reflow 方法（旧版 show_pdf_page）：逐页 new_page + show_pdf_page 重渲染"""
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
                f"第 {i + 1} 页等比高度 ({calculated_h:.1f}pt) 触发安全限制，"
                f"修正为: {new_h:.1f}pt",
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
    """全功能、可编程、工业级 PDF 页面标准化处理主函数

    method:
        "matrix" (默认) -> insert_pdf 整本复制 + cm 矩阵包裹，保留文字/图片/矢量/超链接
        "reflow"        -> 逐页 new_page + show_pdf_page 重渲染（旧版方法）
    """
    if method not in ("matrix", "reflow"):
        raise ValueError(f"未知的渲染方法: {method!r}（应为 'matrix' 或 'reflow'）")

    with (
        closing(fitz.open(input_pdf_path)) as src_doc,
        closing(fitz.open()) as dst_doc,
    ):
        total_pages = len(src_doc)
        if total_pages == 0:
            raise ValueError("输入的 PDF 文件没有页面！")

        if auto_ref:
            chosen_ref_index = find_best_ref_page(src_doc)
            log(
                f"[页面分析] 自动探测最佳参考页: 第 {chosen_ref_index + 1} 页",
                Level.INFO,
            )
        else:
            if ref_page_index is None:
                ref_page_index = 0
            chosen_ref_index = min(max(0, ref_page_index), total_pages - 1)
            log(
                f"[页面分析] 使用手动指定参考页: 第 {chosen_ref_index + 1} 页",
                Level.INFO,
            )

        ref_page = src_doc[chosen_ref_index]
        ref_box = ref_page.rect  # 使用 rect 替代 cropbox，防止带偏移导致错误裁剪
        target_w = ref_box.width
        target_h = ref_box.height

        if fit_width:
            log(
                f"[画布目标] 模式: 按宽度适配 | 目标宽度: {target_w:.2f} pt（高度动态自适应）",
                Level.INFO,
            )
        else:
            log(
                f"[画布目标] 模式: 固定画布 | 基准尺寸: {target_w:.2f} x {target_h:.2f} pt",
                Level.INFO,
            )

        log(
            f"[渲染方法] {'matrix 矩阵（推荐，保留超链接）' if method == 'matrix' else 'reflow 旧版 show_pdf_page'}",
            Level.INFO,
        )

        # matrix 方法需整本一次性 insert_pdf（共享资源只存一份，避免逐页复制导致体积膨胀/卡死）
        # 再逐页用 cm 仿射矩阵包裹内容流实现统一缩放+居中
        # reflow 方法逐页 new_page + show_pdf_page，无需预复制
        page_matrices = None
        named_links_count = 0
        if method == "matrix":
            dst_doc.insert_pdf(
                src_doc, annots=False, links=True, widgets=False,
            )

            # 预计算所有页面的变换矩阵（供内容流包裹与链接 to 点坐标变换共用）
            page_matrices = [
                _compute_page_matrix(page, target_w, target_h, fit_width, i)
                for i, page in enumerate(src_doc)
            ]

            # 重建 NAMED 链接为 GOTO 类型
            # insert_pdf(links=True) 不复制 NAMED 链接——这类链接引用 /Names 命名目标
            # 字典，而 insert_pdf 不会复制该字典，导致目录超链接全部丢失。
            # 此处从源文档读取已解析的 NAMED 链接，以 GOTO 类型重新插入目标文档。
            named_links_count = _rebuild_named_links(src_doc, dst_doc)

        # 逐页标准化处理
        for i, page in enumerate(src_doc):
            if method == "matrix":
                _apply_matrix_page(dst_doc, page, i, target_w, target_h, fit_width, page_matrices)
            else:
                _apply_reflow_page(dst_doc, src_doc, page, i, target_w, target_h, fit_width)

        # 书签识别与过滤
        log("开始书签树提取与健康度诊断", Level.INFO)
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
                f"[书签诊断] 原生书签校验通过！健康度评分: {final_health_score}/100",
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
                    "[书签诊断] 完整模式 (simple=False) 恢复成功！健康度评分: "
                    f"{final_health_score}/100",
                    Level.OK,
                )
            else:
                log(
                    "[书签诊断] 未检测到有效书签（目录为空或全部为自动生成的占位书签），本次跳过书签修复",
                    Level.WARN,
                )

        injected_count = 0
        if toc_to_use:
            valid_toc = [item for item in toc_to_use if 1 <= item[2] <= total_pages]
            filtered_count = len(toc_to_use) - len(valid_toc)

            if filtered_count > 0:
                log(
                    f"[书签诊断] 已自动剔除 {filtered_count} 条指向不存在页码的非法书签",
                    Level.WARN,
                )

            dst_doc.set_toc(valid_toc)
            injected_count = len(valid_toc)

        # 保存策略：
        # matrix -> garbage=1（整本 insert_pdf 已共享资源，回收未用对象即可，避免大文件卡死）
        # reflow -> garbage=3（逐页重渲染，需去重 + 紧凑化体积）
        garbage_level = 1 if method == "matrix" else 3
        dst_doc.save(
            output_pdf_path,
            use_objstms=True,
            deflate=True,
            garbage=garbage_level,
        )
        log(f"处理完成，文件成功保存至: {output_pdf_path}", Level.OK)

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
    """统一入口：未提供输入文件则启动图形界面；提供输入 PDF 路径则进入命令行处理。"""
    # windowed 打包下，带参数运行即 CLI：先把日志接回终端（双击 GUI 无参数，不受影响）
    if len(sys.argv) > 1:
        _ensure_cli_console()

    parser = argparse.ArgumentParser(
        prog="pdftoy",
        description=(
            "PDF 页面尺寸统一工具\n"
            "[默认模式]: matrix 矩阵方法（insert_pdf + cm 包裹，零飘移且保留超链接）\n"
            "[旧版模式]: 加 -l / --legacy 使用 show_pdf_page 重渲染方法\n"
            "[画布模式]: 默认固定参考页画布尺寸；加 -w 按宽度适配（高度等比例自适应）\n"
            "\n"
            "无参数运行启动图形界面；传入输入 PDF 路径则按命令行模式处理。"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "input",
        nargs="?",
        default=None,
        help="输入 PDF 文件的路径（省略则启动图形界面）",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="输出 PDF 文件路径（默认：自动添加 _fixed 后缀）",
        default=None,
    )
    parser.add_argument(
        "-w",
        "--fit-width",
        action="store_true",
        help="[核心开关] 启用按宽度适配模式（高度按比例自适应）。",
    )
    parser.add_argument(
        "-p",
        "--page",
        type=int,
        metavar="PAGE_NUM",
        help="指定基准参考页码（从 1 开始计算，默认自动探测）",
    )
    parser.add_argument(
        "-l",
        "--legacy",
        action="store_true",
        help="使用旧版 show_pdf_page 重渲染方法（默认：matrix 矩阵方法，推荐）。",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="显示工具当前版本",
    )

    args = parser.parse_args()

    # 模式判定：未提供输入文件 → 图形界面；否则命令行处理
    if args.input is None:
        # windowed 打包：GUI 模式本就没有控制台窗口，无需任何隐藏处理
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
            log("错误：指定的页码必须 >= 1", Level.ERROR)
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
    """启动 tkinter 图形界面（GUI 模式下 tkinter 必然可用；此处做兜底）"""
    if not _TK_AVAILABLE:
        print("错误：当前 Python 环境未安装 tkinter，无法启动图形界面。", file=sys.stderr)
        print("请改用命令行模式：pdftoy <输入PDF> [-o 输出PDF] [-w] [-l] [-p 页码]", file=sys.stderr)
        sys.exit(1)
    root = tk.Tk()
    App(root)
    root.mainloop()


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
        ttk.Label(header, text="PDF 页面标准化工具", style="Title.TLabel").pack(anchor="w")
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

        global log
        log = gui_log

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




if __name__ == "__main__":
    main()
