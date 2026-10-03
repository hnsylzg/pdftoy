# pdftoy

统一 PDF **页面尺寸** 并智能修复 **书签（目录）** 的命令行与图形界面一体工具，基于 [PyMuPDF](https://pymupdf.readt.io/)。

> **v1.2.0 为测试版**：本版把 CLI 与 GUI 合并进同一个文件——双击运行进图形界面（自动隐藏控制台），带参数运行走命令行。求稳请用 [v1.0.1](https://github.com/hnsylzg/pdftoy/releases/tag/v1.0.1)。

## 基本介绍

默认把整本 PDF 的页面统一到同一画布尺寸并居中缩放；加 -w 则只统一宽度，高度按原图比例自适应。同时修好书签树：剔除指向不存在页码的非法书签，保留原生书签结构。

- CLI 与 GUI 合并在一个文件：无参数运行启动图形界面；传入输入 PDF 路径则按命令行模式处理
- exe 双击启动时自动隐藏控制台窗口（进程内 ShowWindow，无副作用）；从终端启动则保留控制台，正常输出日志
- 参考页默认自动探测：扫描前若干页里频次最高的正文尺寸页；也支持 `-p` 手动指定参考页码，跳过自动探测
- 默认 matrix 方法（整本复制 + 矩阵包裹）保持零飘移并保留超链接；加 `-l` 可退回旧版 show_pdf_page 重渲染
- 日志分五个级别（INFO / OK / WARN / ERROR / DEBUG），交互式终端带 ANSI 配色
- 零额外依赖（tkinter 随 Python 标准库附带）；中文版入口为 `pdftoy_zh.py`

## 用法

```bash
python pdftoy.py                            # 无参数：启动图形界面
python pdftoy.py 输入.pdf                   # 命令行模式：自动探测参考页
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
| Python | 3.10+ | 推荐 3.12（源码用了 `X \| None` 注解） |
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

每个 bat 产出一个 exe：`build.bat` → `dist\pdftoy.exe`，`build_zh.bat` → `dist\pdftoy-zh.exe`（均为 console 打包——双击进图形界面自动隐藏窗口，终端里运行走命令行输出日志）。
`build.bat` 直接用虚拟环境里的 PyInstaller 打包，打包前会先清掉 `build/` 与 `dist/`。

### UPX 压缩

两个 bat 都带 `--upx-dir .venv\Scripts`，让 PyInstaller 从虚拟环境里找 `upx.exe`。UPX 正常工作时比不压缩小 5～7 MB。
