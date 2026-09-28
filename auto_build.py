# -*- coding: utf-8 -*-
"""
文件监听自动打包
- 监听当前目录（含子目录）的所有 .py 文件变化
- 保存后延迟 1.5 秒触发打包（debounce）
- 打包期间的新变化会被合并到下一次
- Ctrl+C 停止

用法:
    python auto_build.py
"""

import os
import sys
import time
import threading
import subprocess
from datetime import datetime

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ==================== 配置 ====================
WATCH_EXTS = (".py",)                # 监听的文件类型
DEBOUNCE_SEC = 1.5                   # 保存后延迟多少秒才打包
IGNORE_DIRS = {"build", "dist", "__pycache__", ".git", ".idea", ".vscode",
               "log", "error", "covers"}
BUILD_CMD = [sys.executable, "build.py"]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def should_ignore(path):
    parts = set(path.replace("\\", "/").split("/"))
    return bool(parts & IGNORE_DIRS)


class ChangeHandler(FileSystemEventHandler):
    def __init__(self, trigger):
        super().__init__()
        self.trigger = trigger

    def on_modified(self, event):
        if event.is_directory:
            return
        if not event.src_path.endswith(WATCH_EXTS):
            return
        if should_ignore(event.src_path):
            return
        self.trigger(event.src_path)

    def on_created(self, event):
        self.on_modified(event)

    def on_moved(self, event):
        # IDE 保存常用"临时文件→正式文件"的方式
        dest = getattr(event, "dest_path", "")
        if dest.endswith(WATCH_EXTS) and not should_ignore(dest):
            self.trigger(dest)


class DebouncedBuilder:
    """
    防抖打包器：
    - 收到 change 事件时启动/重置定时器
    - 定时器到期后触发 build
    - build 期间再收到变化，标记 pending，build 完立即再跑一次
    """

    def __init__(self, delay, build_cmd):
        self.delay = delay
        self.build_cmd = build_cmd
        self.timer = None
        self.lock = threading.Lock()
        self.building = False
        self.pending = False
        self.last_change = ""

    def notify(self, path):
        with self.lock:
            self.last_change = path
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(self.delay, self._fire)
            self.timer.daemon = True
            self.timer.start()

    def _fire(self):
        with self.lock:
            if self.building:
                self.pending = True
                return
            self.building = True
        try:
            log(f"检测到变化: {os.path.basename(self.last_change)} → 开始打包")
            t0 = time.time()
            r = subprocess.run(self.build_cmd, cwd=os.getcwd())
            dt = time.time() - t0
            if r.returncode == 0:
                log(f"✅ 打包成功 ({dt:.1f}s)")
            else:
                log(f"❌ 打包失败，退出码 {r.returncode}")
        except Exception as e:
            log(f"❌ 打包异常: {type(e).__name__}: {e}")
        finally:
            with self.lock:
                self.building = False
                again = self.pending
                self.pending = False
            if again:
                log("检测到打包期间又发生变化，马上再打一次")
                self._fire()


def main():
    print("=" * 60)
    log("自动打包监听器已启动")
    log(f"监听目录: {os.getcwd()}")
    log(f"监听扩展: {WATCH_EXTS}")
    log(f"防抖延迟: {DEBOUNCE_SEC}s")
    log("按 Ctrl+C 停止")
    print("=" * 60)

    builder = DebouncedBuilder(DEBOUNCE_SEC, BUILD_CMD)
    handler = ChangeHandler(builder.notify)

    observer = Observer()
    observer.schedule(handler, os.getcwd(), recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，正在停止...")
        observer.stop()
    observer.join()
    log("已停止")


if __name__ == "__main__":
    main()