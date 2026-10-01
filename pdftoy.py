import argparse
import sys
from collections import Counter
from contextlib import closing
import fitz  # PyMuPDF

# ================= 全局元数据与配置开关 =================
__version__ = "0.1.2"

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


def fix_pdf_scale_pro_module(
    input_pdf_path: str,
    output_pdf_path: str,
    auto_ref: bool = True,
    ref_page_index: int | None = None,
    fit_width: bool = False,
) -> dict:
    """全功能、可编程、工业级 PDF 页面标准化处理主函数"""
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

        # 页面标准化处理
        # 逐页 new_page + show_pdf_page(clip=src_box)
        # 配合 use_objstms + deflate + garbage=3 保存策略
        # 实测：零偏移 + 输出体积小于原始文件
        for i, page in enumerate(src_doc):
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
        # use_objstms=True → 对象流压缩，匹配源 PDF 存储方式
        # deflate=True       → 压缩未压缩流数据
        # garbage=3          → 回收未用对象 + 去重 + 紧凑化（不重写内容流）
        # 实测效果：输出体积小于原始文件，零页面偏移
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
            "fit_width": fit_width,
            "target_size_pt": (round(target_w, 2), round(target_h, 2)),
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
            "PDF 页面尺寸统一工具\n"
            "【默认模式】: 固定参考页画布尺寸，将所有页面缩放居中。\n"
            "【按宽模式】: 添加 -w / --fit-width 参数，统一页面宽度，高度等比例自适应。"
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

    try:
        fix_pdf_scale_pro_module(
            input_pdf_path=input_path,
            output_pdf_path=output_path,
            auto_ref=auto_ref,
            ref_page_index=ref_index,
            fit_width=args.fit_width,
        )
    except Exception as e:
        log(f"{e}", Level.ERROR)
        sys.exit(1)


if __name__ == "__main__":
    main()
