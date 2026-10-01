import argparse
import sys
from collections import Counter
from contextlib import closing
import fitz  # PyMuPDF

# ================= 全局元数据与配置开关 =================
__version__ = "0.2.1"

DEBUG_TOC = False  # True: 打印提取到的原始书签，方便调试奇葩 PDF
STRICT_TOC = False  # True: 开启严格模式，提高书签判定门槛
USE_EMOJI = True  # True: 使用 Emoji 前缀；False: 使用兼容性文本前缀

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


LEVEL_STYLE = {
    Level.INFO: "ℹ️" if USE_EMOJI else "[INFO] ",
    Level.OK: "✅" if USE_EMOJI else "[OK]   ",
    Level.WARN: "⚠️" if USE_EMOJI else "[WARN] ",
    Level.ERROR: "❌" if USE_EMOJI else "[ERROR]",
    Level.DEBUG: "🔍" if USE_EMOJI else "[DEBUG]",
}


def log(msg: str, level: str = Level.INFO):
    """解耦日志输出逻辑，支持 Emoji/纯文本自动适配与防错兜底"""
    prefix = LEVEL_STYLE.get(level, f"[{level}]")
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


def _apply_matrix_page(
    dst_doc, page, i, target_w, target_h, fit_width, page_matrices=None
):
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
                f"【页面分析】自动探测最佳参考页: 第 {chosen_ref_index + 1} 页",
                Level.INFO,
            )
        else:
            if ref_page_index is None:
                ref_page_index = 0
            chosen_ref_index = min(max(0, ref_page_index), total_pages - 1)
            log(
                f"【页面分析】使用手动指定参考页: 第 {chosen_ref_index + 1} 页",
                Level.INFO,
            )

        ref_page = src_doc[chosen_ref_index]
        ref_box = ref_page.rect  # 使用 rect 替代 cropbox，防止带偏移导致错误裁剪
        target_w = ref_box.width
        target_h = ref_box.height

        if fit_width:
            log(
                f"【画布目标】模式: 按宽度适配 | 目标宽度: {target_w:.2f} pt（高度动态自适应）",
                Level.INFO,
            )
        else:
            log(
                f"【画布目标】模式: 固定画布 | 基准尺寸: {target_w:.2f} x {target_h:.2f} pt",
                Level.INFO,
            )

        log(
            f"【渲染方法】{'matrix 矩阵（推荐，保留超链接）' if method == 'matrix' else 'reflow 旧版 show_pdf_page'}",
            Level.INFO,
        )

        # matrix 方法需整本一次性 insert_pdf（共享资源只存一份，避免逐页复制导致体积膨胀/卡死）
        # 再逐页用 cm 仿射矩阵包裹内容流实现统一缩放+居中
        # reflow 方法逐页 new_page + show_pdf_page，无需预复制
        page_matrices = None
        named_links_count = 0
        if method == "matrix":
            dst_doc.insert_pdf(
                src_doc,
                annots=False,
                links=True,
                widgets=False,
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
                _apply_matrix_page(
                    dst_doc, page, i, target_w, target_h, fit_width, page_matrices
                )
            else:
                _apply_reflow_page(
                    dst_doc, src_doc, page, i, target_w, target_h, fit_width
                )

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
                f"【书签诊断】原生书签校验通过！健康度评分: {final_health_score}/100",
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
                    "【书签诊断】完整模式 (simple=False) 恢复成功！健康度评分: "
                    f"{final_health_score}/100",
                    Level.OK,
                )
            else:
                log(
                    "【书签诊断】未检测到有效书签（目录为空或全部为自动生成的占位书签），本次跳过书签修复",
                    Level.WARN,
                )

        injected_count = 0
        if toc_to_use:
            valid_toc = [item for item in toc_to_use if 1 <= item[2] <= total_pages]
            filtered_count = len(toc_to_use) - len(valid_toc)

            if filtered_count > 0:
                log(
                    f"【书签诊断】已自动剔除 {filtered_count} 条指向不存在页码的非法书签",
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
    """CLI 命令行入口配置"""
    parser = argparse.ArgumentParser(
        prog="pdftoy.exe",
        description=(
            "PDF 页面尺寸统一工具\n"
            "【默认模式】: matrix 矩阵方法（insert_pdf + cm 包裹，零飘移且保留超链接）\n"
            "【旧版模式】: 加 -l / --legacy 使用 show_pdf_page 重渲染方法\n"
            "【画布模式】: 默认固定参考页画布尺寸；加 -w 按宽度适配（高度等比例自适应）"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument("input", help="输入 PDF 文件的路径")
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


if __name__ == "__main__":
    main()
