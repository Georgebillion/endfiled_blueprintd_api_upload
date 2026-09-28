# -*- coding: utf-8 -*-
"""
一键打包脚本
双击运行 或 命令行: python build.py
"""

import os
import sys
import time
import shutil
import subprocess
from datetime import datetime

# ==================== 配置 ====================
TARGET_SCRIPT = "bili_gui.py"
APP_NAME = "B站蓝图采集工具"
ICON_FILE = ""                    # 可选：.ico 路径，如 "app.ico"
ONE_FILE = True                   # True=单文件 exe；False=目录形式（启动更快）
WINDOWED = True                   # True=无控制台窗口（GUI 程序用）；False=保留黑框

# 附加依赖（PyInstaller 自动分析不到的模块）
HIDDEN_IMPORTS = [
    "qrcode",
    "PIL",
    "PIL.Image",
    "PIL.ImageTk",
    "selenium",
    "webdriver_manager",
]

# 附加数据文件（可选，[(源路径, 目标目录), ...]）
ADD_DATA = [
    # ("config.json", "."),
]

# ==================== 工具 ====================
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def clean_previous():
    """清理上次打包产物。"""
    for d in ("build", "dist"):
        if os.path.isdir(d):
            log(f"清理 {d}/")
            try:
                shutil.rmtree(d)
            except Exception as e:
                log(f"⚠️  清理 {d}/ 失败: {e}")


def check_target():
    if not os.path.exists(TARGET_SCRIPT):
        log(f"❌ 找不到源文件 {TARGET_SCRIPT}")
        sys.exit(1)


def build_command():
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", APP_NAME,
    ]
    cmd.append("--onefile" if ONE_FILE else "--onedir")
    if WINDOWED:
        cmd.append("--windowed")
    if ICON_FILE and os.path.exists(ICON_FILE):
        cmd += ["--icon", ICON_FILE]

    for h in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", h]

    for src, dst in ADD_DATA:
        cmd += ["--add-data", f"{src}{os.pathsep}{dst}"]

    cmd.append(TARGET_SCRIPT)
    return cmd


def run_build():
    cmd = build_command()
    log("执行: " + " ".join(cmd))
    print("-" * 60)
    t0 = time.time()
    r = subprocess.run(cmd, cwd=os.getcwd())
    dt = time.time() - t0
    print("-" * 60)
    if r.returncode != 0:
        log(f"❌ 打包失败，退出码 {r.returncode}（耗时 {dt:.1f}s）")
        sys.exit(1)
    log(f"✅ 打包成功（耗时 {dt:.1f}s）")


def show_result():
    if ONE_FILE:
        out = os.path.join("dist", f"{APP_NAME}.exe")
    else:
        out = os.path.join("dist", APP_NAME, f"{APP_NAME}.exe")
    if os.path.exists(out):
        size = os.path.getsize(out) / (1024 * 1024)
        log(f"产物: {out}  ({size:.1f} MB)")
    else:
        log(f"⚠️  未找到产物 {out}，请检查 dist/ 目录")


def main():
    print("=" * 60)
    log("开始打包")
    print("=" * 60)
    check_target()
    clean_previous()
    run_build()
    show_result()
    print("=" * 60)
    log("完成")
    print("=" * 60)
    if WINDOWED:
        input("\n按回车键退出...")   # 双击运行时保持窗口


if __name__ == "__main__":
    main()