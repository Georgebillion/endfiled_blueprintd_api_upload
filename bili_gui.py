# -*- coding: utf-8 -*-
"""
B站蓝图采集工具 - 单文件 GUI 版（含独立B站登录模块）
运行: python bili_gui.py
"""

import os
BP_ACCOUNT = os.environ.get("BP_ACCOUNT", "")
BP_PASSWORD = os.environ.get("BP_PASSWORD", "")
import sys
import csv
import json
import time
import queue
import importlib
import logging
import threading
import traceback
import urllib.parse
import urllib3
import re
from datetime import datetime
from functools import wraps, reduce
from hashlib import md5
from typing import Optional, Dict, Any, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog, simpledialog

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.support.ui import WebDriverWait
    from webdriver_manager.chrome import ChromeDriverManager
    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False


try:
    from video_info import BiliVideoFetcher
except ImportError:
    class BiliVideoFetcher:
        BASE = "https://api.bilibili.com/x/web-interface/view"
        HEADERS = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
        }

        def fetch(self, url: str) -> Dict[str, Any]:
            m = re.search(r"(BV[a-zA-Z0-9]+)", url or "")
            if not m:
                return {"status": "failed", "message": "无法解析 BV 号"}
            bvid = m.group(1)
            try:
                r = requests.get(
                    self.BASE, params={"bvid": bvid},
                    headers=self.HEADERS, timeout=15,
                )
                j = r.json()
                if j.get("code") != 0:
                    return {"status": "failed", "message": j.get("message")}
                d = j["data"]
                return {
                    "status": "success",
                    "basic": {
                        "bvid": d.get("bvid", ""),
                        "aid": d.get("aid"),
                        "title": d.get("title", ""),
                        "desc": d.get("desc", "") or "",
                        "pubdate": d.get("pubdate", 0),
                        "cover": d.get("pic", ""),
                        "duration": d.get("duration", 0),
                    },
                    "owner": {
                        "name": d.get("owner", {}).get("name", ""),
                        "mid": d.get("owner", {}).get("mid", 0),
                        "face": d.get("owner", {}).get("face", ""),
                    },
                    "stats": d.get("stat", {}),
                    "raw": d,
                }
            except Exception as e:
                return {"status": "failed", "message": str(e)}


AUTO_FALLBACK_NO_VERIFY = True
CURRENT_LOGGER_NAME = "bili_blueprint"


# ============================================================
# 敏感配置加载（优先级：环境变量 > config.local.json > 默认值）
# ============================================================
LOCAL_CONFIG_FILE = "config.local.json"


def _load_local_config() -> Dict[str, Any]:
    """读取 config.local.json（如果存在）。"""
    if not os.path.exists(LOCAL_CONFIG_FILE):
        return {}
    try:
        with open(LOCAL_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        print(f"[警告] 读取 {LOCAL_CONFIG_FILE} 失败: {e}")
        return {}


def _get_config(key: str, default: str = "") -> str:
    """
    按优先级读取配置：
      1. 环境变量（如 BP_ACCOUNT）
      2. config.local.json（如 {"BP_ACCOUNT": "..."}）
      3. default
    """
    # 1. 环境变量
    env_val = os.environ.get(key)
    if env_val:
        return env_val
    # 2. 本地配置文件
    local = _load_local_config()
    val = local.get(key)
    if val:
        return str(val)
    # 3. 默认值
    return default


# 在模块加载时就解析一次，供 GUI 初始化时使用
BP_ACCOUNT = _get_config("BP_ACCOUNT", "")
BP_PASSWORD = _get_config("BP_PASSWORD", "")
BP_API_ROOT = _get_config("BP_API_ROOT", "https://end-api.shallow.ink")


# 记录本次运行创建的所有蓝图（id、code、审核状态），供后续复审/编辑使用
_CREATED_BLUEPRINTS: List[Dict[str, Any]] = []


def build_retry_adapter(retries=3, backoff=1.0, pool_size=20):
    retry_strategy = Retry(
        total=retries, connect=retries, read=retries, status=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"]),
        raise_on_status=False,
    )
    return HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=pool_size, pool_maxsize=pool_size,
    )


class PrecisionFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created)
        return dt.strftime('%Y-%m-%d %H:%M:%S') + f".{dt.microsecond // 1000:03d}ms"


def init_folders():
    for folder in ['error', 'log', 'covers']:
        if not os.path.exists(folder):
            os.makedirs(folder)


init_folders()

logger = logging.getLogger(CURRENT_LOGGER_NAME)
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    fh = logging.FileHandler(
        f"log/run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
        encoding='utf-8',
    )
    sh = logging.StreamHandler()
    fmt = PrecisionFormatter('%(asctime)s [%(levelname)s] %(message)s')
    fh.setFormatter(fmt)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)

logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("selenium").setLevel(logging.WARNING)


def timer_decorator(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.perf_counter()
        result = func(*args, **kwargs)
        duration = time.perf_counter() - start_time
        logger.info(f"[计时] 执行 {func.__name__} 耗时: {duration:.4f}s")
        return result
    return wrapper


class BaseAPIClient:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.session = requests.Session()
        adapter = build_retry_adapter(retries=3, backoff=0.5, pool_size=20)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        self.access_token = ""
        self.refresh_token = ""
        self.session.headers.update({'Content-Type': 'application/json'})
        self._auth_lock = threading.Lock()

    def update_token(self, access_token: str, refresh_token: str = ""):
        self.access_token = access_token
        if refresh_token:
            self.refresh_token = refresh_token
        self.session.headers.update(
            {'Authorization': f'Bearer {access_token}' if access_token else ''}
        )

    @timer_decorator
    def login(self, account: str, password: str):
        url = f"{self.base_url}/api/v1/auth/login"
        try:
            r = self.session.post(url, json={"account": account, "password": password}, timeout=15)
            if r.status_code == 200:
                data = r.json()
                self.update_token(
                    data['data']['access_token'], data['data']['refresh_token']
                )
                logger.info("登录成功")
                return True
            logger.error(f"登录失败: {r.text}")
        except Exception as e:
            logger.error(f"登录异常: {e}")
        return False

    def _handle_auth_failure(self):
        with self._auth_lock:
            if self.refresh_token:
                try:
                    r = requests.post(
                        f"{self.base_url}/api/v1/auth/refresh",
                        json={"refresh_token": self.refresh_token}, timeout=10,
                    )
                    if r.status_code == 200:
                        self.update_token(r.json()['data']['access_token'])
                        return True
                except Exception:
                    pass
        return False

    def safe_request(self, method, url, retries=5, backoff_factor=0.5, **kwargs):
        for attempt in range(retries):
            try:
                r = self.session.request(
                    method, url, timeout=kwargs.get('timeout', 20), **kwargs
                )
                if r.status_code == 401 and self._handle_auth_failure():
                    kwargs['headers'] = self.session.headers
                    continue
                if r.status_code in (500, 502, 503, 504):
                    time.sleep(backoff_factor * (2 ** attempt))
                    continue
                try:
                    return r.json()
                except Exception:
                    return {"success": r.ok, "status_code": r.status_code, "text": r.text}
            except (requests.exceptions.RequestException, requests.exceptions.Timeout) as e:
                if attempt == retries - 1:
                    return {"error": "MAX_RETRIES_EXCEEDED", "message": str(e)}
                time.sleep(backoff_factor * (2 ** attempt))
        return {"error": "UNKNOWN_ERROR"}

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass


class BlueprintCacheer(BaseAPIClient):
    def upload_by_code(self, code):
        return self.safe_request("GET", f"{self.base_url}/api/blueprint/get?code={code}")


class BlueprintFiller(BaseAPIClient):
    def autofiller(self, code):
        return self.safe_request("POST", f"{self.base_url}/api/blueprints/autofill", json={"code": code})


class BlueprintUploader(BaseAPIClient):
    """蓝图增删改 + 复审。"""

    def uploader(self, data):
        """POST /api/blueprints —— 创建蓝图。"""
        return self.safe_request("POST", f"{self.base_url}/api/blueprints", json=data)

    def get_blueprint(self, blueprint_id):
        """GET /api/blueprints/:id —— 获取单个蓝图。"""
        return self.safe_request("GET", f"{self.base_url}/api/blueprints/{blueprint_id}")

    def update_blueprint(self, blueprint_id, data):
        """PUT /api/blueprints/:id —— 编辑蓝图（仅创建者，字段全部可选）。"""
        return self.safe_request(
            "PUT", f"{self.base_url}/api/blueprints/{blueprint_id}", json=data
        )

    def delete_blueprint(self, blueprint_id):
        """DELETE /api/blueprints/:id —— 删除蓝图（仅创建者）。"""
        return self.safe_request(
            "DELETE", f"{self.base_url}/api/blueprints/{blueprint_id}"
        )

    def retry_review(self, blueprint_id, cap_token=""):
        """
        POST /api/blueprints/:id/review —— 触发复审。
        仅 manual_pending / rejected 状态允许；pending 状态会返回错误。
        """
        body = {}
        if cap_token:
            body["cap_token"] = cap_token
        return self.safe_request(
            "POST",
            f"{self.base_url}/api/blueprints/{blueprint_id}/review",
            json=body,
        )


class BlueprintEditer(BaseAPIClient):
    def editer(self, code, data):
        return self.safe_request("PUT", f"{self.base_url}/api/blueprints/{code}", json=data)


class BlueprintSearcher(BaseAPIClient):
    def searcher(self, params):
        return self.safe_request("GET", f"{self.base_url}/api/blueprints", params=params)


# ============================================================
# 图片上传器（对接 /api/blueprints/upload/image）
# ============================================================
class ImageUploader(BaseAPIClient):
    """
    图片上传器：上传本地图片到蓝图平台的私有对象存储。

    接口: POST /api/blueprints/upload/image
         Content-Type: multipart/form-data
         file: (图片文件)

    响应:
         {
           "code": 0,
           "message": "成功",
           "data": {
             "key": "blueprints/2026/02/abc-def-123.jpg",
             "url": "https://cdn.example.com/...?token=xxx&e=1707300000"
           }
         }
    """

    UPLOAD_ENDPOINT = "/api/blueprints/upload/image"
    UPLOAD_FIELD_NAME = "file"

    MAX_FILE_SIZE = 10 * 1024 * 1024                # 10 MB
    ALLOWED_MAGIC_MIMES = ("image/jpeg", "image/png", "image/webp")

    def __init__(self, base_url: str):
        super().__init__(base_url)
        self._cache: Dict[str, Dict[str, str]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _detect_mime_from_magic(data: bytes) -> str:
        if data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
            return "image/webp"
        return "application/octet-stream"

    def upload_file(
        self,
        local_path: str,
        field_name: Optional[str] = None,
        extra_fields: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, str]]:
        if not local_path or not os.path.exists(local_path):
            logger.warning(f"图片上传失败：文件不存在 {local_path}")
            return None

        # 缓存命中
        with self._lock:
            if local_path in self._cache:
                cached = self._cache[local_path]
                ref = cached.get("key") or cached.get("url") or "(空)"
                logger.info(f"图片上传缓存命中: {ref}")
                return cached

        # 本地预检 1: 文件大小
        try:
            file_size = os.path.getsize(local_path)
            if file_size <= 0:
                logger.warning(f"图片上传失败：文件为空 {local_path}")
                return None
            if file_size > self.MAX_FILE_SIZE:
                logger.warning(
                    f"图片超过 {self.MAX_FILE_SIZE // (1024 * 1024)}MB 限制: "
                    f"{local_path} ({file_size} bytes)"
                )
                return None
        except OSError as e:
            logger.warning(f"读取图片大小失败: {e}")
            return None

        # 本地预检 2: 魔数
        try:
            with open(local_path, "rb") as f:
                magic = f.read(16)
        except OSError as e:
            logger.warning(f"读取图片失败: {e}")
            return None

        mime = self._detect_mime_from_magic(magic)
        if mime not in self.ALLOWED_MAGIC_MIMES:
            logger.warning(
                f"图片格式不支持（魔数检测为 {mime}，仅允许 jpg/png/webp）: "
                f"{local_path}"
            )
            return None

        field_name = field_name or self.UPLOAD_FIELD_NAME
        url = f"{self.base_url}{self.UPLOAD_ENDPOINT}"

        try:
            with open(local_path, "rb") as f:
                files = {
                    field_name: (os.path.basename(local_path), f, mime)
                }
                data = extra_fields or {}

                # ★ 关键修复：
                #   上传期间临时把 session 里的 Content-Type 拿掉，
                #   否则 requests 会把它合并进最终请求，
                #   导致后端收不到 multipart/form-data + boundary。
                saved_content_type = self.session.headers.pop("Content-Type", None)
                saved_content_type_lower = None
                # 兼容小写 key 的情况
                for k in list(self.session.headers.keys()):
                    if k.lower() == "content-type" and k != "Content-Type":
                        saved_content_type_lower = (
                            k, self.session.headers.pop(k)
                        )
                        break

                try:
                    r = self.session.post(
                        url, files=files, data=data, timeout=60,
                    )
                finally:
                    # 恢复 Content-Type
                    if saved_content_type is not None:
                        self.session.headers["Content-Type"] = saved_content_type
                    if saved_content_type_lower is not None:
                        self.session.headers[saved_content_type_lower[0]] = \
                            saved_content_type_lower[1]

                if r.status_code != 200:
                    logger.warning(f"图片上传 HTTP {r.status_code}: {r.text[:200]}")
                    return None

                try:
                    result = r.json()
                except Exception:
                    logger.warning(f"图片上传返回非 JSON: {r.text[:200]}")
                    return None

                if result.get("code") not in (0, None):
                    logger.warning(
                        f"图片上传业务失败: {result.get('message') or result}"
                    )
                    return None

                data_obj = result.get("data") or {}
                key = data_obj.get("key", "") or ""
                signed_url = data_obj.get("url", "") or ""

                if not key and not signed_url:
                    logger.warning(
                        f"图片上传响应缺少 key 和 url: "
                        f"{json.dumps(result, ensure_ascii=False)[:200]}"
                    )
                    return None

                info = {"key": key, "url": signed_url}
                logger.info(
                    f"图片上传成功: {os.path.basename(local_path)} -> "
                    f"key={key or '(无)'}"
                )

                with self._lock:
                    self._cache[local_path] = info
                return info

        except Exception as e:
            logger.warning(f"图片上传异常: {type(e).__name__}: {e}")
            return None
    def sign(self, key: str) -> Optional[str]:
        """
        调用 /api/blueprints/image/sign 为 key 生成新的签名 URL。
        :param key: 图片的 object key
        :return: 签名 URL 或 None
        """
        if not key:
            return None
        # 如果传入的已经是 url，先尝试从 url 里提取 key
        if key.startswith(("http://", "https://")):
            m = re.search(r'/(blueprints/[^?]+)', key)
            if m:
                key = m.group(1)

        try:
            r = self.session.get(
                f"{self.base_url}/api/blueprints/image/sign",
                params={"key": key},
                timeout=15,
            )
            if r.status_code != 200:
                logger.warning(f"签名 HTTP {r.status_code}: {r.text[:200]}")
                return None
            data = r.json()
            if data.get("code") not in (0, None):
                logger.warning(f"签名业务失败: {data.get('message') or data}")
                return None
            url = (data.get("data") or {}).get("url", "") or ""
            return url or None
        except Exception as e:
            logger.warning(f"签名请求异常: {type(e).__name__}: {e}")
            return None


class BlueprintManager:
    def __init__(self, raw_text, exclude_list=None):
        self.raw_text = raw_text or ""
        self.exclude_list = exclude_list or []

    def get_flat_codes(self):
        codes = re.findall(r'EF01[a-zA-Z0-9]+', self.raw_text)
        out, seen = [], set()
        for c in codes:
            if c not in self.exclude_list and c not in seen:
                out.append(c)
                seen.add(c)
        return out


# ============================================================
# B站评论抓取器
# ============================================================
class BilibiliCommentFetcher:
    MIXIN_KEY_ENC_TAB = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
        27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
        37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
        22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
    ]
    BASE_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }

    def __init__(self, cookie_file="bili_cookies.json"):
        self.session = requests.Session()
        self.session.headers.update(self.BASE_HEADERS)
        self.cookie_file = cookie_file
        adapter = build_retry_adapter(retries=3, backoff=1.0, pool_size=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._ssl_verify = True
        self._img_key = self._sub_key = self._mixin_key = None
        self._load_cookies()

    def _request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", 15)
        kwargs["verify"] = self._ssl_verify
        try:
            return self.session.request(method, url, **kwargs)
        except requests.exceptions.SSLError as e:
            if self._ssl_verify and AUTO_FALLBACK_NO_VERIFY:
                logger.warning(f"SSL 失败，降级 verify=False: {e}")
                self._ssl_verify = False
                kwargs["verify"] = False
                return self.session.request(method, url, **kwargs)
            raise

    def _load_cookies(self):
        if os.path.exists(self.cookie_file):
            try:
                with open(self.cookie_file, "r", encoding="utf-8") as f:
                    self.session.cookies.update(json.load(f))
            except Exception:
                pass

    def _save_cookies(self):
        with open(self.cookie_file, "w", encoding="utf-8") as f:
            json.dump(self.session.cookies.get_dict(), f, ensure_ascii=False, indent=2)

    def is_logged_in(self):
        return self.get_login_info() is not None

    def get_login_info(self):
        try:
            r = self._request("GET", "https://api.bilibili.com/x/web-interface/nav")
            d = r.json()
            if d.get("code") != 0:
                return None
            data = d.get("data", {})
            if not data.get("isLogin"):
                return None
            return {
                "uname": data.get("uname", ""),
                "mid": data.get("mid", 0),
                "is_vip": data.get("vipStatus", 0) == 1,
                "face": data.get("face", ""),
            }
        except Exception as e:
            logger.debug(f"get_login_info 异常: {e}")
            return None

    def logout(self):
        try:
            if os.path.exists(self.cookie_file):
                os.remove(self.cookie_file)
        except OSError:
            pass
        self.session.cookies.clear()
        self._img_key = self._sub_key = self._mixin_key = None
        logger.info("已退出 B站登录")

    def login_qrcode(self, poll_interval=2, timeout=180,
                     status_callback=None, qr_callback=None, cancel_event=None):
        def notify(msg):
            logger.info(msg)
            if status_callback:
                try:
                    status_callback(msg)
                except Exception:
                    pass

        r = self._request(
            "GET",
            "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
        )
        qd = r.json()
        if qd.get("code") != 0:
            notify(f"二维码申请失败: {qd.get('message')}")
            return False
        qrcode_key = qd["data"]["qrcode_key"]
        qr_url = qd["data"]["url"]

        if qr_callback:
            try:
                qr_callback(qr_url)
            except Exception as e:
                logger.warning(f"二维码回调异常: {e}")
        elif HAS_QRCODE:
            try:
                qr = qrcode.QRCode(border=4, box_size=10)
                qr.add_data(qr_url)
                qr.make(fit=True)
                qr.print_ascii(invert=True)
            except Exception:
                pass
        else:
            print(f"[提示] 请扫码: {qr_url}")

        notify("请使用 B站 App 扫描二维码")

        start = time.time()
        last_code = None
        while time.time() - start < timeout:
            if cancel_event is not None and cancel_event.is_set():
                notify("已取消登录")
                return False
            try:
                poll = self._request(
                    "GET",
                    "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                    params={"qrcode_key": qrcode_key},
                )
                pd = poll.json()
            except Exception as e:
                logger.warning(f"轮询异常: {e}")
                time.sleep(poll_interval)
                continue

            code = pd.get("data", {}).get("code")
            if code != last_code:
                if code == 0:
                    notify("登录成功")
                elif code == 86038:
                    notify("二维码已过期")
                elif code == 86090:
                    notify("已扫码，等待确认...")
                elif code == 86101:
                    notify("等待扫码...")
                else:
                    notify(f"扫码状态: {code}")
                last_code = code

            if code == 0:
                self._save_cookies()
                return True
            if code == 86038:
                return False
            time.sleep(poll_interval)

        notify("登录超时")
        return False

    def _get_wbi_keys(self):
        r = self._request("GET", "https://api.bilibili.com/x/web-interface/nav")
        d = r.json()
        wbi = d["data"]["wbi_img"]
        self._img_key = wbi["img_url"].rsplit("/", 1)[-1].split(".")[0]
        self._sub_key = wbi["sub_url"].rsplit("/", 1)[-1].split(".")[0]
        self._mixin_key = reduce(
            lambda s, i: s + (self._img_key + self._sub_key)[i],
            self.MIXIN_KEY_ENC_TAB, ""
        )[:32]

    def _enc_wbi(self, params):
        if self._mixin_key is None:
            self._get_wbi_keys()
        params = dict(params)
        params["wts"] = round(time.time())
        params = dict(sorted(params.items()))
        params = {k: "".join(c for c in str(v) if c not in "!'()*")
                  for k, v in params.items()}
        q = urllib.parse.urlencode(params)
        params["w_rid"] = md5((q + self._mixin_key).encode()).hexdigest()
        return params

    def get_video_info(self, bvid):
        r = self._request(
            "GET", "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
        )
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"获取视频信息失败: {d.get('message')}")
        return {
            "oid": d["data"]["aid"],
            "uploader_mid": d["data"]["owner"]["mid"],
            "title": d["data"].get("title", ""),
        }

    @staticmethod
    def _parse_comment(raw, uploader_mid=0):
        if not isinstance(raw, dict):
            return {"rpid": None, "content": "", "user_name": "", "user_mid": 0,
                    "like": 0, "ctime": 0, "reply_count": 0,
                    "location": "", "is_uploader": False, "root": None, "parent": None}
        member = raw.get("member") or {}
        content = raw.get("content", {})
        mid = int(member.get("mid", 0) or 0)
        uname = member.get("uname", "") or ""
        if not member and raw.get("mid"):
            mid = int(raw.get("mid", 0) or 0)
            uname = raw.get("uname", "") or ""
        if isinstance(content, str):
            cmsg = content
        elif isinstance(content, dict):
            cmsg = content.get("message", "") or ""
        else:
            cmsg = raw.get("message", "") or ""
        rc = raw.get("reply_control") or {}
        return {
            "rpid": raw.get("rpid"),
            "root": raw.get("root"),
            "parent": raw.get("parent"),
            "user_name": uname,
            "user_mid": mid,
            "content": cmsg,
            "like": raw.get("like", 0),
            "ctime": raw.get("ctime"),
            "reply_count": raw.get("rcount", 0),
            "location": rc.get("location", ""),
            "is_uploader": mid == uploader_mid,
        }

    def _fetch_main_page(self, oid, next_cursor, sort_mode, page_size):
        p = {"oid": oid, "type": 1, "mode": sort_mode, "next": next_cursor,
             "ps": page_size, "plat": 1, "web_location": 1315875}
        signed = self._enc_wbi(p)
        r = self._request("GET", "https://api.bilibili.com/x/v2/reply/wbi/main", params=signed)
        return r.json()

    def _fetch_sub_page(self, oid, root_rpid, page=1, size=20):
        p = {"oid": oid, "type": 1, "root": root_rpid, "ps": size,
             "pn": page, "web_location": 333.788}
        signed = self._enc_wbi(p)
        r = self._request("GET", "https://api.bilibili.com/x/v2/reply/reply", params=signed)
        return r.json()

    def _get_all_sub_replies(self, oid, root_rpid, max_pages=20):
        out = []
        page = 1
        while page <= max_pages:
            try:
                data = self._fetch_sub_page(oid, root_rpid, page=page)
            except Exception as e:
                logger.warning(f"楼中楼抓取异常: {e}")
                break
            if data.get("code") != 0:
                break
            pd = data.get("data", {})
            replies = pd.get("replies") or []
            if not replies:
                break
            out.extend(replies)
            total = pd.get("page", {}).get("count", 0)
            if page * 20 >= total:
                break
            page += 1
            time.sleep(1.0)
        return out

    def get_top_and_uploader_comments(
        self, bvid, max_pages=20, include_sub_replies=True,
        sub_reply_max_pages=20, ssl_retry_wait=5, ssl_max_retries=3,
    ):
        info = self.get_video_info(bvid)
        oid = info["oid"]
        uploader_mid = info["uploader_mid"]

        top_comment = None
        uploader_comments = []
        seen = set()
        roots_to_scan = []
        next_cursor = 0
        page = 0
        ssl_retry = 0

        while page < max_pages:
            try:
                data = self._fetch_main_page(oid, next_cursor, 2, 20)
            except requests.exceptions.SSLError as e:
                ssl_retry += 1
                if ssl_retry > ssl_max_retries:
                    logger.error(f"评论 SSL 失败，放弃: {e}")
                    break
                time.sleep(ssl_retry_wait)
                continue
            except Exception as e:
                logger.warning(f"评论请求异常: {e}")
                break
            ssl_retry = 0

            if data.get("code") != 0:
                logger.warning(f"评论第 {page + 1} 页失败: {data.get('message')}")
                break

            pd = data.get("data", {})

            if page == 0:
                top_container = pd.get("top") or {}
                top_raw = top_container.get("upper")
                if top_raw:
                    rpid = top_raw.get("rpid")
                    top_comment = self._parse_comment(top_raw, uploader_mid)
                    if rpid:
                        seen.add(rpid)
                    if top_comment["is_uploader"]:
                        uploader_comments.append(top_comment)
                    logger.info(
                        f"置顶评论: 作者={top_comment['user_name']} "
                        f"是UP主={top_comment['is_uploader']} "
                        f"内容长度={len(top_comment['content'])} "
                        f"子回复={top_raw.get('rcount', 0)}"
                    )
                    rcount = top_raw.get("rcount", 0) or 0
                    if rpid and rcount > 0:
                        subs = self._get_all_sub_replies(
                            oid, rpid, max_pages=sub_reply_max_pages
                        )
                        for s in subs:
                            sp = self._parse_comment(s, uploader_mid)
                            if sp["rpid"] and sp["rpid"] not in seen:
                                seen.add(sp["rpid"])
                                uploader_comments.append(sp)
                        logger.info(f"置顶楼中楼共 {len(subs)} 条")

            for r in (pd.get("replies") or []):
                rpid = r.get("rpid")
                if rpid in seen:
                    continue
                seen.add(rpid)
                if int(r.get("member", {}).get("mid", 0) or 0) == uploader_mid:
                    uploader_comments.append(self._parse_comment(r, uploader_mid))
                if include_sub_replies and r.get("rcount", 0) > 0:
                    roots_to_scan.append(rpid)

            cursor = pd.get("cursor", {})
            if cursor.get("is_end"):
                break
            next_cursor = cursor.get("next", 0)
            page += 1
            time.sleep(1.2)

        uploader_sub_replies = []
        if include_sub_replies and roots_to_scan:
            uploader_sub_replies = self._scan_sub_replies(
                oid, uploader_mid, roots_to_scan, sub_reply_max_pages
            )

        return {
            "top": top_comment,
            "uploader_comments": uploader_comments,
            "uploader_sub_replies": uploader_sub_replies,
            "scanned_pages": page + 1,
        }

    def _scan_sub_replies(self, oid, uploader_mid, root_rpids, max_pages_per_root=20):
        out = []
        for idx, rp in enumerate(root_rpids):
            page = 1
            while page <= max_pages_per_root:
                try:
                    data = self._fetch_sub_page(oid, rp, page=page)
                except Exception:
                    break
                if data.get("code") != 0:
                    break
                pd = data.get("data", {})
                replies = pd.get("replies") or []
                if not replies:
                    break
                for r in replies:
                    if int(r.get("member", {}).get("mid", 0) or 0) == uploader_mid:
                        out.append(self._parse_comment(r, uploader_mid))
                total = pd.get("page", {}).get("count", 0)
                if page * 20 >= total:
                    break
                page += 1
                time.sleep(1.0)
            if (idx + 1) % 10 == 0:
                logger.info(f"  已扫描 {idx + 1}/{len(root_rpids)} 条楼中楼")
        return out

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass


class BilibiliUploaderFetcher:
    def __init__(self, comment_fetcher):
        self.cf = comment_fetcher

    def get_uploader_info(self, mid):
        r = self.cf._request(
            "GET", "https://api.bilibili.com/x/space/acc/info",
            params={"mid": mid},
        )
        d = r.json()
        if d.get("code") != 0:
            return {"mid": mid, "name": f"UP_{mid}", "face": ""}
        x = d["data"]
        return {"mid": mid, "name": x.get("name", ""), "face": x.get("face", "")}

    def get_all_videos(self, mid, page_size=30, max_pages=50,
                       order="pubdate", keyword=""):
        out = []
        pn = 1
        while pn <= max_pages:
            raw = {"mid": mid, "ps": page_size, "pn": pn, "tid": 0,
                   "keyword": keyword, "order": order,
                   "platform": "web", "web_location": 1550101}
            signed = self.cf._enc_wbi(raw)
            try:
                r = self.cf._request(
                    "GET", "https://api.bilibili.com/x/space/wbi/arc/search",
                    params=signed,
                )
                data = r.json()
            except Exception as e:
                logger.error(f"UP主视频列表异常 [mid={mid}]: {e}")
                break

            if data.get("code") != 0:
                logger.warning(
                    f"UP主视频列表返回错误 [mid={mid}, pn={pn}]: "
                    f"{data.get('message')} (code={data.get('code')})"
                )
                break

            pd = data.get("data", {})
            vlist = pd.get("list", {}).get("vlist", []) or []
            if not vlist:
                break
            for v in vlist:
                out.append({
                    "bvid": v.get("bvid", ""),
                    "aid": v.get("aid"),
                    "title": v.get("title", ""),
                    "created": v.get("created", 0),
                    "cover": v.get("pic", ""),
                    "description": v.get("description", ""),
                })
            total = pd.get("page", {}).get("count", 0)
            logger.info(f"  UP主[mid={mid}] 第 {pn} 页，累计 {len(out)}/{total}")
            if pn * page_size >= total:
                break
            pn += 1
            time.sleep(1.5)
        return out

    def close(self):
        pass


# ============================================================
# UP主昵称搜索器
# ============================================================
class BilibiliUserSearcher:
    """
    通过昵称搜索UP主，返回候选列表。
    接口: /x/web-interface/search/type?search_type=bili_user
    需要登录态（复用 BilibiliCommentFetcher 的 session）
    """

    def __init__(self, comment_fetcher):
        self.cf = comment_fetcher

    @staticmethod
    def _clean_html(s: str) -> str:
        """去掉搜索结果里的 <em class="keyword">高亮标签。"""
        return re.sub(r'<[^>]+>', '', s or "").strip()

    def search(self, keyword: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        搜索UP主。
        :param keyword: 昵称关键字
        :param limit: 最多返回几个候选
        :return: [{"mid", "uname", "fans", "videos", "level", "sign", "face"}, ...]
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        try:
            r = self.cf._request(
                "GET",
                "https://api.bilibili.com/x/web-interface/search/type",
                params={
                    "search_type": "bili_user",
                    "keyword": keyword,
                    "page": 1,
                },
            )
            data = r.json()
        except Exception as e:
            logger.warning(f"搜索UP主请求异常: {e}")
            return []

        if data.get("code") != 0:
            logger.warning(
                f"搜索UP主接口返回错误: "
                f"{data.get('message')} (code={data.get('code')})"
            )
            return []

        result = (data.get("data") or {}).get("result") or []
        out = []
        for u in result[:limit]:
            out.append({
                "mid": int(u.get("mid", 0) or 0),
                "uname": self._clean_html(u.get("uname", "")),
                "fans": int(u.get("fans", 0) or 0),
                "videos": int(u.get("videos", 0) or 0),
                "level": int(u.get("level", 0) or 0),
                "sign": self._clean_html(u.get("usign", "")),
                "face": (u.get("upic") or "").replace("http://", "https://"),
            })
        return out


# ============================================================
# 视频搜索器（昵称搜索的兜底方案）
# ============================================================
class BilibiliVideoSearcher:
    """
    通过视频关键词搜索，从视频作者里反查 UP 主 mid。

    用途：当按昵称搜不到 UP 主时（改名、昵称含特殊符号、用户搜索索引不准等），
    用视频标题/主题词搜索，从结果里拿到视频作者的 mid。
    """

    def __init__(self, comment_fetcher):
        self.cf = comment_fetcher

    @staticmethod
    def _clean_html(s: str) -> str:
        return re.sub(r'<[^>]+>', '', s or "").strip()

    def search_videos(
        self,
        keyword: str,
        limit: int = 30,
        order: str = "totalrank",
    ) -> List[Dict[str, Any]]:
        """
        搜索视频。
        :param keyword: 视频关键词（标题 / 主题词 / UP主昵称都可以）
        :param limit: 最多返回几条
        :param order: 排序 totalrank=综合 / click=播放 / pubdate=最新
        :return: [{"bvid", "title", "author", "mid", "play", "duration", "pubdate", ...}, ...]
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        raw_params = {
            "search_type": "video",
            "keyword": keyword,
            "page": 1,
            "order": order,
            "duration": 0,           # 全部时长
            "tids": 0,               # 全部分区
            "web_location": 1430654,
        }
        signed = self.cf._enc_wbi(raw_params)

        try:
            r = self.cf._request(
                "GET",
                "https://api.bilibili.com/x/web-interface/wbi/search/type",
                params=signed,
            )
            data = r.json()
        except Exception as e:
            logger.warning(f"视频搜索请求异常: {e}")
            return []

        if data.get("code") != 0:
            logger.warning(
                f"视频搜索接口返回错误: "
                f"{data.get('message')} (code={data.get('code')})"
            )
            return []

        result = (data.get("data") or {}).get("result") or []
        out = []
        for v in result[:limit]:
            # 有些结果是广告/番剧，过滤掉
            if v.get("type") != "video":
                continue
            mid = int(v.get("mid", 0) or 0)
            if not mid:
                continue
            out.append({
                "bvid": v.get("bvid", ""),
                "aid": int(v.get("aid", 0) or 0),
                "title": self._clean_html(v.get("title", "")),
                "author": self._clean_html(v.get("author", "")),
                "mid": mid,
                "play": int(v.get("play", 0) or 0),
                "duration": str(v.get("duration", "") or ""),
                "pubdate": int(v.get("pubdate", 0) or 0),
                "description": self._clean_html(v.get("description", "")),
            })
        return out

    @staticmethod
    def extract_uploaders(
        videos: List[Dict[str, Any]],
        dedupe: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        从视频列表里提取去重后的 UP 主。
        每个 UP 主保留"播放量最高的那条视频"作为代表。
        """
        by_mid: Dict[int, Dict[str, Any]] = {}
        for v in videos:
            mid = v.get("mid", 0)
            if not mid:
                continue
            if mid not in by_mid:
                by_mid[mid] = {
                    "mid": mid,
                    "uname": v["author"],
                    "sample_title": v["title"],
                    "sample_bvid": v["bvid"],
                    "total_play": v["play"],
                    "video_count": 1,
                }
            else:
                by_mid[mid]["video_count"] += 1
                # 用播放量最高的那条作代表
                if v["play"] > by_mid[mid]["total_play"]:
                    by_mid[mid]["total_play"] = v["play"]
                    by_mid[mid]["sample_title"] = v["title"]
                    by_mid[mid]["sample_bvid"] = v["bvid"]

        out = list(by_mid.values())
        out.sort(key=lambda x: -x["total_play"])
        return out


class VideoCoverFetcher:
    BASE_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
    }
    SUPPORTED_EXTS = ("jpg", "jpeg", "png", "gif", "webp")
    ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    MAX_NAME = 60

    def __init__(self, save_dir="covers", group_by_uploader=True):
        self.save_dir = save_dir
        self.group = group_by_uploader
        os.makedirs(save_dir, exist_ok=True)
        self.session = requests.Session()
        self.session.mount("http://", build_retry_adapter(3, 0.5, 5))
        self.session.mount("https://", build_retry_adapter(3, 0.5, 5))
        self.session.headers.update(self.BASE_HEADERS)
        self._ssl_verify = True

    def _request(self, url, **kw):
        kw.setdefault("timeout", 15)
        kw["verify"] = self._ssl_verify
        try:
            return self.session.get(url, **kw)
        except requests.exceptions.SSLError:
            if self._ssl_verify and AUTO_FALLBACK_NO_VERIFY:
                self._ssl_verify = False
                kw["verify"] = False
                return self.session.get(url, **kw)
            raise

    @classmethod
    def _safe_name(cls, name):
        if not name:
            return ""
        s = cls.ILLEGAL.sub("_", str(name))
        s = re.sub(r"_+", "_", s).strip(" .")
        if len(s) > cls.MAX_NAME:
            s = s[:cls.MAX_NAME].rstrip(" .")
        return s or "_unknown"

    def _sub_dir(self, name, mid):
        if not self.group:
            return ""
        sn = self._safe_name(name)
        if mid and sn:
            return f"{mid}_{sn}"
        if mid:
            return str(mid)
        if sn:
            return sn
        return "_unknown"

    def _target_dir(self, name, mid):
        sd = self._sub_dir(name, mid)
        t = os.path.join(self.save_dir, sd) if sd else self.save_dir
        os.makedirs(t, exist_ok=True)
        return t

    def _find_cached(self, bvid, target_dir):
        for ext in self.SUPPORTED_EXTS:
            p = os.path.join(target_dir, f"{bvid}.{ext}")
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return p
        if self.group and target_dir != self.save_dir:
            for ext in self.SUPPORTED_EXTS:
                p = os.path.join(self.save_dir, f"{bvid}.{ext}")
                if os.path.exists(p) and os.path.getsize(p) > 0:
                    return p
        return None

    @staticmethod
    def _detect_mime(data):
        if data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
            return "image/webp"
        return "image/jpeg"

    @staticmethod
    def _ext(m):
        return {"image/jpeg": "jpg", "image/png": "png",
                "image/gif": "gif", "image/webp": "webp"}.get(m, "jpg")

    @staticmethod
    def _normalize(url):
        if not url:
            return ""
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("http://"):
            return "https://" + url[7:]
        return url

    def fetch(self, bvid, cover_url, uploader_name="", uploader_mid=0, force=False):
        if not bvid or not cover_url:
            return None
        cover_url = self._normalize(cover_url)
        target = self._target_dir(uploader_name, uploader_mid)
        sd = self._sub_dir(uploader_name, uploader_mid)

        if not force:
            p = self._find_cached(bvid, target)
            if p:
                try:
                    with open(p, "rb") as f:
                        mime = self._detect_mime(f.read(16))
                    size = os.path.getsize(p)
                    return {"bvid": bvid, "url": cover_url,
                            "local_path": os.path.abspath(p),
                            "relative_path": os.path.relpath(p, self.save_dir),
                            "folder": sd, "mime": mime, "size": size}
                except OSError:
                    pass
        try:
            r = self._request(cover_url)
            r.raise_for_status()
            data = r.content
            if not data:
                return None
            mime = self._detect_mime(data)
            ext = self._ext(mime)
            p = os.path.join(target, f"{bvid}.{ext}")
            for old in self.SUPPORTED_EXTS:
                op = os.path.join(target, f"{bvid}.{old}")
                if op != p and os.path.exists(op):
                    try:
                        os.remove(op)
                    except OSError:
                        pass
            with open(p, "wb") as f:
                f.write(data)
            return {"bvid": bvid, "url": cover_url,
                    "local_path": os.path.abspath(p),
                    "relative_path": os.path.relpath(p, self.save_dir),
                    "folder": sd, "mime": mime, "size": len(data)}
        except Exception as e:
            logger.warning(f"封面下载失败 [{bvid}]: {e}")
            return None

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass


class ManualInterventionManager:
    def __init__(self, cache_file="doc_cache.json", interactive=True, prompt_callback=None):
        self.cache_file = cache_file
        self.interactive = interactive
        self.prompt_callback = prompt_callback
        self.cache: Dict[str, str] = {}
        self.pending: List[str] = []
        self._load()

    def _load(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    self.cache = json.load(f)
            except Exception:
                self.cache = {}

    def _save(self):
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _key(url):
        m = re.search(r'docs\.qq\.com/([a-zA-Z]+)/([a-zA-Z0-9_\-]+)', url)
        return m.group(2) if m else url

    def get_cached(self, url):
        return self.cache.get(self._key(url))

    def set_cache(self, url, content):
        self.cache[self._key(url)] = content
        self._save()

    def has_pending(self):
        return bool(self.pending)

    def mark_failed(self, url):
        if url not in self.pending:
            self.pending.append(url)

    def flush_pending(self):
        if not self.pending:
            return {}
        pending = list(self.pending)
        self.pending.clear()

        if not self.interactive:
            logger.warning(f"非交互模式，跳过 {len(pending)} 个失败文档")
            return {}

        results = {}
        need = []
        for u in pending:
            c = self.get_cached(u)
            if c:
                results[u] = c
            else:
                need.append(u)
        if not need:
            return results

        if self.prompt_callback is not None:
            for i, u in enumerate(need, 1):
                c = None
                try:
                    c = self.prompt_callback(u, i, len(need))
                except Exception as e:
                    logger.warning(f"人工输入回调异常: {e}")
                if c and c.strip():
                    self.set_cache(u, c.strip())
                    results[u] = c.strip()
            return results
        return results


class TencentDocFetcher:
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://docs.qq.com/",
    }

    @staticmethod
    def extract_doc_links(text):
        pat = r'https?://docs\.qq\.com/[a-zA-Z]+/[a-zA-Z0-9_\-]+'
        return list(dict.fromkeys(re.findall(pat, text or "")))

    @classmethod
    def fetch_doc_text(cls, url, timeout=15, use_selenium=True, manual_manager=None):
        if manual_manager:
            c = manual_manager.get_cached(url)
            if c:
                return c

        t = cls._opendoc(url, timeout)
        if t and re.search(r'EF01[a-zA-Z0-9]+', t):
            return t

        if use_selenium and HAS_SELENIUM:
            t = cls._selenium_dom(url, timeout)
            if t and re.search(r'EF01[a-zA-Z0-9]+', t):
                return t
            t = cls._clipboard(url, timeout)
            if t and re.search(r'EF01[a-zA-Z0-9]+', t):
                return t

        t = cls._requests(url, timeout)
        if t and re.search(r'EF01[a-zA-Z0-9]+', t):
            return t

        logger.warning(f"腾讯文档抓取失败: {url}")
        if manual_manager:
            manual_manager.mark_failed(url)
        return ""

    @classmethod
    def _opendoc(cls, url, timeout=15):
        m = re.search(r'docs\.qq\.com/([a-zA-Z]+)/([a-zA-Z0-9_\-]+)', url)
        if not m:
            return ""
        did = m.group(2)
        try:
            r = requests.get(
                "https://docs.qq.com/dop-api/opendoc",
                params={"id": did, "normal": 1, "outformat": 1, "noEscape": 1},
                headers=cls.HEADERS, timeout=timeout,
            )
            if r.status_code != 200:
                return ""
            data = r.json()
            strings = []
            cls._walk(data, strings)
            return "\n".join(strings)
        except Exception:
            return ""

    @staticmethod
    def _walk(obj, out):
        if isinstance(obj, dict):
            for v in obj.values():
                TencentDocFetcher._walk(v, out)
        elif isinstance(obj, list):
            for v in obj:
                TencentDocFetcher._walk(v, out)
        elif isinstance(obj, str):
            out.append(obj)

    @staticmethod
    def _driver(timeout=20):
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        )
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        service = Service(ChromeDriverManager().install())
        drv = webdriver.Chrome(service=service, options=opts)
        drv.set_page_load_timeout(timeout)
        drv.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
        )
        return drv

    @classmethod
    def _selenium_dom(cls, url, timeout=20):
        if not HAS_SELENIUM:
            return ""
        drv = None
        try:
            drv = cls._driver(timeout)
            drv.get(url)
            time.sleep(5)
            try:
                WebDriverWait(drv, 15).until(
                    lambda d: re.search(
                        r'EF01[a-zA-Z0-9]+', d.find_element(By.TAG_NAME, "body").text
                    )
                )
            except Exception:
                pass
            sel = [
                "div[class*='editor']", "div[class*='content']",
                "div[class*='doc-content']", "div[class*='render']",
                "article", "main", "body",
            ]
            out = []
            for s in sel:
                try:
                    for e in drv.find_elements(By.CSS_SELECTOR, s):
                        t = e.text
                        if t and t not in out:
                            out.append(t)
                except Exception:
                    continue
            return "\n".join(out)
        except Exception as e:
            logger.debug(f"Selenium DOM 异常: {e}")
            return ""
        finally:
            if drv:
                try:
                    drv.quit()
                except Exception:
                    pass

    @classmethod
    def _clipboard(cls, url, timeout=30):
        if not HAS_SELENIUM:
            return ""
        drv = None
        try:
            drv = cls._driver(timeout)
            drv.get(url)
            time.sleep(5)
            try:
                drv.execute_cdp_cmd(
                    "Browser.grantPermissions",
                    {"origin": "https://docs.qq.com",
                     "permissions": ["clipboardReadWrite"]},
                )
            except Exception:
                pass
            body = drv.find_element(By.TAG_NAME, "body")
            body.click()
            time.sleep(0.5)
            ActionChains(drv).key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).perform()
            time.sleep(0.5)
            ActionChains(drv).key_down(Keys.CONTROL).send_keys("c").key_up(Keys.CONTROL).perform()
            time.sleep(1.0)
            try:
                t = drv.execute_async_script("""
                    var cb = arguments[arguments.length - 1];
                    navigator.clipboard.readText().then(function(t){cb(t);})
                        .catch(function(e){cb('');});
                """)
                if t and t.strip():
                    return t
            except Exception:
                pass
            return ""
        except Exception:
            return ""
        finally:
            if drv:
                try:
                    drv.quit()
                except Exception:
                    pass

    @classmethod
    def _requests(cls, url, timeout=15):
        s = requests.Session()
        s.headers.update(cls.HEADERS)
        s.mount("https://", build_retry_adapter(2, 0.5, 5))
        m = re.search(r'docs\.qq\.com/([a-zA-Z]+)/([a-zA-Z0-9_\-]+)', url)
        if not m:
            return ""
        did = m.group(2)
        verify = True
        try:
            api = f"https://docs.qq.com/dop-api/opendoc?id={did}&normal=1&outformat=1&noEscape=1"
            try:
                r = s.get(api, timeout=timeout, verify=verify)
            except requests.exceptions.SSLError:
                if AUTO_FALLBACK_NO_VERIFY:
                    verify = False
                    r = s.get(api, timeout=timeout, verify=False)
                else:
                    raise
            if r.status_code == 200:
                try:
                    strings = []
                    cls._walk(r.json(), strings)
                    t = "\n".join(strings)
                    if re.search(r'EF01[a-zA-Z0-9]+', t):
                        return t
                except Exception:
                    pass
        except Exception:
            pass
        try:
            try:
                r = s.get(url, timeout=timeout, verify=verify)
            except requests.exceptions.SSLError:
                if AUTO_FALLBACK_NO_VERIFY:
                    r = s.get(url, timeout=timeout, verify=False)
                else:
                    raise
            if r.status_code == 200 and re.search(r'EF01[a-zA-Z0-9]+', r.text):
                return r.text
        except Exception:
            pass
        return ""


def extract_codes_from_video(
    video_data, comment_fetcher, exclude_list,
    manual_text=None, use_manual=False, comment_max_pages=10,
    scan_sub_replies=False, fetch_doc=True, use_selenium=True,
    manual_manager=None, cover_fetcher=None, source_modes=None,
):
    if source_modes is None:
        source_modes = [1]
    enable_comment = any(m in (1, 4) for m in source_modes)
    enable_doc = any(m in (2, 4) for m in source_modes)

    result = {"desc": [], "doc": [], "top": [], "uploader": [], "uploader_sub": [],
              "merged": [], "doc_urls": [], "doc_failed_urls": []}

    if cover_fetcher is not None:
        bvid = video_data.get("basic", {}).get("bvid", "")
        cover_url = video_data.get("basic", {}).get("cover", "")
        owner = video_data.get("owner", {}) or {}
        name = owner.get("name", "") or ""
        mid = int(owner.get("mid", 0) or 0)
        if bvid and cover_url:
            info = cover_fetcher.fetch(bvid, cover_url, uploader_name=name, uploader_mid=mid)
            video_data["cover"] = info
            if info:
                logger.info(f"  → 封面: {info['relative_path']} ({info['size']} bytes)")
        else:
            video_data["cover"] = None

    if use_manual and manual_text and manual_text.strip():
        desc_text = manual_text
    else:
        desc_text = video_data.get("basic", {}).get("desc", "") or ""
        dyn = video_data.get("raw", {}).get("dynamic", "") or ""
        if dyn:
            desc_text = (desc_text + "\n" + dyn) if desc_text else dyn
    result["desc"] = BlueprintManager(desc_text, exclude_list).get_flat_codes()

    if fetch_doc and enable_doc:
        links = TencentDocFetcher.extract_doc_links(desc_text)
        result["doc_urls"] = list(links)
        for u in links:
            t = TencentDocFetcher.fetch_doc_text(
                u, use_selenium=use_selenium, manual_manager=manual_manager
            )
            if t:
                cs = BlueprintManager(t, exclude_list).get_flat_codes()
                if cs:
                    logger.info(f"  → 文档 {u}: {len(cs)} 个代码")
                    result["doc"].extend(cs)
            else:
                result["doc_failed_urls"].append(u)
        result["doc"] = list(dict.fromkeys(result["doc"]))

    if comment_fetcher is not None and enable_comment:
        bvid = video_data.get("basic", {}).get("bvid", "")
        if bvid:
            try:
                cmt = comment_fetcher.get_top_and_uploader_comments(
                    bvid, max_pages=comment_max_pages,
                    include_sub_replies=scan_sub_replies,
                )
                if cmt["top"] and cmt["top"].get("content"):
                    result["top"] = BlueprintManager(cmt["top"]["content"], exclude_list).get_flat_codes()
                for c in cmt["uploader_comments"]:
                    if c.get("content"):
                        result["uploader"].extend(
                            BlueprintManager(c["content"], exclude_list).get_flat_codes()
                        )
                result["uploader"] = list(dict.fromkeys(result["uploader"]))
                for c in cmt["uploader_sub_replies"]:
                    if c.get("content"):
                        result["uploader_sub"].extend(
                            BlueprintManager(c["content"], exclude_list).get_flat_codes()
                        )
                result["uploader_sub"] = list(dict.fromkeys(result["uploader_sub"]))
            except Exception as e:
                logger.warning(f"评论抓取失败 [{bvid}]: {e}")

    merged = []
    for k in ("desc", "doc", "top", "uploader", "uploader_sub"):
        for c in result[k]:
            if c not in merged:
                merged.append(c)
    result["merged"] = merged
    return result


def merge_manual_docs_into_result(result, manually_fetched, exclude_list):
    added = 0
    for u, content in manually_fetched.items():
        if u not in result["doc_urls"]:
            continue
        cs = BlueprintManager(content, exclude_list).get_flat_codes()
        for c in cs:
            if c not in result["doc"]:
                result["doc"].append(c)
            if c not in result["merged"]:
                result["merged"].append(c)
                added += 1
    return added


def process_code_task(code_item, video_data, link, clients,
                      source="unknown", include_cover=True):
    try:
        clients["cacheer"].upload_by_code(code_item)
        result = clients["filler"].autofiller(code_item)

        if result and "error" not in result and result.get('data'):
            data = result['data']

            def items(key):
                out = []
                for i in data.get(key, []):
                    if 'item_id' not in i:
                        continue
                    out.append({
                        'item_id': i['item_id'],
                        'name': i.get('name', ''),
                        'amount': int(i.get('quantity', i.get('amount', 0)) or 0),
                    })
                return out

            def facs(key):
                out = []
                for i in data.get(key, []):
                    if 'item_id' not in i:
                        continue
                    out.append({
                        'item_id': i['item_id'],
                        'name': i.get('name', ''),
                        'quantity': int(i.get('quantity', 0) or 0),
                    })
                return out

            bp = {
                'title': f"【{video_data['owner']['name']}-{video_data['basic']['title']}】{data['blueprint_name']}",
                'description': data.get('blueprint_desc', ''),
                'code': code_item,
                'output_items': items('output_items'),
                'input_materials': [str(i['name']) for i in data.get('input_materials', [])
                                    if isinstance(i, dict) and 'name' in i],
                'power_requirement': int(data.get('power_requirement', 0) or 0),
                'width': int(data.get('width', 0) or 0),
                'height': int(data.get('height', 0) or 0),
                'base_region': "武陵",
                'base_location': "",
                'required_facilities': facs('required_facilities'),
                'server_region': "cn",
                'is_original': False,
                'original_author': video_data['owner']['name'],
                'original_source': f"https://www.bilibili.com/video/{video_data['basic']['bvid']}",
                'status': "published",
            }

            # ---------- 封面：上传到平台，优先用 url ----------
            if include_cover:
                platform_cover_ref = None

                info = video_data.get("cover") or {}
                local_path = info.get("local_path")
                if local_path:
                    uploaded = clients["image_uploader"].upload_file(local_path)
                    if uploaded:
                        # ★ 优先 url（符合 API 文档：images/cover_image 都是 URL 字段）
                        #   url 是上传后新签发的临时签名地址，立即可访问
                        #   后端若按注释"自动提取 key 存储"，会从 url 中反解出 key
                        platform_cover_ref = (
                            uploaded.get("url") or uploaded.get("key")
                        )
                        if platform_cover_ref:
                            logger.info(
                                f"封面使用平台对象: "
                                f"{'signed_url' if uploaded.get('url') else 'key'}="
                                f"{platform_cover_ref[:80]}"
                            )

                # 回退：B站原始 URL
                if not platform_cover_ref:
                    u = video_data.get("basic", {}).get("cover", "") or ""
                    if u.startswith("//"):
                        u = "https:" + u
                    elif u.startswith("http://"):
                        u = "https://" + u[7:]
                    if u:
                        platform_cover_ref = u
                        logger.warning(
                            f"封面回退 B站 URL（可能被防盗链拦截）: "
                            f"{platform_cover_ref[:60]}..."
                        )

                if platform_cover_ref:
                    bp["cover_image"] = platform_cover_ref
                    bp["images"] = [platform_cover_ref]

            up = clients["uploader"].uploader(data=bp)
            if "error" in up or up.get("code") != 0:
                return False, {
                    "url": link, "video_title": video_data['basic']['title'],
                    "owner": video_data['owner']['name'], "code": code_item,
                    "source": source,
                    "reason": f"上传失败: {up.get('message', '未知')}",
                }

            # 记录新蓝图的 id 和 review_status
            up_data = up.get("data") or {}
            new_id = up_data.get("id") or up_data.get("_id") or ""
            review_status = up_data.get("review_status") or "unknown"
            if new_id:
                logger.info(f"  → 蓝图 ID: {new_id} (审核状态: {review_status})")
                # 全局记录，方便后续批量复审
                _CREATED_BLUEPRINTS.append({
                    "id": new_id,
                    "code": code_item,
                    "review_status": review_status,
                    "source": source,
                    "video": video_data['basic'].get('bvid', ''),
                })

            return True, code_item
        else:
            return False, {
                "url": link, "video_title": video_data['basic']['title'],
                "owner": video_data['owner']['name'], "code": code_item,
                "source": source,
                "reason": f"Autofill 失败: {result.get('message', '空数据')}",
            }
    except Exception as e:
        return False, {
            "url": link,
            "video_title": video_data.get('basic', {}).get('title', '未知'),
            "owner": video_data.get('owner', {}).get('name', '未知'),
            "code": code_item, "source": source,
            "reason": f"系统异常: {e}",
        }


def run_pipeline(
    source_modes, links=None, manual_text="", uploader_mids=None,
    uploader_max_pages=20, uploader_page_size=30, uploader_order="pubdate",
    api_root="https://end-api.shallow.ink",
    bp_account="", bp_password="",
    fetch_comments=True, scan_sub_replies=False, comment_max_pages=10,
    fetch_doc=True, use_selenium_fallback=True,
    interactive_manual=True, defer_manual=True,
    manual_prompt_callback=None,
    comment_fetcher: Optional[BilibiliCommentFetcher] = None,
    fetch_cover=True, include_cover_in_upload=True,
    cover_save_dir="covers", group_cover_by_uploader=True,
    exclude_codes=None, stop_event=None,
):
    links = links or []
    uploader_mids = uploader_mids or []
    exclude_codes = exclude_codes or [""]
    source_modes = sorted(set(int(m) for m in source_modes))
    if not source_modes:
        raise ValueError("source_modes 不能为空")
    if any(m not in (1, 2, 3, 4) for m in source_modes):
        raise ValueError("source_modes 仅支持 1/2/3/4")

    def _stopped():
        return stop_event is not None and stop_event.is_set()

    need_comment = any(m in (1, 4) for m in source_modes)
    need_uploader = 4 in source_modes
    need_manual = 3 in source_modes

    start = time.perf_counter()
    mode_names = {1: "评论", 2: "文档", 3: "手动", 4: "UP主视频"}
    logger.info("=" * 60)
    logger.info(f"运行模式: {source_modes}")
    logger.info("启用: " + " + ".join(mode_names[m] for m in source_modes))
    logger.info("=" * 60)

    clients = {
        "cacheer": BlueprintCacheer(api_root),
        "filler": BlueprintFiller(api_root),
        "uploader": BlueprintUploader(api_root),
        "editer": BlueprintEditer(api_root),
        "searcher": BlueprintSearcher(api_root),
        "image_uploader": ImageUploader(api_root),
    }
    bili = BiliVideoFetcher()

    owns_comment_fetcher = False
    if fetch_comments and need_comment:
        if comment_fetcher is None:
            owns_comment_fetcher = True
            try:
                comment_fetcher = BilibiliCommentFetcher()
                info = comment_fetcher.get_login_info()
                if info:
                    logger.info(
                        f"评论抓取器已登录: {info['uname']} (mid: {info['mid']})"
                    )
                else:
                    logger.warning("评论抓取器未登录，评论接口可能受限")
            except Exception as e:
                logger.warning(f"评论抓取器初始化失败: {e}")
                comment_fetcher = None
        else:
            info = comment_fetcher.get_login_info()
            if info:
                logger.info(
                    f"复用外部评论抓取器: {info['uname']} (mid: {info['mid']})"
                )
            else:
                logger.warning("外部评论抓取器未登录")

    uploader_fetcher = None
    if need_uploader and comment_fetcher is not None:
        uploader_fetcher = BilibiliUploaderFetcher(comment_fetcher)

    cover_fetcher = None
    if fetch_cover:
        cover_fetcher = VideoCoverFetcher(
            save_dir=cover_save_dir, group_by_uploader=group_cover_by_uploader,
        )
        logger.info(f"封面抓取器已启用: {cover_save_dir} 分组={group_cover_by_uploader}")

    if not clients["cacheer"].login(bp_account, bp_password):
        raise RuntimeError("蓝图平台登录失败")
    m = clients["cacheer"]
    for inst in clients.values():
        inst.update_token(m.access_token, m.refresh_token)

    manual_manager = ManualInterventionManager(
        cache_file="doc_cache.json",
        interactive=interactive_manual,
        prompt_callback=manual_prompt_callback,
    )

    all_extracted = []
    failed_tasks = []
    success_count = 0
    all_tasks = []

    logger.info("--- 阶段 0：收集视频 ---")
    video_set = set()
    if any(m_ in (1, 2) for m_ in source_modes):
        for lk in links:
            if lk and lk.strip():
                video_set.add(lk.strip())
        logger.info(f"来自 links: {len(video_set)} 个")

    if need_uploader:
        if uploader_fetcher is None:
            logger.error("模式4 需要评论抓取器已初始化")
        else:
            before = len(video_set)
            for mid in uploader_mids:
                if _stopped():
                    break
                logger.info(f"--- 拉取 UP主 [mid={mid}] ---")
                info = uploader_fetcher.get_uploader_info(mid)
                logger.info(f"UP主: {info['name']} (mid={mid})")
                vs = uploader_fetcher.get_all_videos(
                    mid=mid, page_size=uploader_page_size,
                    max_pages=uploader_max_pages, order=uploader_order,
                )
                logger.info(f"UP主 [{info['name']}] 共 {len(vs)} 个视频")
                for v in vs:
                    if v.get("bvid"):
                        video_set.add(f"https://www.bilibili.com/video/{v['bvid']}")
            logger.info(f"来自UP主列表: {len(video_set) - before} 个")

    video_links = sorted(video_set)
    logger.info(f"去重后视频总数: {len(video_links)}")

    manual_used = False
    if video_links:
        logger.info("--- 阶段 1：提取代码 ---")
        for idx, lk in enumerate(video_links, 1):
            if _stopped():
                logger.info(f"[已中止] {idx - 1}/{len(video_links)}")
                break
            logger.info(f"[{idx}/{len(video_links)}] {lk}")
            vd = bili.fetch(lk)
            if vd.get("status") != "success":
                failed_tasks.append({
                    "url": lk, "video_title": "N/A", "owner": "N/A",
                    "code": "N/A", "source": "video_parse",
                    "reason": f"解析失败: {vd.get('message')}",
                })
                continue

            use_manual = bool(
                need_manual and manual_text and manual_text.strip() and not manual_used
            )
            if use_manual:
                logger.info("manual_text 作为该视频补充源")
                manual_used = True

            ex = extract_codes_from_video(
                video_data=vd, comment_fetcher=comment_fetcher,
                exclude_list=exclude_codes, manual_text=manual_text,
                use_manual=use_manual, comment_max_pages=comment_max_pages,
                scan_sub_replies=scan_sub_replies, fetch_doc=fetch_doc,
                use_selenium=use_selenium_fallback,
                manual_manager=manual_manager, cover_fetcher=cover_fetcher,
                source_modes=source_modes,
            )
            logger.info(
                f"  → 描述/手动:{len(ex['desc'])} 文档:{len(ex['doc'])} "
                f"置顶:{len(ex['top'])} UP主:{len(ex['uploader'])} "
                f"其他楼中楼:{len(ex['uploader_sub'])}"
            )
            all_extracted.append((lk, vd, ex))

            if defer_manual and manual_manager.has_pending() and not _stopped():
                mf = manual_manager.flush_pending()
                add = merge_manual_docs_into_result(ex, mf, exclude_codes)
                if add:
                    logger.info(f"  人工介入补充 {add} 个代码")

        if not defer_manual and manual_manager.has_pending():
            mf = manual_manager.flush_pending()
            total = 0
            for _, _, ex in all_extracted:
                total += merge_manual_docs_into_result(ex, mf, exclude_codes)
            if total:
                logger.info(f"人工介入共补充 {total} 个代码")

    if need_manual:
        logger.info("--- 模式3：手动填入 ---")
        if manual_text and manual_text.strip() and not manual_used:
            cs = BlueprintManager(manual_text, exclude_codes).get_flat_codes()
            logger.info(f"从 manual_text 提取 {len(cs)} 个代码")
            dummy = {
                "basic": {"bvid": "MANUAL", "title": "手动填入", "cover": "", "desc": ""},
                "owner": {"name": "手动填入", "mid": 0},
                "raw": {}, "cover": None,
            }
            for c in cs:
                all_tasks.append((c, dummy, "manual", "manual"))
        elif not manual_text or not manual_text.strip():
            logger.warning("模式3 已启用，但 manual_text 为空")

    logger.info("--- 阶段 3：构建任务队列 ---")
    for lk, vd, ex in all_extracted:
        merged = []
        for k in ("desc", "doc", "top", "uploader", "uploader_sub"):
            for c in ex[k]:
                if c not in merged:
                    merged.append(c)
        ex["merged"] = merged
        if not ex["merged"]:
            logger.warning(f"{lk} 无代码")
            continue
        smap = {}
        for c in ex["desc"]:
            smap.setdefault(c, "manual" if manual_text else "desc")
        for c in ex["doc"]:
            smap.setdefault(c, "tencent_doc")
        for c in ex["top"]:
            smap.setdefault(c, "top_comment")
        for c in ex["uploader"]:
            smap.setdefault(c, "uploader_comment")
        for c in ex["uploader_sub"]:
            smap.setdefault(c, "uploader_sub_reply")
        for c in ex["merged"]:
            all_tasks.append((c, vd, lk, smap.get(c, "unknown")))

    seen = set()
    deduped = []
    for t in all_tasks:
        if t[0] in seen:
            continue
        seen.add(t[0])
        deduped.append(t)
    if len(deduped) < len(all_tasks):
        logger.info(f"全局去重: {len(all_tasks)} → {len(deduped)}")
    all_tasks = deduped
    logger.info(f"待上传任务: {len(all_tasks)}")

    if all_tasks:
        logger.info(f"--- 阶段 4：上传 {len(all_tasks)} 个任务 ---")
        with ThreadPoolExecutor(max_workers=5) as ex_:
            futs = {}
            for code, vd, lk, src in all_tasks:
                if _stopped():
                    break
                futs[ex_.submit(
                    process_code_task, code, vd, lk, clients, src,
                    include_cover_in_upload,
                )] = code
            for f in as_completed(futs):
                ok, res = f.result()
                if ok:
                    success_count += 1
                    logger.info(f"✅ 成功: {res}")
                else:
                    failed_tasks.append(res)
                    logger.warning(
                        f"❌ {res['code']} | {res.get('source')} | {res['reason']}"
                    )

    logger.info("=" * 50)
    logger.info(f"📊 成功 {success_count} | 失败 {len(failed_tasks)}")
    logger.info(f"🎯 模式: {source_modes}")

    if cover_fetcher and all_extracted:
        cc = sum(1 for _, vd, _ in all_extracted if vd.get("cover"))
        logger.info(f"🖼  封面: {cc}/{len(all_extracted)}")

    if failed_tasks:
        fn = f"error/failed_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        try:
            with open(fn, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.DictWriter(f, fieldnames=[
                    "url", "video_title", "owner", "code", "source", "reason"
                ])
                w.writeheader()
                w.writerows(failed_tasks)
            logger.info(f"💾 失败记录: {fn}")
        except Exception as e:
            logger.warning(f"保存失败记录出错: {e}")

    for inst in clients.values():
        inst.close()
    if cover_fetcher:
        cover_fetcher.close()
    if uploader_fetcher:
        uploader_fetcher.close()
    if owns_comment_fetcher and comment_fetcher:
        comment_fetcher.close()

    logger.info(f"总耗时: {time.perf_counter() - start:.2f}s")
    return success_count, len(failed_tasks)


class QueueLogHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put(self.format(record))
        except Exception:
            self.handleError(record)


class BlueprintGUI:
    QR_DISPLAY_SIZE = 400

    def __init__(self, root):
        self.root = root
        self.root.title("B站蓝图采集工具")
        self.root.geometry("1200x900")
        self.root.minsize(1000, 720)

        self.log_q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.prompt_result = None
        self.prompt_event = threading.Event()

        self.bili_fetcher: Optional[BilibiliCommentFetcher] = None
        self.user_searcher: Optional[BilibiliUserSearcher] = None
        self.video_searcher: Optional[BilibiliVideoSearcher] = None
        self.bili_logging_in = False
        self.bili_status_checking = False
        self.bili_cancel_event = threading.Event()
        self.qrcode_dialog = None
        self._qr_image_ref = None

        self._setup_logger()
        self._build_ui()
        self._poll_log()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(300, self._refresh_bili_status)

    def _setup_logger(self):
        h = QueueLogHandler(self.log_q)
        h.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
        ))
        h.setLevel(logging.INFO)
        logger.addHandler(h)

    def _build_ui(self):
        self._build_login_bar()

        top = ttk.LabelFrame(self.root, text="运行模式（可多选）", padding=10)
        top.pack(fill="x", padx=10, pady=(5, 5))
        self.mode_vars = {
            1: tk.BooleanVar(value=True),
            2: tk.BooleanVar(value=True),
            3: tk.BooleanVar(value=False),
            4: tk.BooleanVar(value=True),
        }
        for m, txt in [(1, "① 评论"), (2, "② 文档"), (3, "③ 手动"), (4, "④ UP主视频")]:
            ttk.Checkbutton(top, text=txt, variable=self.mode_vars[m]).pack(
                side="left", padx=15
            )

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=5)
        self._tab_basic(nb)
        self.var_acc = add("蓝图平台账号", "zhhyylove8@163.com")
        self.var_pwd = add("蓝图平台密码", "Yu.7198193.", show="*")
        self._tab_sources(nb)
        self._tab_advanced(nb)

        ctrl = ttk.Frame(self.root, padding=(10, 5))
        ctrl.pack(fill="x")
        self.btn_run = ttk.Button(ctrl, text="▶ 开始运行", command=self._on_run, width=15)
        self.btn_run.pack(side="left", padx=5)
        self.btn_stop = ttk.Button(ctrl, text="⏹ 停止", command=self._on_stop,
                                    width=12, state="disabled")
        self.btn_stop.pack(side="left", padx=5)
        ttk.Button(ctrl, text="🔧 自检", command=self._on_self_check,
                   width=10).pack(side="left", padx=5)
        ttk.Button(ctrl, text="📋 复审记录", command=self._on_show_reviewed,
                   width=12).pack(side="left", padx=5)
        ttk.Button(ctrl, text="🗑 清空日志", command=self._clear_log,
                   width=12).pack(side="left", padx=5)
        ttk.Button(ctrl, text="🗑 清空日志", command=self._clear_log,
                   width=12).pack(side="left", padx=5)
        ttk.Label(ctrl, text="  进度:").pack(side="left", padx=(20, 5))
        self.progress = ttk.Progressbar(ctrl, mode="indeterminate", length=300)
        self.progress.pack(side="left", padx=5, fill="x", expand=True)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(ctrl, textvariable=self.status_var,
                  foreground="blue").pack(side="right", padx=10)

        lf = ttk.LabelFrame(self.root, text="运行日志", padding=5)
        lf.pack(fill="both", expand=True, padx=10, pady=(5, 10))
        self.log_text = scrolledtext.ScrolledText(
            lf, wrap="word", height=16, font=("Consolas", 9),
            state="disabled", background="#1e1e1e", foreground="#d4d4d4",
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_config("ERROR", foreground="#f48771")
        self.log_text.tag_config("WARNING", foreground="#dcdcaa")
        self.log_text.tag_config("SUCCESS", foreground="#4ec9b0")

    def _build_login_bar(self):
        bar = ttk.LabelFrame(self.root, text="B站账号", padding=8)
        bar.pack(fill="x", padx=10, pady=(10, 5))

        self.bili_status_var = tk.StringVar(value="检查中...")
        self.bili_status_label = ttk.Label(
            bar, textvariable=self.bili_status_var,
            font=("", 10, "bold"), foreground="gray",
        )
        self.bili_status_label.pack(side="left", padx=(5, 20))

        self.bili_logout_btn = ttk.Button(
            bar, text="退出登录", command=self._do_bili_logout,
            width=12, state="disabled",
        )
        self.bili_logout_btn.pack(side="right", padx=3)

        self.bili_refresh_btn = ttk.Button(
            bar, text="刷新状态", command=self._refresh_bili_status,
            width=12,
        )
        self.bili_refresh_btn.pack(side="right", padx=3)

        self.bili_login_btn = ttk.Button(
            bar, text="扫码登录", command=self._do_bili_login,
            width=12,
        )
        self.bili_login_btn.pack(side="right", padx=3)

        ttk.Label(
            bar,
            text="（登录后 Cookie 自动保存到 bili_cookies.json，下次启动自动复用）",
            foreground="gray",
        ).pack(side="left", padx=20)

    def _tab_basic(self, nb):
        f = ttk.Frame(nb, padding=15)
        nb.add(f, text="基础参数")
        row = 0

        def add(label, default="", show=None):
            nonlocal row
            ttk.Label(f, text=label, width=18).grid(row=row, column=0, sticky="w", pady=6)
            v = tk.StringVar(value=default)
            ttk.Entry(f, textvariable=v, show=show, width=60).grid(
                row=row, column=1, sticky="we", padx=5
            )
            row += 1
            return v

        self.var_api = add("蓝图平台 API", BP_API_ROOT)
        self.var_acc = add("蓝图平台账号", BP_ACCOUNT)
        self.var_pwd = add("蓝图平台密码", BP_PASSWORD, show="*")

        ttk.Separator(f, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="we", pady=15
        )
        row += 1

        ttk.Label(f, text="抓取选项", font=("", 10, "bold")).grid(
            row=row, column=0, sticky="w", pady=5
        )
        row += 1

        self.var_fc = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="抓取评论", variable=self.var_fc).grid(
            row=row, column=0, sticky="w", pady=3
        )
        row += 1
        self.var_ssr = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="扫描所有楼中楼（慢）", variable=self.var_ssr).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=3
        )
        row += 1
        self.var_fd = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="抓取腾讯文档", variable=self.var_fd).grid(
            row=row, column=0, sticky="w", pady=3
        )
        row += 1
        self.var_sf = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="启用 Selenium 兜底（需 Chrome）",
                        variable=self.var_sf).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=3
        )
        row += 1

        ttk.Label(f, text="评论最大页数:").grid(row=row, column=0, sticky="w", pady=6)
        self.var_cmp = tk.IntVar(value=10)
        ttk.Spinbox(f, from_=1, to=100, textvariable=self.var_cmp, width=10).grid(
            row=row, column=1, sticky="w", padx=5
        )
        f.columnconfigure(1, weight=1)

    def _tab_sources(self, nb):
        f = ttk.Frame(nb, padding=15)
        nb.add(f, text="数据源")

        ttk.Label(f, text="指定视频（每行一个，模式1/2）",
                  font=("", 10, "bold")).pack(anchor="w")
        self.text_links = scrolledtext.ScrolledText(f, height=6, wrap="word", font=("Consolas", 9))
        self.text_links.pack(fill="x", pady=(3, 10))

        # UP主标题行 + 搜索按钮
        up_header = ttk.Frame(f)
        up_header.pack(anchor="w", fill="x")
        ttk.Label(up_header, text="UP主 mid（每行一个数字，模式4）",
                  font=("", 10, "bold")).pack(side="left")
        ttk.Button(
            up_header, text="🎬 按视频搜UP主",
            command=self._on_search_video_uploader, width=20,
        ).pack(side="right", padx=(5, 0))
        ttk.Button(
            up_header, text="🔍 按昵称搜索",
            command=self._on_search_uploader, width=18,
        ).pack(side="right")

        # 视频兜底选项
        opt_row = ttk.Frame(f)
        opt_row.pack(anchor="w", fill="x", pady=(3, 0))
        self.var_video_fallback = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt_row,
            text="昵称搜索无结果时，自动改用视频搜索兜底",
            variable=self.var_video_fallback,
        ).pack(anchor="w")

        self.text_mids = scrolledtext.ScrolledText(f, height=4, wrap="word", font=("Consolas", 9))
        self.text_mids.insert("1.0", "27500557")
        self.text_mids.pack(fill="x", pady=(3, 10))

        sub = ttk.Frame(f)
        sub.pack(fill="x", pady=(0, 10))
        ttk.Label(sub, text="最大页数:").pack(side="left")
        self.var_ump = tk.IntVar(value=20)
        ttk.Spinbox(sub, from_=1, to=200, textvariable=self.var_ump, width=6).pack(side="left", padx=5)
        ttk.Label(sub, text="每页:").pack(side="left", padx=(15, 0))
        self.var_ups = tk.IntVar(value=30)
        ttk.Spinbox(sub, from_=1, to=50, textvariable=self.var_ups, width=6).pack(side="left", padx=5)
        ttk.Label(sub, text="排序:").pack(side="left", padx=(15, 0))
        self.var_uo = tk.StringVar(value="pubdate")
        ttk.Combobox(sub, textvariable=self.var_uo,
                     values=["pubdate", "click", "stow"],
                     width=10, state="readonly").pack(side="left", padx=5)

        ttk.Label(f, text="手动填入代码（模式3）",
                  font=("", 10, "bold")).pack(anchor="w")
        self.text_manual = scrolledtext.ScrolledText(f, height=6, wrap="word", font=("Consolas", 9))
        self.text_manual.pack(fill="both", expand=True, pady=(3, 0))

    def _tab_advanced(self, nb):
        f = ttk.Frame(nb, padding=15)
        nb.add(f, text="高级配置")

        ttk.Label(f, text="封面", font=("", 10, "bold")).pack(anchor="w")
        self.var_fc_ = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="下载视频封面", variable=self.var_fc_).pack(anchor="w", pady=3)
        self.var_icu = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="上传封面到平台 cover_image",
                        variable=self.var_icu).pack(anchor="w", pady=3)
        self.var_gc = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="按UP主分目录", variable=self.var_gc).pack(anchor="w", pady=3)

        row = ttk.Frame(f)
        row.pack(fill="x", pady=5)
        ttk.Label(row, text="封面目录:").pack(side="left")
        self.var_cd = tk.StringVar(value="covers")
        ttk.Entry(row, textvariable=self.var_cd, width=30).pack(side="left", padx=5)

        ttk.Separator(f, orient="horizontal").pack(fill="x", pady=15)

        ttk.Label(f, text="人工介入", font=("", 10, "bold")).pack(anchor="w")
        self.var_im = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="文档抓取失败时弹窗输入",
                        variable=self.var_im).pack(anchor="w", pady=3)
        self.var_dm = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="每个视频后立即处理失败文档",
                        variable=self.var_dm).pack(anchor="w", pady=3)

        ttk.Separator(f, orient="horizontal").pack(fill="x", pady=15)

        ttk.Label(f, text="排除的蓝图码（每行一个）").pack(anchor="w")
        self.text_ex = scrolledtext.ScrolledText(f, height=4, wrap="word", font=("Consolas", 9))
        self.text_ex.pack(fill="both", expand=True, pady=3)

    
    # ============================================================
    # UP主昵称搜索
    # ============================================================
    def _on_search_uploader(self):
        """点击【按昵称搜索 UP主】按钮。"""
        if self.bili_fetcher is None or not self.bili_fetcher.is_logged_in():
            messagebox.showwarning("提示", "请先登录 B站账号（搜索接口需要登录）")
            return

        if self.user_searcher is None:
            self.user_searcher = BilibiliUserSearcher(self.bili_fetcher)

        keyword = simpledialog.askstring(
            "搜索 UP主", "请输入UP主昵称：", parent=self.root
        )
        if not keyword or not keyword.strip():
            return
        keyword = keyword.strip()

        self._append_log(f"🔍 正在搜索 UP主: {keyword}")
        threading.Thread(
            target=self._do_search_uploader_thread,
            args=(keyword,),
            daemon=True,
        ).start()

    def _do_search_uploader_thread(self, keyword):
        results = []
        err = None
        try:
            results = self.user_searcher.search(keyword, limit=20)
        except Exception as e:
            err = str(e)
            logger.error(traceback.format_exc())
        self.root.after(
            0, lambda: self._handle_search_result(keyword, results, err)
        )

    def _handle_search_result(self, keyword, results, err):
        """昵称搜索结果回到主线程后处理。"""
        if err:
            self._append_log(f"❌ 昵称搜索失败: {err}")
            messagebox.showerror("搜索失败", f"搜索UP主出错：\n\n{err}")
            return

        if not results:
            # 无结果 → 检查是否启用视频兜底
            if self.var_video_fallback.get():
                self._append_log(
                    f"⚠️  昵称搜索无结果，自动切换到视频搜索兜底: {keyword}"
                )
                self._do_video_search(keyword, fallback=True)
            else:
                self._append_log(f"⚠️  未找到匹配的UP主: {keyword}")
                messagebox.showinfo(
                    "无结果",
                    f"没有找到昵称包含“{keyword}”的UP主。\n\n"
                    f"提示: 可以勾选“昵称搜索无结果时自动改用视频搜索兜底”，"
                    f"或使用【🎬 按视频搜UP主】按钮。"
                )
            return

        self._append_log(f"🔍 “{keyword}” 找到 {len(results)} 个候选")

        # ---- 场景 1: 唯一精确匹配 ----
        kw_lower = keyword.strip().lower()
        exact = [r for r in results if r["uname"].lower() == kw_lower]
        if len(exact) == 1:
            u = exact[0]
            if messagebox.askyesno(
                "确认添加",
                f"找到唯一精确匹配：\n\n"
                f"昵称: {u['uname']}\n"
                f"MID: {u['mid']}\n"
                f"粉丝: {self._fmt_num(u['fans'])}\n"
                f"投稿: {u['videos']}\n\n"
                f"是否添加到UP主列表？"
            ):
                self._insert_mid(u["mid"], u["uname"])
            return

        # ---- 场景 2: 只有一个结果，无歧义 ----
        if len(results) == 1:
            u = results[0]
            if messagebox.askyesno(
                "确认添加",
                f"只找到一个结果：\n\n"
                f"昵称: {u['uname']}\n"
                f"MID: {u['mid']}\n"
                f"粉丝: {self._fmt_num(u['fans'])}\n\n"
                f"是否添加到UP主列表？"
            ):
                self._insert_mid(u["mid"], u["uname"])
            return

        # ---- 场景 3: 多候选，弹窗让用户选 ----
        self._show_user_picker(keyword, results)

    # ============================================================
    # 视频搜索兜底
    # ============================================================
    def _on_search_video_uploader(self):
        """点击【🎬 按视频搜UP主】按钮。"""
        if self.bili_fetcher is None or not self.bili_fetcher.is_logged_in():
            messagebox.showwarning("提示", "请先登录 B站账号（搜索接口需要登录）")
            return

        if self.video_searcher is None:
            self.video_searcher = BilibiliVideoSearcher(self.bili_fetcher)

        keyword = simpledialog.askstring(
            "按视频搜UP主",
            "请输入视频关键词：\n（标题、主题词、UP主昵称均可）",
            parent=self.root,
        )
        if not keyword or not keyword.strip():
            return
        self._do_video_search(keyword.strip(), fallback=False)

    def _do_video_search(self, keyword, fallback=False):
        self._append_log(f"🎬 正在按视频搜索: {keyword}")
        threading.Thread(
            target=self._do_video_search_thread,
            args=(keyword, fallback),
            daemon=True,
        ).start()

    def _do_video_search_thread(self, keyword, fallback):
        videos = []
        err = None
        try:
            videos = self.video_searcher.search_videos(keyword, limit=30)
        except Exception as e:
            err = str(e)
            logger.error(traceback.format_exc())
        self.root.after(
            0,
            lambda: self._handle_video_search_result(
                keyword, videos, err, fallback
            ),
        )

    def _handle_video_search_result(self, keyword, videos, err, fallback):
        if err:
            self._append_log(f"❌ 视频搜索失败: {err}")
            messagebox.showerror("搜索失败", f"视频搜索出错：\n\n{err}")
            return

        if not videos:
            self._append_log(f"⚠️  视频搜索无结果: {keyword}")
            messagebox.showinfo(
                "无结果", f"没有找到包含“{keyword}”的视频"
            )
            return

        uploaders = self.video_searcher.extract_uploaders(videos, dedupe=True)

        self._append_log(
            f"🎬 “{keyword}” 找到 {len(videos)} 个视频，"
            f"涉及 {len(uploaders)} 个 UP主"
        )

        self._show_video_search_picker(keyword, videos, uploaders)

    def _show_video_search_picker(self, keyword, videos, uploaders):
        """视频搜索结果选择对话框：Tab1 视频列表，Tab2 UP主列表。"""
        dlg = tk.Toplevel(self.root)
        dlg.title(f"按视频搜UP主 - “{keyword}”")
        dlg.geometry("1100x700")
        dlg.minsize(900, 560)
        dlg.transient(self.root)
        dlg.grab_set()

        header = ttk.Frame(dlg, padding=(12, 10, 12, 5))
        header.pack(fill="x")
        ttk.Label(
            header,
            text=f"关键词：{keyword}  ·  找到 {len(videos)} 个视频，"
                 f"涉及 {len(uploaders)} 个 UP主",
            font=("", 11, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            header,
            text="Tab1 展示视频原始结果，Tab2 按UP主去重。双击行可直接添加。",
            foreground="gray",
        ).pack(anchor="w", pady=(3, 0))

        nb = ttk.Notebook(dlg)
        nb.pack(fill="both", expand=True, padx=10, pady=5)

        # ===== Tab1: 视频列表 =====
        tab_v = ttk.Frame(nb)
        nb.add(tab_v, text=f"视频列表 ({len(videos)})")

        cols_v = ("title", "author", "mid", "play", "duration", "pubdate")
        tree_v = ttk.Treeview(
            tab_v, columns=cols_v, show="headings", selectmode="browse"
        )
        heads_v = {
            "title": ("标题", 380, "w"),
            "author": ("UP主", 150, "w"),
            "mid": ("MID", 100, "w"),
            "play": ("播放", 80, "e"),
            "duration": ("时长", 70, "center"),
            "pubdate": ("发布日期", 100, "center"),
        }
        for c, (t, w, a) in heads_v.items():
            tree_v.heading(c, text=t)
            tree_v.column(c, width=w, anchor=a, stretch=(c == "title"))

        for i, v in enumerate(videos):
            pd_str = (
                time.strftime("%Y-%m-%d", time.localtime(v["pubdate"]))
                if v["pubdate"] else ""
            )
            tree_v.insert(
                "", "end",
                iid=str(i),
                values=(
                    v["title"][:80],
                    v["author"],
                    v["mid"],
                    self._fmt_num(v["play"]),
                    v["duration"],
                    pd_str,
                ),
            )

        vsb_v = ttk.Scrollbar(tab_v, orient="vertical", command=tree_v.yview)
        tree_v.configure(yscrollcommand=vsb_v.set)
        tree_v.pack(side="left", fill="both", expand=True)
        vsb_v.pack(side="right", fill="y")

        # ===== Tab2: UP主列表（去重）=====
        tab_u = ttk.Frame(nb)
        nb.add(tab_u, text=f"UP主列表 ({len(uploaders)})")

        cols_u = ("uname", "mid", "video_count", "total_play", "sample_title")
        tree_u = ttk.Treeview(
            tab_u, columns=cols_u, show="headings", selectmode="browse"
        )
        heads_u = {
            "uname": ("昵称", 180, "w"),
            "mid": ("MID", 100, "w"),
            "video_count": ("命中视频数", 90, "center"),
            "total_play": ("代表视频播放", 110, "e"),
            "sample_title": ("代表视频标题", 420, "w"),
        }
        for c, (t, w, a) in heads_u.items():
            tree_u.heading(c, text=t)
            tree_u.column(c, width=w, anchor=a, stretch=(c == "sample_title"))

        for u in uploaders:
            tree_u.insert(
                "", "end",
                iid=str(u["mid"]),
                values=(
                    u["uname"],
                    u["mid"],
                    u["video_count"],
                    self._fmt_num(u["total_play"]),
                    u["sample_title"][:80],
                ),
            )

        vsb_u = ttk.Scrollbar(tab_u, orient="vertical", command=tree_u.yview)
        tree_u.configure(yscrollcommand=vsb_u.set)
        tree_u.pack(side="left", fill="both", expand=True)
        vsb_u.pack(side="right", fill="y")

        # 默认选中首行
        ch_v = tree_v.get_children()
        if ch_v:
            tree_v.selection_set(ch_v[0])
            tree_v.focus(ch_v[0])
        ch_u = tree_u.get_children()
        if ch_u:
            tree_u.selection_set(ch_u[0])
            tree_u.focus(ch_u[0])

        # 底部
        bf = ttk.Frame(dlg, padding=10)
        bf.pack(fill="x")

        def on_ok():
            current_tab = nb.index(nb.select())
            if current_tab == 0:
                sel = tree_v.selection()
                if not sel:
                    messagebox.showwarning("提示", "请先选择一个视频")
                    return
                idx = int(sel[0])
                v = videos[idx]
                mid, uname = v["mid"], v["author"]
            else:
                sel = tree_u.selection()
                if not sel:
                    messagebox.showwarning("提示", "请先选择一个 UP主")
                    return
                mid = int(sel[0])
                u = next(
                    (x for x in uploaders if x["mid"] == mid), None
                )
                uname = u["uname"] if u else ""

            if not mid:
                messagebox.showwarning("提示", "未获取到有效的 UP主 MID")
                return
            self._insert_mid(mid, uname)
            dlg.destroy()

        ttk.Button(bf, text="取消", command=dlg.destroy, width=12).pack(
            side="right", padx=5
        )
        ttk.Button(
            bf, text="✔ 添加该UP主", command=on_ok, width=16
        ).pack(side="right", padx=5)

        tree_v.bind("<Double-1>", lambda e: on_ok())
        tree_u.bind("<Double-1>", lambda e: on_ok())
        tree_v.bind("<Return>", lambda e: on_ok())
        tree_u.bind("<Return>", lambda e: on_ok())

        dlg.update_idletasks()
        x = self.root.winfo_x() + (
            self.root.winfo_width() - dlg.winfo_width()
        ) // 2
        y = self.root.winfo_y() + (
            self.root.winfo_height() - dlg.winfo_height()
        ) // 2
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _show_user_picker(self, keyword, results):
        """多候选弹窗，带单选 Treeview。"""
        dlg = tk.Toplevel(self.root)
        dlg.title(f"选择 UP主 - 搜索“{keyword}”")
        dlg.geometry("960x600")
        dlg.minsize(800, 500)
        dlg.transient(self.root)
        dlg.grab_set()

        # 头部
        header = ttk.Frame(dlg, padding=(12, 10, 12, 5))
        header.pack(fill="x")
        ttk.Label(
            header,
            text=f"找到 {len(results)} 个候选，请选择要添加的 UP主：",
            font=("", 11, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            header,
            text="提示: 双击一行可直接添加",
            foreground="gray",
        ).pack(anchor="w", pady=(3, 0))

        # 表格
        table_frame = ttk.Frame(dlg)
        table_frame.pack(fill="both", expand=True, padx=10, pady=5)

        cols = ("uname", "mid", "fans", "videos", "level", "sign")
        tree = ttk.Treeview(
            table_frame,
            columns=cols,
            show="headings",
            selectmode="browse",
            height=18,
        )
        headings = {
            "uname": ("昵称", 180, "w"),
            "mid": ("MID", 100, "w"),
            "fans": ("粉丝", 80, "e"),
            "videos": ("投稿", 60, "e"),
            "level": ("等级", 50, "center"),
            "sign": ("简介", 400, "w"),
        }
        for c, (txt, w, anchor) in headings.items():
            tree.heading(c, text=txt)
            tree.column(c, width=w, anchor=anchor, stretch=(c == "sign"))

        for r in results:
            sign_short = (r["sign"] or "").replace("\n", " ")[:80]
            tree.insert(
                "", "end",
                iid=str(r["mid"]),
                values=(
                    r["uname"],
                    r["mid"],
                    self._fmt_num(r["fans"]),
                    r["videos"],
                    r["level"],
                    sign_short,
                ),
            )

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # 默认选中第一项
        children = tree.get_children()
        if children:
            tree.selection_set(children[0])
            tree.focus(children[0])
            tree.see(children[0])

        # 底部按钮
        bf = ttk.Frame(dlg, padding=10)
        bf.pack(fill="x")

        def on_ok(event=None):
            sel = tree.selection()
            if not sel:
                messagebox.showwarning("提示", "请先选择一个 UP主")
                return
            mid_str = sel[0]
            values = tree.item(sel[0], "values")
            uname = values[0]
            try:
                mid = int(mid_str)
            except ValueError:
                mid = int(values[1])
            self._insert_mid(mid, uname)
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        ttk.Button(bf, text="取消", command=on_cancel, width=12).pack(
            side="right", padx=5
        )
        ttk.Button(bf, text="✔ 确定添加", command=on_ok, width=14).pack(
            side="right", padx=5
        )

        tree.bind("<Double-1>", on_ok)
        tree.bind("<Return>", on_ok)

        # 让窗口居中
        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _insert_mid(self, mid, uname):
        """把 mid 追加到 UP主列表，并去重。"""
        existing = self.text_mids.get("1.0", "end").strip()
        existing_mids = set()
        for line in existing.splitlines():
            line = line.strip()
            if line.isdigit():
                existing_mids.add(int(line))

        if mid in existing_mids:
            self._append_log(f"ℹ️  UP主 {uname} (mid: {mid}) 已在列表中")
            messagebox.showinfo("提示", f"UP主 {uname} (mid: {mid}) 已经在列表中")
            return

        if existing:
            self.text_mids.insert("end", f"\n{mid}")
        else:
            self.text_mids.insert("1.0", str(mid))
        self.text_mids.see("end")

        self._append_log(f"✅ 已添加 UP主: {uname} (mid: {mid})")

    @staticmethod
    def _fmt_num(n) -> str:
        """数字格式化: 12345 → 1.2万"""
        try:
            n = int(n)
        except (TypeError, ValueError):
            return str(n)
        if n >= 100000000:
            return f"{n / 100000000:.1f}亿"
        if n >= 10000:
            return f"{n / 10000:.1f}万"
        return str(n)


    # ============================================================
    # B站登录相关
    # ============================================================
    def _refresh_bili_status(self):
        if self.bili_status_checking:
            return
        self.bili_status_checking = True
        self.bili_refresh_btn.configure(state="disabled")
        self.bili_status_var.set("检查中...")
        self.bili_status_label.configure(foreground="gray")
        threading.Thread(target=self._bili_status_thread, daemon=True).start()

    def _bili_status_thread(self):
        info = None
        err = None
        try:
            if self.bili_fetcher is None:
                self.bili_fetcher = BilibiliCommentFetcher()
            info = self.bili_fetcher.get_login_info()
            # 登录成功后初始化 searcher
            if info and self.user_searcher is None:
                self.user_searcher = BilibiliUserSearcher(self.bili_fetcher)
                self.video_searcher = BilibiliVideoSearcher(self.bili_fetcher)
        except Exception as e:
            err = str(e)
        self.root.after(0, lambda: self._update_bili_status(info, err))

    def _update_bili_status(self, info, err=None):
        self.bili_status_checking = False
        self.bili_refresh_btn.configure(state="normal")

        if err:
            self.bili_status_var.set(f"○未登录 (状态查询失败: {err[:40]})")
            self.bili_status_label.configure(foreground="red")
            self.bili_login_btn.configure(state="normal")
            self.bili_logout_btn.configure(state="disabled")
            return

        if info:
            vip = " [大会员]" if info.get("is_vip") else ""
            self.bili_status_var.set(
                f"●已登录 - {info['uname']} (mid: {info['mid']}){vip}"
            )
            self.bili_status_label.configure(foreground="green")
            self.bili_login_btn.configure(state="disabled")
            self.bili_logout_btn.configure(state="normal")
        else:
            self.bili_status_var.set("○未登录")
            self.bili_status_label.configure(foreground="red")
            self.bili_login_btn.configure(state="normal")
            self.bili_logout_btn.configure(state="disabled")

    def _do_bili_login(self):
        if self.bili_logging_in:
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "任务运行中，请先停止后再登录")
            return

        self.bili_logging_in = True
        self.bili_login_btn.configure(state="disabled")
        self.bili_refresh_btn.configure(state="disabled")
        self.bili_cancel_event.clear()

        if self.bili_fetcher is None:
            try:
                self.bili_fetcher = BilibiliCommentFetcher()
            except Exception as e:
                messagebox.showerror("错误", f"初始化失败: {e}")
                self.bili_logging_in = False
                self.bili_login_btn.configure(state="normal")
                self.bili_refresh_btn.configure(state="normal")
                return

        self._create_qrcode_dialog()
        threading.Thread(target=self._bili_login_thread, daemon=True).start()

    def _create_qrcode_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("扫码登录 B站")
        dlg.geometry("540x760")
        dlg.minsize(540, 760)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.protocol("WM_DELETE_WINDOW", self._cancel_bili_login)

        ttk.Label(dlg, text="请使用 B站 App 扫描二维码登录",
                  font=("", 12, "bold")).pack(pady=(18, 8))

        # 原生 tk.Label（支持 image + 像素尺寸，且不裁剪）
        img_label = tk.Label(
            dlg,
            text="正在申请二维码...",
            anchor="center",
            bg="#f0f0f0",
            relief="solid",
            borderwidth=1,
        )
        img_label.pack(pady=10, padx=20)

        status_label = ttk.Label(dlg, text="正在申请二维码...",
                                 foreground="blue", font=("", 11))
        status_label.pack(pady=8)

        url_label = ttk.Label(dlg, text="", wraplength=480, foreground="gray")
        url_label.pack(pady=5, padx=20)

        btn = ttk.Button(dlg, text="取消登录", command=self._cancel_bili_login)
        btn.pack(pady=15)

        self.qrcode_dialog = {
            "dlg": dlg,
            "img_label": img_label,
            "status_label": status_label,
            "url_label": url_label,
        }

    def _bili_login_thread(self):
        ok = False
        try:
            ok = self.bili_fetcher.login_qrcode(
                status_callback=self._on_qrcode_status,
                qr_callback=self._on_qrcode_ready,
                cancel_event=self.bili_cancel_event,
            )
        except Exception as e:
            logger.error(f"登录异常: {e}")
            logger.error(traceback.format_exc())
        finally:
            self.root.after(0, lambda: self._bili_login_done(ok))

    def _on_qrcode_ready(self, qr_url):
        self.root.after(0, lambda: self._render_qr(qr_url))

    def _render_qr(self, qr_url):
        if not self.qrcode_dialog:
            return

        if not (HAS_QRCODE and HAS_PIL):
            self.qrcode_dialog["img_label"].configure(
                image="",
                text="未安装 qrcode/Pillow\n请复制下方 URL 到手机浏览器打开",
                font=("", 11), width=50, height=15,
            )
            self.qrcode_dialog["url_label"].configure(text=qr_url)
            self.qrcode_dialog["dlg"].update_idletasks()
            return

        try:
            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10,
                border=4,
            )
            qr.add_data(qr_url)
            qr.make(fit=True)

            img = qr.make_image(fill_color="black", back_color="white")
            img = img.convert("RGB")

            target = self.QR_DISPLAY_SIZE
            img = img.resize((target, target), Image.LANCZOS)

            tkimg = ImageTk.PhotoImage(img)
            self._qr_image_ref = tkimg

            self.qrcode_dialog["img_label"].configure(image=tkimg, text="")
            self.qrcode_dialog["img_label"].image = tkimg

            self.qrcode_dialog["dlg"].update_idletasks()
            self.qrcode_dialog["url_label"].configure(text=qr_url)

        except Exception as e:
            logger.warning(f"渲染二维码失败: {e}")
            logger.warning(traceback.format_exc())
            self.qrcode_dialog["img_label"].configure(
                image="",
                text="二维码渲染失败\n请复制下方 URL 到手机浏览器打开",
                font=("", 11), width=50, height=15,
            )
            self.qrcode_dialog["url_label"].configure(text=qr_url)

    def _on_qrcode_status(self, msg):
        self.root.after(0, lambda: self._update_qr_status(msg))

    def _update_qr_status(self, msg):
        if self.qrcode_dialog:
            try:
                self.qrcode_dialog["status_label"].configure(text=msg)
            except Exception:
                pass

    def _bili_login_done(self, ok):
        self.bili_logging_in = False
        self.bili_login_btn.configure(state="normal")
        self.bili_refresh_btn.configure(state="normal")

        if self.qrcode_dialog:
            try:
                self.qrcode_dialog["dlg"].destroy()
            except Exception:
                pass
            self.qrcode_dialog = None
        self._qr_image_ref = None

        if ok:
            self._append_log("✅ B站扫码登录成功")
        else:
            self._append_log("⚠️  B站扫码登录未完成（取消/超时/失败）")
        self._refresh_bili_status()

    def _cancel_bili_login(self):
        self.bili_cancel_event.set()
        if self.qrcode_dialog:
            try:
                self.qrcode_dialog["dlg"].destroy()
            except Exception:
                pass
            self.qrcode_dialog = None
        self._qr_image_ref = None

    def _do_bili_logout(self):
        if not messagebox.askyesno("确认", "确认退出 B站登录？将删除本地 Cookie。"):
            return
        if self.bili_fetcher is None:
            try:
                self.bili_fetcher = BilibiliCommentFetcher()
            except Exception:
                pass
        if self.bili_fetcher:
            self.bili_fetcher.logout()
        self._append_log("ℹ️  已退出 B站登录")
        self._refresh_bili_status()

    # ============================================================
    # 日志
    # ============================================================
    def _poll_log(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log)

    def _append_log(self, msg):
        self.log_text.configure(state="normal")
        tag = "INFO"
        if "[ERROR]" in msg or "❌" in msg:
            tag = "ERROR"
        elif "[WARNING]" in msg or "⚠️" in msg:
            tag = "WARNING"
        elif "成功" in msg or "✅" in msg:
            tag = "SUCCESS"
        self.log_text.insert("end", msg + "\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ============================================================
    # 代码自检
    # ============================================================
    def _on_self_check(self):
        """点击【🔧 自检】按钮——后台线程运行，不阻塞 UI。"""
        self._append_log("\n" + "=" * 70)
        self._append_log("🔧 开始代码自检")
        self._append_log("=" * 70)
        threading.Thread(target=self._self_check_thread, daemon=True).start()

    def _self_check_thread(self):
        """自检主逻辑：依赖、类、函数、方法、目录、语法、网络。"""
        issues: List[str] = []
        warnings: List[str] = []

        def ok(msg):
            self.log_q.put(f"✅ {msg}")

        def warn(msg):
            warnings.append(msg)
            self.log_q.put(f"⚠️  {msg}")

        def err(msg):
            issues.append(msg)
            self.log_q.put(f"❌ {msg}")

        def info(msg):
            self.log_q.put(f"ℹ️  {msg}")

        # ---- 1. Python 版本 ----
        v = sys.version_info
        info(f"Python {v.major}.{v.minor}.{v.micro} ({sys.executable})")
        if v < (3, 8):
            err(f"Python {v.major}.{v.minor} 过旧，建议 3.8+")

        # ---- 2. 依赖模块 ----
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
                importlib.import_module(name)
                ok(f"依赖 {name} ({note})")
            except ImportError:
                if "必需" in note:
                    err(f"缺少必需依赖: {name}")
                else:
                    warn(f"缺少可选依赖: {name} ({note})")

        # ---- 3. 目录结构 ----
        for d in ("error", "log", "covers"):
            if os.path.isdir(d):
                ok(f"目录 {d}/ 存在")
            else:
                try:
                    os.makedirs(d, exist_ok=True)
                    warn(f"目录 {d}/ 缺失，已自动创建")
                except Exception as e:
                    err(f"目录 {d}/ 无法创建: {e}")

        # ---- 4. 关键类 ----
        expected_classes = {
            "BaseAPIClient":            "基础 API 客户端",
            "BlueprintCacheer":         "蓝图缓存",
            "BlueprintFiller":          "蓝图 autofill",
            "BlueprintUploader":        "蓝图上传/编辑/删除/复审",
            "BlueprintEditer":          "蓝图编辑（旧）",
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
            "QueueLogHandler":          "日志队列 Handler",
            "BlueprintGUI":             "GUI 主类",
        }
        g = globals()
        for cls_name, desc in expected_classes.items():
            obj = g.get(cls_name)
            if obj is None:
                err(f"类 {cls_name} ({desc}) 未定义")
            elif not isinstance(obj, type):
                err(f"{cls_name} 不是类")
            else:
                ok(f"类 {cls_name} ({desc})")

        # ---- 5. 关键函数 ----
        expected_fns = [
            "run_pipeline",
            "extract_codes_from_video",
            "process_code_task",
            "merge_manual_docs_into_result",
            "build_retry_adapter",
            "init_folders",
        ]
        for fn in expected_fns:
            if callable(g.get(fn)):
                ok(f"函数 {fn}")
            else:
                err(f"函数 {fn} 未定义或不可调用")

        # ---- 6. BlueprintUploader 方法完整性 ----
        up_cls = g.get("BlueprintUploader")
        if isinstance(up_cls, type):
            for m in ("uploader", "get_blueprint",
                      "update_blueprint", "delete_blueprint", "retry_review"):
                if hasattr(up_cls, m):
                    ok(f"BlueprintUploader.{m}()")
                else:
                    err(f"BlueprintUploader 缺少方法: {m}")

        # ---- 7. ImageUploader 关键属性 ----
        img_cls = g.get("ImageUploader")
        if isinstance(img_cls, type):
            for attr, expect in [
                ("UPLOAD_ENDPOINT", "/api/blueprints/upload/image"),
                ("UPLOAD_FIELD_NAME", "file"),
            ]:
                if hasattr(img_cls, attr):
                    actual = getattr(img_cls, attr)
                    if actual == expect:
                        ok(f"ImageUploader.{attr} = {actual}")
                    else:
                        warn(f"ImageUploader.{attr} = {actual}（预期 {expect}）")
                else:
                    err(f"ImageUploader 缺少属性: {attr}")

            # 大小限制
            size = getattr(img_cls, "MAX_FILE_SIZE", 0)
            if size == 10 * 1024 * 1024:
                ok(f"ImageUploader.MAX_FILE_SIZE = 10MB")
            else:
                warn(f"ImageUploader.MAX_FILE_SIZE = {size}（预期 10MB）")

        # ---- 8. GUI 自身方法完整性 ----
        required_gui_methods = [
            # 基础
            "_build_ui", "_build_login_bar",
            "_tab_basic", "_tab_sources", "_tab_advanced",
            "_setup_logger", "_poll_log", "_append_log", "_clear_log",
            # 运行
            "_on_run", "_run", "_on_finished", "_on_stop", "_collect",
            # B站登录
            "_refresh_bili_status", "_bili_status_thread",
            "_update_bili_status", "_do_bili_login", "_do_bili_logout",
            "_bili_login_thread", "_bili_login_done",
            "_create_qrcode_dialog", "_render_qr",
            "_on_qrcode_ready", "_on_qrcode_status", "_update_qr_status",
            "_cancel_bili_login",
            # 搜索
            "_on_search_uploader", "_do_search_uploader_thread",
            "_handle_search_result", "_show_user_picker",
            "_on_search_video_uploader", "_do_video_search",
            "_do_video_search_thread", "_handle_video_search_result",
            "_show_video_search_picker", "_insert_mid",
            # 人工介入
            "_ask_manual", "_manual_dialog",
            # 自检
            "_on_self_check", "_self_check_thread",
            "_on_show_reviewed",
            # 关闭
            "_on_close",
        ]
        for m in required_gui_methods:
            if hasattr(self, m):
                ok(f"GUI 方法 {m}()")
            else:
                err(f"GUI 缺少方法: {m}")

        # ---- 9. 语法自检（用 ast 解析当前源文件）----
        try:
            import ast
            # 打包成 exe 时 __file__ 可能不可靠，做兼容
            src_file = None
            if not getattr(sys, "frozen", False):
                src_file = os.path.abspath(__file__)
            else:
                src_file = sys.argv[0]
            if src_file and os.path.exists(src_file):
                with open(src_file, "r", encoding="utf-8") as f:
                    src = f.read()
                ast.parse(src)
                ok(f"语法检查通过: {os.path.basename(src_file)}")
            else:
                warn(f"无法定位源文件，跳过语法检查")
        except SyntaxError as e:
            err(f"语法错误: 行{e.lineno}: {e.msg}")
        except Exception as e:
            warn(f"语法检查跳过: {e}")

        # ---- 10. 配置文件状态 ----
        for fname in ("bili_cookies.json", "doc_cache.json"):
            if os.path.exists(fname):
                try:
                    with open(fname, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    info(f"{fname}: 存在（{len(d)} 项）")
                except Exception as e:
                    warn(f"{fname}: 存在但无法解析 ({e})")
            else:
                info(f"{fname}: 尚未创建（首次运行后生成）")

        # ---- 11. 网络快速检查（3秒超时，可选）----
        try:
            import requests as _rq
            r = _rq.head(
                "https://api.bilibili.com/x/web-interface/nav",
                timeout=3, allow_redirects=True,
            )
            ok(f"网络 B站API 可达 (HTTP {r.status_code})")
        except Exception as e:
            warn(f"网络 B站API 不可达: {type(e).__name__}")

        # ---- 汇总 ----
        self.log_q.put("")
        self.log_q.put("=" * 70)
        if issues:
            self.log_q.put(f"❌ 自检发现 {len(issues)} 个严重问题：")
            for i, x in enumerate(issues, 1):
                self.log_q.put(f"   {i}. {x}")
        if warnings:
            self.log_q.put(f"⚠️  {len(warnings)} 个警告（不影响运行）：")
            for i, x in enumerate(warnings, 1):
                self.log_q.put(f"   {i}. {x}")
        if not issues and not warnings:
            self.log_q.put("✨ 自检全部通过，未发现问题")
        self.log_q.put("=" * 70 + "\n")

    # ============================================================
    # 复审记录
    # ============================================================
    def _on_show_reviewed(self):
        """点击【📋 复审记录】——展示本次运行创建过的蓝图。"""
        if not _CREATED_BLUEPRINTS:
            messagebox.showinfo(
                "复审记录",
                "本次运行还没创建任何蓝图。\n\n"
                "运行一次任务后再点此按钮，可查看所有已创建蓝图的 id 和审核状态。"
            )
            return

        dlg = tk.Toplevel(self.root)
        dlg.title(f"本次运行创建的蓝图（{len(_CREATED_BLUEPRINTS)} 个）")
        dlg.geometry("900x500")
        dlg.minsize(700, 400)
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(
            dlg,
            text=f"共 {len(_CREATED_BLUEPRINTS)} 个蓝图。"
                 f"可对 manual_pending / rejected 状态的蓝图触发复审。",
            font=("", 10, "bold"),
        ).pack(anchor="w", padx=10, pady=10)

        cols = ("code", "id", "status", "source", "video")
        tree = ttk.Treeview(dlg, columns=cols, show="headings", selectmode="extended")
        for c, (txt, w, a) in {
            "code":   ("蓝图码", 260, "w"),
            "id":     ("蓝图 ID", 180, "w"),
            "status": ("审核状态", 100, "center"),
            "source": ("来源", 120, "center"),
            "video":  ("视频 BV", 120, "center"),
        }.items():
            tree.heading(c, text=txt)
            tree.column(c, width=w, anchor=a, stretch=(c == "code"))

        for bp in _CREATED_BLUEPRINTS:
            tree.insert(
                "", "end",
                values=(bp["code"], bp["id"], bp["review_status"],
                        bp["source"], bp.get("video", "")),
            )
        vsb = ttk.Scrollbar(dlg, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        vsb.pack(side="right", fill="y", pady=10, padx=(0, 10))

        bf = ttk.Frame(dlg, padding=10)
        bf.pack(fill="x")

        def retry_selected():
            sel = tree.selection()
            if not sel:
                messagebox.showwarning("提示", "请先选中至少一个蓝图")
                return
            # 拿到选中的蓝图数据
            targets = []
            for iid in sel:
                vals = tree.item(iid, "values")
                targets.append({"id": vals[1], "code": vals[0], "status": vals[2]})

            threading.Thread(
                target=self._retry_review_thread,
                args=(targets,),
                daemon=True,
            ).start()

        ttk.Button(bf, text="关闭", command=dlg.destroy, width=10).pack(side="right", padx=5)
        ttk.Button(
            bf, text="🔄 对选中项触发复审",
            command=retry_selected, width=22,
        ).pack(side="right", padx=5)

    def _retry_review_thread(self, targets):
        """后台批量复审。"""
        if not (self.bili_fetcher or True):
            pass

        # 复用 client_pool 里已登录的 uploader
        # 由于 run_pipeline 里的 clients 是局部的，这里新建一个 uploader
        try:
            api_root = self.var_api.get().strip()
            uploader = BlueprintUploader(api_root)
            acc = self.var_acc.get().strip()
            pwd = self.var_pwd.get()

            self.log_q.put(f"\n🔄 开始对 {len(targets)} 个蓝图触发复审")
            if not uploader.login(acc, pwd):
                self.log_q.put("❌ 复审失败: 蓝图平台登录失败")
                return

            success = 0
            for t in targets:
                bid, code, status = t["id"], t["code"], t["status"]
                if status == "pending":
                    self.log_q.put(f"⏭️  跳过 {code}（当前 pending，禁止重复提审）")
                    continue
                try:
                    res = uploader.retry_review(bid)
                    if res.get("code") == 0:
                        success += 1
                        new_st = (res.get("data") or {}).get("review_status", "?")
                        self.log_q.put(f"✅ {code}: 复审已触发 → {new_st}")
                    else:
                        self.log_q.put(
                            f"❌ {code}: {res.get('message', '失败')}"
                        )
                except Exception as e:
                    self.log_q.put(f"❌ {code}: {type(e).__name__}: {e}")
            self.log_q.put(f"🔄 复审完成: 成功 {success}/{len(targets)}\n")
            uploader.close()
        except Exception as e:
            self.log_q.put(f"❌ 复审异常: {e}\n")
            self.log_q.put(traceback.format_exc())

    # ============================================================
    # 运行流程
    # ============================================================
    def _collect(self):
        modes = [m for m, v in self.mode_vars.items() if v.get()]
        if not modes:
            messagebox.showwarning("提示", "至少勾选一个模式")
            return None
        links = [l.strip() for l in self.text_links.get("1.0", "end").splitlines() if l.strip()]
        mids = []
        for l in self.text_mids.get("1.0", "end").splitlines():
            l = l.strip()
            if not l:
                continue
            try:
                mids.append(int(l))
            except ValueError:
                messagebox.showwarning("提示", f"UP主 mid 非数字: {l}")
                return None
        manual_text = self.text_manual.get("1.0", "end").strip()
        excl = [l.strip() for l in self.text_ex.get("1.0", "end").splitlines() if l.strip()] or [""]
        return {
            "source_modes": modes, "links": links, "manual_text": manual_text,
            "uploader_mids": mids,
            "uploader_max_pages": self.var_ump.get(),
            "uploader_page_size": self.var_ups.get(),
            "uploader_order": self.var_uo.get(),
            "api_root": self.var_api.get().strip(),
            "bp_account": self.var_acc.get().strip(),
            "bp_password": self.var_pwd.get(),
            "fetch_comments": self.var_fc.get(),
            "scan_sub_replies": self.var_ssr.get(),
            "comment_max_pages": self.var_cmp.get(),
            "fetch_doc": self.var_fd.get(),
            "use_selenium_fallback": self.var_sf.get(),
            "interactive_manual": self.var_im.get(),
            "defer_manual": self.var_dm.get(),
            "fetch_cover": self.var_fc_.get(),
            "include_cover_in_upload": self.var_icu.get(),
            "cover_save_dir": self.var_cd.get().strip() or "covers",
            "group_cover_by_uploader": self.var_gc.get(),
            "exclude_codes": excl,
        }

    def _on_run(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "任务运行中")
            return
        if self.bili_logging_in:
            messagebox.showinfo("提示", "B站登录中，请先完成登录")
            return

        cfg = self._collect()
        if cfg is None:
            return

        self.stop_event.clear()
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress.start(10)
        self.status_var.set("运行中...")
        self._append_log("\n" + "=" * 70)
        self._append_log(f"启动: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self._append_log("=" * 70 + "\n")

        self.worker = threading.Thread(
            target=self._run, args=(cfg,), daemon=True
        )
        self.worker.start()

    def _run(self, cfg):
        try:
            s, f = run_pipeline(
                **cfg,
                manual_prompt_callback=self._ask_manual,
                comment_fetcher=self.bili_fetcher,
                stop_event=self.stop_event,
            )
            self.log_q.put(f"\n✅ 完成: 成功 {s} | 失败 {f}\n")
        except Exception as e:
            self.log_q.put(f"\n❌ 出错: {e}\n")
            self.log_q.put(traceback.format_exc())
        finally:
            self.root.after(0, self._on_finished)

    def _on_finished(self):
        self.progress.stop()
        self.btn_run.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.status_var.set("就绪")

    def _on_stop(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.status_var.set("正在中止...")
            self._append_log("\n⚠️  中止请求已发送...\n")

    def _ask_manual(self, url, idx, total):
        self.prompt_result = None
        self.prompt_event.clear()

        def show():
            try:
                self.prompt_result = self._manual_dialog(url, idx, total)
            finally:
                self.prompt_event.set()

        self.root.after(0, show)
        self.prompt_event.wait(timeout=600)
        return self.prompt_result

    def _manual_dialog(self, url, idx, total):
        dlg = tk.Toplevel(self.root)
        dlg.title(f"人工介入 [{idx}/{total}]")
        dlg.geometry("800x600")
        dlg.transient(self.root)
        dlg.grab_set()

        res = {"v": None}

        ttk.Label(
            dlg,
            text=f"文档 [{idx}/{total}] 自动抓取失败，请手动复制内容粘贴到下方：",
            font=("", 10, "bold"),
        ).pack(anchor="w", padx=10, pady=10)

        entry = ttk.Entry(dlg, width=100)
        entry.insert(0, url)
        entry.configure(state="readonly")
        entry.pack(fill="x", padx=10)

        ttk.Label(dlg, text="提示: 打开链接 → 全选复制 → 粘贴到下方",
                  foreground="gray").pack(anchor="w", padx=10, pady=5)

        txt = scrolledtext.ScrolledText(dlg, wrap="word", height=20)
        txt.pack(fill="both", expand=True, padx=10, pady=5)
        txt.focus_set()

        bf = ttk.Frame(dlg, padding=10)
        bf.pack(fill="x")

        def ok():
            res["v"] = txt.get("1.0", "end").strip() or None
            dlg.destroy()

        def skip():
            res["v"] = None
            dlg.destroy()

        def load():
            p = filedialog.askopenfilename(
                filetypes=[("文本", "*.txt"), ("所有", "*.*")])
            if p:
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        c = f.read()
                    txt.delete("1.0", "end")
                    txt.insert("1.0", c)
                except Exception as e:
                    messagebox.showerror("错误", f"读取失败: {e}")

        ttk.Button(bf, text="从文件导入", command=load).pack(side="left", padx=5)
        ttk.Button(bf, text="跳过", command=skip).pack(side="right", padx=5)
        ttk.Button(bf, text="确定", command=ok).pack(side="right", padx=5)

        dlg.wait_window()
        return res["v"]

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("确认", "任务运行中，确认退出？"):
                return
            self.stop_event.set()
        if self.bili_fetcher:
            try:
                self.bili_fetcher.close()
            except Exception:
                pass
        self.root.destroy()


def main():
    root = tk.Tk()
    app = BlueprintGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()