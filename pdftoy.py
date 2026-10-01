import argparse
import sys
from collections import Counter
from contextlib import closing
import fitz  # PyMuPDF

# ================= 全局元数据与配置开关 =================
__version__ = "0.1.1"

DEBUG_TOC = False  # True: 打印提取到的原始书签，方便调试奇葩 PDF
STRICT_TOC = False  # True: 开启严格模式，提高书签判定门槛
USE_EMOJI = True  # True: 使用 Emoji 前缀；False: 使用兼容性文本前缀

# 高度安全边界保护（单位：pt）
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


def log(msg, level=Level.INFO):
    """解耦日志输出逻辑，支持 Emoji/纯文本自动适配与防错兜底"""
    prefix = LEVEL_STYLE.get(level, f"[{level}]")
    print(f"{prefix} {msg}")


# ============================================


def calculate_toc_health(toc):
    """计算书签健康度得分 (0 - 100)"""
    if not toc:
        return 0, 0.0

    meaningful_score = 0.0

    for item in toc:
        level = item[0]
        title = str(item[1]).strip()
        t_lower = title.lower()

        if title.isdigit():
            continue
        if any(
            k in t_lower
            for k in (
                "page",
                "link",
                "img",
                "doc",
                "figure",
                "uncategorized",
                "anchor",
            )
        ):
            continue
        if len(title) < 2:
            continue

        score = 1.0
        if level <= 2:
            score += 0.5
        elif level > 4:
            score -= 0.3

        meaningful_score += max(0.0, score)

    ratio = meaningful_score / len(toc)
    health_score = min(100, int(ratio * 100))
    return health_score, ratio


def find_best_ref_page(doc, start=2, end=15, min_width_pt=400):
    """自动选择最优参考页（以目标标准宽度为主指标）"""
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

    # 等宽模式下：仅校验宽度是否低于安全阈值
    if most_common_size[0] < min_width_pt:
        log(
            f"检测到频次最高宽度 {most_common_size[0]}pt "
            f"低于安全阈值({min_width_pt}pt)，触发突变保护，退回首页",
            Level.WARN,
        )
        return 0

    for i in range(start, scan_end):
        box = doc[i].rect
        if (round(box.width, 1), round(box.height, 1)) == most_common_size:
            return i

    return min(4, total - 1)


def fix_pdf_scale_pro_module(
    input_pdf_path, output_pdf_path, auto_ref=True, ref_page_index=None
):
    """全功能、可编程、工业级 PDF 统一宽度处理主函数

    输出 PDF 为“流式等宽页面”：所有页面宽度与参考页保持完全一致，
    高度根据原图比例动态调整（自适应缩放），而非固定尺寸画布。

    :param input_pdf_path: 输入 PDF 路径
    :param output_pdf_path: 输出 PDF 路径
    :param auto_ref: True 时自动探测正文尺寸页；False 时使用 ref_page_index
    :param ref_page_index: 手动指定的参考页索引（0-based），仅在 auto_ref=False 时生效
    :return: 包含处理诊断信息的结构化字典，主要字段包括：
        - output_path (str): 输出文件路径
        - total_pages (int): 总页数
        - ref_page_index (int): 实际使用的参考页索引（0-based）
        - target_size_pt (tuple): 参考页基准尺寸 (width, height)
        - target_width_pt (float): 统一后的目标宽度 (pt)
        - toc_injected (bool): 是否成功注入书签
        - toc_mode (str): 书签模式（"simple" / "full" / "None"）
        - toc_count (int): 注入的书签数量
        - toc_health_score (int): 书签健康度评分（0–100）
    """
    with (
        closing(fitz.open(input_pdf_path)) as src_doc,
        closing(fitz.open()) as dst_doc,
    ):
        total_pages = len(src_doc)
        if total_pages == 0:
            raise ValueError("输入的 PDF 文件没有页面！")

        # 路径完全解耦：自动探测 vs 手动指定
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
        ref_box = ref_page.rect
        target_w = ref_box.width
        ref_h = ref_box.height

        log(
            f"【画布目标】统一宽度: {target_w:.2f} pt（高度按原图比例动态自适应）",
            Level.INFO,
        )

        # 页面标准化映射（统一宽度，高度等比自适应 + 安全保护）
        for i, page in enumerate(src_doc):
            src_box = page.rect
            orig_w = src_box.width
            orig_h = src_box.height

            # 防零除异常
            if orig_w <= 0:
                orig_w = target_w

            # 根据目标宽度计算缩放比例
            scale = target_w / orig_w
            calculated_h = orig_h * scale

            # 施加高度安全边界防御
            new_h = max(MIN_PAGE_HEIGHT, min(calculated_h, MAX_PAGE_HEIGHT))

            if calculated_h > MAX_PAGE_HEIGHT or calculated_h < MIN_PAGE_HEIGHT:
                log(
                    f"第 {i + 1} 页等比高度 ({calculated_h:.1f}pt) 触发安全限制，"
                    f"修正为: {new_h:.1f}pt",
                    Level.WARN,
                )

            # 创建宽度为 target_w，高度动态自适应的新页面
            new_page = dst_doc.new_page(width=target_w, height=new_h)

            fit_rect = fitz.Rect(0, 0, target_w, new_h)
            new_page.show_pdf_page(fit_rect, src_doc, i, clip=src_box)

            # 仅规范化 MediaBox，避免显式设置 CropBox 引发浮点数越界校验报错
            unified_box = fitz.Rect(0, 0, target_w, new_h)
            new_page.set_mediabox(unified_box)

        # 书签识别与过滤
        log("开始书签树提取与健康度诊断", Level.INFO)
        threshold = 0.8 if STRICT_TOC else 0.6
        toc_to_use = None
        final_health_score = 0
        used_mode = "None"

        toc_simple = src_doc.get_toc(simple=True)
        health_score_simple, ratio_simple = calculate_toc_health(toc_simple)

        if toc_simple and ratio_simple > threshold:
            toc_to_use = toc_simple
            final_health_score = health_score_simple
            used_mode = "simple"
            log(
                f"【书签诊断】原生书签校验通过！健康度评分:"
                f" {final_health_score}/100",
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
                    "【书签诊断】完整模式 (simple=False) 恢复成功！健康度评分:"
                    f" {final_health_score}/100",
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

        dst_doc.save(
            output_pdf_path,
            use_objstms=True,
            deflate=True,
            garbage=3,
        )
        log(f"处理完成，文件成功保存至: {output_pdf_path}", Level.OK)

        return {
            "output_path": output_pdf_path,
            "total_pages": total_pages,
            "ref_page_index": chosen_ref_index,
            "target_size_pt": (round(target_w, 2), round(ref_h, 2)),
            "target_width_pt": round(target_w, 2),
            "toc_injected": bool(injected_count > 0),
            "toc_mode": used_mode,
            "toc_count": injected_count,
            "toc_health_score": final_health_score,
        }


def main():
    """CLI 命令行入口配置"""
    parser = argparse.ArgumentParser(
        prog="pdftoy.py",
        description=(
            "PDF 页面尺寸统一工具 (PDF Page Size Unification Tool)\n"
            "默认行为：以参考页宽度为基准，所有页按原比例等比缩放高度（流式页面）。"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    # 位置参数（必选）
    parser.add_argument("input", help="输入 PDF 文件的路径")
    parser.add_argument(
        "-o",
        "--output",
        help="输出 PDF 文件的路径（默认：在原文件名后加 _fixed.pdf）",
        default=None,
    )

    # 手动覆盖开关（可选）
    parser.add_argument(
        "-p",
        "--page",
        type=int,
        metavar="PAGE_NUM",
        help=(
            "手动覆盖模式：指定参考页码（自然页码，从 1 开始计）。\n"
            "若不提供此参数，默认自动探测频次最高的最优正文宽度页。"
        ),
    )

    # 版本号输出
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="显示当前工具版本号",
    )

    args = parser.parse_args()

    # 自动推导默认输出路径
    input_path = args.input
    if args.output:
        output_path = args.output
    else:
        if input_path.lower().endswith(".pdf"):
            output_path = input_path[:-4] + "_fixed.pdf"
        else:
            output_path = input_path + "_fixed.pdf"

    # 根据是否传入 -p/--page 决定模式与参数
    if args.page is not None:
        if args.page < 1:
            log("错误：指定的页码必须 >= 1", Level.ERROR)
            sys.exit(1)
        auto_ref = False
        ref_index = args.page - 1  # 转为 0-based 索引
    else:
        auto_ref = True
        ref_index = None

    try:
        fix_pdf_scale_pro_module(
            input_pdf_path=input_path,
            output_pdf_path=output_path,
            auto_ref=auto_ref,
            ref_page_index=ref_index,
        )
    except Exception as e:
        log(f"{e}", Level.ERROR)
        sys.exit(1)


if __name__ == "__main__":
    main()
