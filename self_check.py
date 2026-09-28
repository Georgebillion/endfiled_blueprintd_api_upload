# -*- coding: utf-8 -*-
"""
代码自检脚本（独立运行，不依赖 GUI）

用法:
    python self_check.py                  # 检查默认的 bili_gui.py
    python self_check.py other.py         # 检查其他文件
    python self_check.py --no-net         # 跳过网络检查
"""

import os
import sys
import ast
import argparse
import importlib
import py_compile
import traceback
from datetime import datetime


# ---- 颜色（Windows 10+ 支持 ANSI） ----
class C:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleMode(
                ctypes.windll.kernel32.GetStdHandle(-11), 7
            )
        except Exception:
            pass
    OK = "\033[92m"
    WARN = "\033[93m"
    ERR = "\033[91m"
    INFO = "\033[94m"
    DIM = "\033[2m"
    RESET = "\033[0m"


issues = []
warnings = []


def ok(msg):
    print(f"{C.OK}✅ {msg}{C.RESET}")


def warn(msg):
    warnings.append(msg)
    print(f"{C.WARN}⚠️  {msg}{C.RESET}")


def err(msg):
    issues.append(msg)
    print(f"{C.ERR}❌ {msg}{C.RESET}")


def info(msg):
    print(f"{C.INFO}ℹ️  {msg}{C.RESET}")


def section(title):
    print(f"\n{C.DIM}{'=' * 72}{C.RESET}")
    print(f"{C.INFO}{title}{C.RESET}")
    print(f"{C.DIM}{'=' * 72}{C.RESET}")


# ============================================================
# 1. 环境
# ============================================================
def check_environment():
    section("1. 环境")
    v = sys.version_info
    info(f"Python {v.major}.{v.minor}.{v.micro} @ {sys.executable}")
    info(f"平台: {sys.platform}")
    info(f"工作目录: {os.getcwd()}")
    if v < (3, 8):
        err(f"Python {v.major}.{v.minor} 过旧，建议 3.8+")


# ============================================================
# 2. 依赖
# ============================================================
def check_dependencies():
    section("2. 依赖模块")
    deps = [
        ("requests",          "必需"),
        ("urllib3",           "必需"),
        ("qrcode",            "可选（生成二维码）"),
        ("PIL",               "可选（二维码/图片处理）"),
        ("selenium",          "可选（腾讯文档兜底）"),
        ("webdriver_manager", "可选（Selenium 驱动）"),
    ]
    for name, note in deps:
        try:
            m = importlib.import_module(name)
            ver = getattr(m, "__version__", "")
            ok(f"{name} {ver} ({note})")
        except ImportError:
            if "必需" in note:
                err(f"缺少必需依赖: {name}")
            else:
                warn(f"缺少可选依赖: {name} ({note})")


# ============================================================
# 3. 目录与文件
# ============================================================
def check_files(target_file):
    section("3. 文件与目录")
    if not os.path.exists(target_file):
        err(f"目标文件不存在: {target_file}")
        return False
    size = os.path.getsize(target_file)
    with open(target_file, "r", encoding="utf-8") as f:
        lines = sum(1 for _ in f)
    ok(f"{os.path.basename(target_file)}: {size} bytes, {lines} 行")

    for d in ("error", "log", "covers"):
        if os.path.isdir(d):
            ok(f"目录 {d}/ 存在")
        else:
            try:
                os.makedirs(d, exist_ok=True)
                warn(f"目录 {d}/ 缺失，已自动创建")
            except Exception as e:
                err(f"目录 {d}/ 无法创建: {e}")

    for fname in ("bili_cookies.json", "doc_cache.json"):
        if os.path.exists(fname):
            try:
                import json
                with open(fname, "r", encoding="utf-8") as f:
                    json.load(f)
                info(f"{fname}: 存在且有效")
            except Exception as e:
                warn(f"{fname}: 存在但无法解析 ({e})")
        else:
            info(f"{fname}: 尚未创建（首次运行后生成）")
    return True


# ============================================================
# 4. 语法检查
# ============================================================
def check_syntax(target_file):
    section("4. 语法检查")
    try:
        py_compile.compile(target_file, doraise=True)
        ok("py_compile 通过")
    except py_compile.PyCompileError as e:
        err(f"语法错误: {e}")
        return False
    except Exception as e:
        err(f"编译异常: {e}")
        return False

    try:
        with open(target_file, "r", encoding="utf-8") as f:
            src = f.read()
        ast.parse(src)
        ok("ast.parse 通过")
    except SyntaxError as e:
        err(f"语法错误: 行{e.lineno}: {e.msg}")
        return False
    return True


# ============================================================
# 5. 导入模块 + 检查类/函数
# ============================================================
def check_definitions(target_file):
    section("5. 类与函数定义")

    module_name = os.path.splitext(os.path.basename(target_file))[0]
    sys.path.insert(0, os.path.dirname(os.path.abspath(target_file)))
    try:
        mod = importlib.import_module(module_name)
    except Exception as e:
        err(f"无法 import {module_name}: {type(e).__name__}: {e}")
        print(f"{C.DIM}{traceback.format_exc()}{C.RESET}")
        return

    # 关键类
    expected_classes = {
        "BaseAPIClient":            "基础 API 客户端",
        "BlueprintCacheer":         "蓝图缓存",
        "BlueprintFiller":          "蓝图 autofill",
        "BlueprintUploader":        "蓝图上传/编辑/删除/复审",
        "BlueprintSearcher":        "蓝图搜索",
        "ImageUploader":            "图片上传",
        "BlueprintManager":         "代码提取",
        "BilibiliCommentFetcher":   "B站评论/登录",
        "BilibiliUploaderFetcher":  "UP主视频列表",
        "BilibiliUserSearcher":     "UP主昵称搜索",
        "BilibiliVideoSearcher":    "视频搜索兜底",
        "VideoCoverFetcher":        "视频封面",
        "ManualInterventionManager":"人工介入",
        "TencentDocFetcher":        "腾讯文档",
        "BlueprintGUI":             "GUI 主类",
    }
    for cls_name, desc in expected_classes.items():
        obj = getattr(mod, cls_name, None)
        if obj is None:
            err(f"类 {cls_name} ({desc}) 未定义")
        elif not isinstance(obj, type):
            err(f"{cls_name} 不是类")
        else:
            ok(f"类 {cls_name} ({desc})")

    # 关键函数
    for fn in ["run_pipeline", "extract_codes_from_video",
               "process_code_task", "merge_manual_docs_into_result",
               "build_retry_adapter", "init_folders", "main"]:
        if callable(getattr(mod, fn, None)):
            ok(f"函数 {fn}")
        else:
            err(f"函数 {fn} 未定义或不可调用")

    # BlueprintUploader 方法
    up = getattr(mod, "BlueprintUploader", None)
    if isinstance(up, type):
        for m in ("uploader", "get_blueprint", "update_blueprint",
                  "delete_blueprint", "retry_review"):
            if hasattr(up, m):
                ok(f"BlueprintUploader.{m}()")
            else:
                err(f"BlueprintUploader 缺少方法: {m}")

    # ImageUploader 关键属性
    img = getattr(mod, "ImageUploader", None)
    if isinstance(img, type):
        for attr, expect in [
            ("UPLOAD_ENDPOINT", "/api/blueprints/upload/image"),
            ("UPLOAD_FIELD_NAME", "file"),
        ]:
            actual = getattr(img, attr, None)
            if actual == expect:
                ok(f"ImageUploader.{attr} = {actual}")
            elif actual is None:
                err(f"ImageUploader 缺少属性: {attr}")
            else:
                warn(f"ImageUploader.{attr} = {actual} (预期 {expect})")

        size = getattr(img, "MAX_FILE_SIZE", 0)
        if size == 10 * 1024 * 1024:
            ok("ImageUploader.MAX_FILE_SIZE = 10MB")
        else:
            warn(f"ImageUploader.MAX_FILE_SIZE = {size} (预期 10MB)")

    # GUI 方法完整性
    gui = getattr(mod, "BlueprintGUI", None)
    if isinstance(gui, type):
        required = [
            "_build_ui", "_build_login_bar",
            "_tab_basic", "_tab_sources", "_tab_advanced",
            "_setup_logger", "_poll_log", "_append_log", "_clear_log",
            "_on_run", "_run", "_on_finished", "_on_stop", "_collect",
            "_refresh_bili_status", "_bili_status_thread",
            "_update_bili_status", "_do_bili_login", "_do_bili_logout",
            "_bili_login_thread", "_bili_login_done",
            "_create_qrcode_dialog", "_render_qr",
            "_on_qrcode_ready", "_on_qrcode_status", "_update_qr_status",
            "_cancel_bili_login",
            "_on_search_uploader", "_do_search_uploader_thread",
            "_handle_search_result", "_show_user_picker",
            "_on_search_video_uploader", "_do_video_search",
            "_do_video_search_thread", "_handle_video_search_result",
            "_show_video_search_picker", "_insert_mid",
            "_ask_manual", "_manual_dialog",
            "_on_self_check", "_self_check_thread",
            "_on_show_reviewed", "_retry_review_thread",
            "_on_close",
        ]
        missing = [m for m in required if not hasattr(gui, m)]
        if missing:
            for m in missing:
                err(f"GUI 缺少方法: {m}")
        else:
            ok(f"GUI 方法完整性 ({len(required)} 个)")

    # 全局记录列表
    if hasattr(mod, "_CREATED_BLUEPRINTS"):
        ok("全局记录 _CREATED_BLUEPRINTS 存在")
    else:
        warn("全局记录 _CREATED_BLUEPRINTS 不存在（复审记录功能将不可用）")


# ============================================================
# 6. 网络
# ============================================================
def check_network():
    section("6. 网络连通")
    try:
        import requests
    except ImportError:
        warn("requests 未安装，跳过网络检查")
        return

    for name, url in {
        "B站 API":     "https://api.bilibili.com/x/web-interface/nav",
        "蓝图平台 API": "https://end-api.shallow.ink",
        "腾讯文档":     "https://docs.qq.com/",
    }.items():
        try:
            import time
            t0 = time.time()
            r = requests.head(url, timeout=5, allow_redirects=True)
            dt = (time.time() - t0) * 1000
            ok(f"{name} 可达 (HTTP {r.status_code}, {dt:.0f}ms)")
        except Exception as e:
            warn(f"{name} 不可达: {type(e).__name__}: {e}")


# ============================================================
# 主流程
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="代码自检工具")
    parser.add_argument("file", nargs="?", default="bili_gui.py",
                        help="要检查的 Python 文件（默认 bili_gui.py）")
    parser.add_argument("--no-net", action="store_true",
                        help="跳过网络检查")
    args = parser.parse_args()

    print()
    print(f"{C.INFO}╔" + "═" * 70 + "╗")
    print(f"║  B站蓝图采集工具 - 代码自检" + " " * 42 + "║")
    print(f"║  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}" + " " * 50 + "║")
    print(f"╚" + "═" * 70 + f"╝{C.RESET}")

    check_environment()
    check_dependencies()

    if not check_files(args.file):
        return

    if not check_syntax(args.file):
        print(f"\n{C.ERR}语法检查失败，跳过后续检查。{C.RESET}")
        return

    try:
        check_definitions(args.file)
    except Exception as e:
        err(f"检查定义时异常: {type(e).__name__}: {e}")
        print(f"{C.DIM}{traceback.format_exc()}{C.RESET}")

    if not args.no_net:
        check_network()

    # ---- 汇总 ----
    section("汇总")
    if not issues and not warnings:
        print(f"{C.OK}✨ 全部通过，代码看起来是健康的。{C.RESET}")
        return

    if issues:
        print(f"{C.ERR}❌ 发现 {len(issues)} 个严重问题:{C.RESET}")
        for i, x in enumerate(issues, 1):
            print(f"   {i}. {x}")
    if warnings:
        print(f"{C.WARN}⚠️  {len(warnings)} 个警告（不影响运行）:{C.RESET}")
        for i, x in enumerate(warnings, 1):
            print(f"   {i}. {x}")

    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()