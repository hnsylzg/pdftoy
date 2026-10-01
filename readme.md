# pdftoy

统一 PDF **页面尺寸** 并智能修复 **书签（目录）** 的命令行工具，基于 [PyMuPDF](https://pymupdf.readt.io/)。

## 基本介绍

默认把整本 PDF 的页面统一到同一画布尺寸并居中缩放；加 -w 则只统一宽度，高度按原图比例自适应。同时修好书签树：剔除指向不存在页码的非法书签，保留原生书签结构。

- 参考页默认自动探测：扫描前若干页里频次最高的正文尺寸页
- 也支持 `-p` 手动指定参考页码，跳过自动探测
- 默认 matrix 方法（整本复制 + 矩阵包裹）保持零飘移并保留超链接；加 `-l` 可退回旧版 show_pdf_page 重渲染
- 日志分五个级别（INFO / OK / WARN / ERROR / DEBUG），可用 Emoji 或纯文本前缀
- 单文件命令行工具，不含图形界面

## 用法

```bash
python pdftoy.py 输入.pdf                   # 自动探测参考页
python pdftoy.py 输入.pdf -o 输出.pdf       # 指定输出路径
python pdftoy.py 输入.pdf -p 3              # 手动指定第 3 页为参考页
python pdftoy.py 输入.pdf -w                # 按宽度适配，高度按原比例自适应
python pdftoy.py 输入.pdf -l                # 旧版重渲染方法（默认 matrix）
python pdftoy.py --version                  # 查看版本号
```

不指定 `-o` 时，输出文件在原文件名后加 `_fixed.pdf`。

完整参数见 `python pdftoy.py --help`。

## 运行环境与依赖

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.9+ | 推荐 3.12 |
| PyMuPDF | 1.24+ | `import pymupdf`；旧版用 `import fitz` 兼容 |

```bash
python -m venv .venv
.venv\Scripts\python -m pip install pymupdf
```

## 编译（打包成 exe）

```
.venv\Scripts\python -m pip install pyinstaller pymupdf
build.bat
```

产物是 `dist\pdftoy.exe`（console 窗口版，保留命令行输出）。`build.bat` 直接用虚拟环境里的 PyInstaller 打包，打包前会先清掉 `build/` 与 `dist/`。
