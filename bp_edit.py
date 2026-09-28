# -*- coding: utf-8 -*-
"""
蓝图平台 API 客户端（独立模块）

复用的部分（token）:
- POST /api/v1/auth/login     登录拿 access_token + refresh_token
- POST /api/v1/auth/refresh   刷新 token
- 401 自动刷新重试

本模块独立封装:
- POST   /api/blueprints                       创建蓝图
- PUT    /api/blueprints/:id                   编辑蓝图
- DELETE /api/blueprints/:id                   删除蓝图
- POST   /api/blueprints/:id/review            触发复审
- POST   /api/blueprints/upload/image          上传图片
- GET    /api/blueprints/image/sign            获取签名 URL
"""

import os
import re
import json
import logging
import threading
from typing import Optional, Dict, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


logger = logging.getLogger("blueprint_api")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)


# ============================================================
# 异常
# ============================================================
class BlueprintAPIError(Exception):
    """API 调用异常。"""

    def __init__(self, message, code=None, status_code=None, response=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.response = response

    def __str__(self):
        parts = [self.message]
        if self.code is not None:
            parts.append(f"(code={self.code})")
        if self.status_code is not None:
            parts.append(f"[HTTP {self.status_code}]")
        return " ".join(parts)


# ============================================================
# 主客户端
# ============================================================
class BlueprintAPIClient:
    """
    蓝图平台 API 客户端。

    用法:
        client = BlueprintAPIClient("https://end-api.shallow.ink")
        client.login("user@example.com", "password")

        img = client.upload_image("./cover.jpg")     # {"key": "...", "url": "..."}
        bp = client.create_blueprint({
            "title": "高效铁制零件产线",
            "code": "EF01xxxx",
            "cover_image": img["url"],
            "images": [img["url"]],
            "base_region": "武陵",
            "server_region": "cn",
            "status": "published",
        })
        print(bp.get("id"))

        client.update_blueprint(bp["id"], {"title": "新标题"})
        client.delete_blueprint(bp["id"])
        client.close()
    """

    # 认证端点
    LOGIN_ENDPOINT = "/api/v1/auth/login"
    REFRESH_ENDPOINT = "/api/v1/auth/refresh"

    # 业务端点
    UPLOAD_IMAGE_ENDPOINT = "/api/blueprints/upload/image"
    SIGN_IMAGE_ENDPOINT = "/api/blueprints/image/sign"
    BLUEPRINTS_ENDPOINT = "/api/blueprints"

    # 上传限制
    MAX_IMAGE_SIZE = 10 * 1024 * 1024
    ALLOWED_MAGIC_MIMES = ("image/jpeg", "image/png", "image/webp")

    BASE_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }

    def __init__(
        self,
        base_url: str = "https://end-api.shallow.ink",
        timeout: int = 30,
        retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        self.access_token: str = ""
        self.refresh_token: str = ""

        self.session = requests.Session()
        self.session.headers.update(self.BASE_HEADERS)
        # 默认走 JSON；multipart 上传时会临时弹出
        self.session.headers.update({"Content-Type": "application/json"})

        retry = Retry(
            total=retries,
            connect=retries, read=retries, status=retries,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "HEAD"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self._auth_lock = threading.Lock()

    # ============================================================
    # 认证
    # ============================================================
    def login(self, account: str, password: str) -> bool:
        """登录获取 token。失败返回 False 并打印原因。"""
        try:
            r = self.session.post(
                f"{self.base_url}{self.LOGIN_ENDPOINT}",
                json={"account": account, "password": password},
                timeout=self.timeout,
            )
            if r.status_code != 200:
                logger.error(f"登录失败 HTTP {r.status_code}: {r.text[:200]}")
                return False

            data = r.json()
            if data.get("code") not in (0, None):
                logger.error(f"登录失败: {data.get('message') or data}")
                return False

            payload = data.get("data") or {}
            self.access_token = payload.get("access_token", "") or ""
            self.refresh_token = payload.get("refresh_token", "") or ""
            self._apply_auth_header()
            logger.info("登录成功")
            return bool(self.access_token)

        except Exception as e:
            logger.error(f"登录异常: {type(e).__name__}: {e}")
            return False

    def set_token(self, access_token: str, refresh_token: str = ""):
        """手动注入 token（比如从配置文件读取）。"""
        self.access_token = access_token or ""
        if refresh_token:
            self.refresh_token = refresh_token or ""
        self._apply_auth_header()

    def logout(self):
        """清空 token。"""
        self.access_token = ""
        self.refresh_token = ""
        self.session.headers.pop("Authorization", None)

    def is_logged_in(self) -> bool:
        return bool(self.access_token)

    def _apply_auth_header(self):
        if self.access_token:
            self.session.headers["Authorization"] = f"Bearer {self.access_token}"
        else:
            self.session.headers.pop("Authorization", None)

    def _refresh_access_token(self) -> bool:
        """401 时尝试刷新。加锁防止并发重复刷新。"""
        with self._auth_lock:
            if not self.refresh_token:
                return False
            try:
                r = requests.post(
                    f"{self.base_url}{self.REFRESH_ENDPOINT}",
                    json={"refresh_token": self.refresh_token},
                    timeout=self.timeout,
                )
                if r.status_code != 200:
                    return False
                data = r.json()
                if data.get("code") not in (0, None):
                    return False
                new_token = (data.get("data") or {}).get("access_token", "")
                if not new_token:
                    return False
                self.access_token = new_token
                self._apply_auth_header()
                logger.info("Token 已刷新")
                return True
            except Exception as e:
                logger.warning(f"刷新 token 异常: {e}")
                return False

    # ============================================================
    # 统一请求封装
    # ============================================================
    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        raise_on_error: bool = True,
    ) -> Dict[str, Any]:
        """
        统一请求入口。
        - 401 时自动刷新 token 重试一次
        - multipart 上传时临时弹出 Content-Type，让 requests 自动生成 boundary
        """
        url = f"{self.base_url}{path}"

        saved_ct = None
        if files is not None:
            saved_ct = self.session.headers.pop("Content-Type", None)

        try:
            r = self.session.request(
                method, url,
                json=json_body,
                params=params,
                files=files,
                data=data,
                timeout=self.timeout,
            )
        finally:
            if files is not None and saved_ct is not None:
                self.session.headers["Content-Type"] = saved_ct

        # 401 自动刷新重试
        if r.status_code == 401 and self._refresh_access_token():
            if files is not None:
                # 文件流已被消费，无法自动重试
                raise BlueprintAPIError(
                    "401 需要重新登录（multipart 请求无法自动重试）",
                    status_code=401, response=r,
                )
            r = self.session.request(
                method, url,
                json=json_body, params=params,
                timeout=self.timeout,
            )

        try:
            result = r.json()
        except Exception:
            result = {"_raw": r.text}

        if raise_on_error:
            if r.status_code >= 400:
                raise BlueprintAPIError(
                    result.get("message") or f"HTTP {r.status_code}",
                    code=result.get("code"),
                    status_code=r.status_code,
                    response=r,
                )
            if isinstance(result, dict) and result.get("code") not in (0, None):
                raise BlueprintAPIError(
                    result.get("message") or "业务错误",
                    code=result.get("code"),
                    status_code=r.status_code,
                    response=r,
                )

        return result

    # ============================================================
    # 蓝图 CRUD
    # ============================================================
    def create_blueprint(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        创建蓝图。
        POST /api/blueprints

        :param payload: 见文档字段表。必填: title / code / base_region / server_region
        :return: 后端返回的 data 部分（通常含 id / code 等）
        """
        self._validate_create_payload(payload)
        result = self._request(
            "POST", self.BLUEPRINTS_ENDPOINT, json_body=payload
        )
        return result.get("data") or result

    def update_blueprint(
        self, blueprint_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """编辑蓝图（仅创建者）。字段全部可选，只传要改的。"""
        if not blueprint_id:
            raise BlueprintAPIError("blueprint_id 不能为空")
        result = self._request(
            "PUT",
            f"{self.BLUEPRINTS_ENDPOINT}/{blueprint_id}",
            json_body=payload,
        )
        return result.get("data") or result

    def delete_blueprint(self, blueprint_id: str) -> Dict[str, Any]:
        """删除蓝图（仅创建者）。后端会异步清理点赞/评论/收藏/浏览/复制记录。"""
        if not blueprint_id:
            raise BlueprintAPIError("blueprint_id 不能为空")
        result = self._request(
            "DELETE", f"{self.BLUEPRINTS_ENDPOINT}/{blueprint_id}"
        )
        return result.get("data") or result

    def get_blueprint(self, blueprint_id: str) -> Dict[str, Any]:
        """获取蓝图详情（文档未单列，但通常是 GET /:id）。"""
        if not blueprint_id:
            raise BlueprintAPIError("blueprint_id 不能为空")
        result = self._request(
            "GET", f"{self.BLUEPRINTS_ENDPOINT}/{blueprint_id}"
        )
        return result.get("data") or result

    def retry_review(
        self, blueprint_id: str, cap_token: str = ""
    ) -> Dict[str, Any]:
        """
        触发蓝图复审。
        POST /api/blueprints/:id/review

        允许的场景:
        - manual_pending  → 允许
        - rejected        → 允许
        - pending         → 拒绝（后端返回错误）
        其他状态           → 拒绝

        :param cap_token: 启用 Cap 时需要，通常留空
        :return: {"message": "...", "review_status": "..."}
        """
        if not blueprint_id:
            raise BlueprintAPIError("blueprint_id 不能为空")
        body: Dict[str, Any] = {}
        if cap_token:
            body["cap_token"] = cap_token
        result = self._request(
            "POST",
            f"{self.BLUEPRINTS_ENDPOINT}/{blueprint_id}/review",
            json_body=body,
        )
        return result.get("data") or result

    # ============================================================
    # 图片上传 / 签名
    # ============================================================
    def upload_image(self, local_path: str) -> Dict[str, str]:
        """
        上传本地图片。
        POST /api/blueprints/upload/image   (multipart/form-data, 字段名 file)

        :return: {"key": "...", "url": "..."}
        :raises BlueprintAPIError
        """
        if not local_path or not os.path.exists(local_path):
            raise BlueprintAPIError(f"文件不存在: {local_path}")

        size = os.path.getsize(local_path)
        if size <= 0:
            raise BlueprintAPIError("文件为空")
        if size > self.MAX_IMAGE_SIZE:
            raise BlueprintAPIError(
                f"文件超过 {self.MAX_IMAGE_SIZE // (1024 * 1024)}MB 限制"
            )

        with open(local_path, "rb") as f:
            magic = f.read(16)
        mime = self._detect_mime(magic)
        if mime not in self.ALLOWED_MAGIC_MIMES:
            raise BlueprintAPIError(
                f"图片格式不支持（检测为 {mime}），仅允许 jpg/png/webp"
            )

        with open(local_path, "rb") as f:
            files = {
                "file": (os.path.basename(local_path), f, mime)
            }
            result = self._request(
                "POST", self.UPLOAD_IMAGE_ENDPOINT, files=files
            )

        data = result.get("data") or {}
        key = data.get("key", "") or ""
        url = data.get("url", "") or ""
        if not key and not url:
            raise BlueprintAPIError(
                f"上传响应缺少 key/url: "
                f"{json.dumps(result, ensure_ascii=False)[:200]}"
            )
        logger.info(f"图片上传成功: {os.path.basename(local_path)} -> key={key}")
        return {"key": key, "url": url}

    def sign_image(self, key_or_url: str) -> str:
        """
        为私有空间的图片生成签名临时 URL。
        GET /api/blueprints/image/sign?key=...

        :param key_or_url: object key（如 blueprints/2026/02/xxx.jpg）
                           或已经签名的 URL（会自动提取 key）
        :return: 新的签名 URL
        """
        if not key_or_url:
            raise BlueprintAPIError("key 不能为空")

        key = key_or_url
        # 兼容 url 输入
        if key.startswith(("http://", "https://")):
            m = re.search(r'/(blueprints/[^?]+)', key)
            if m:
                key = m.group(1)

        result = self._request(
            "GET", self.SIGN_IMAGE_ENDPOINT, params={"key": key}
        )
        url = (result.get("data") or {}).get("url", "") or ""
        if not url:
            raise BlueprintAPIError("签名响应缺少 url")
        return url

    @staticmethod
    def _detect_mime(data: bytes) -> str:
        """魔数识别（后端也是按魔数严格校验的）。"""
        if data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
            return "image/webp"
        return "application/octet-stream"

    # ============================================================
    # 本地校验
    # ============================================================
    @staticmethod
    def _validate_create_payload(payload: Dict[str, Any]):
        """创建前做基础校验，避免明显不合规的请求打到后端。"""
        required = ["title", "code", "base_region", "server_region"]
        missing = [k for k in required if not payload.get(k)]
        if missing:
            raise BlueprintAPIError(f"缺少必填字段: {missing}")

        title = str(payload.get("title", ""))
        if not (2 <= len(title) <= 100):
            raise BlueprintAPIError(f"title 长度必须 2-100 字，当前 {len(title)}")

        code = str(payload.get("code", ""))
        if not re.fullmatch(r"[A-Za-z0-9]+", code):
            raise BlueprintAPIError("code 只能包含字母和数字")
        if len(code) > 2000:
            raise BlueprintAPIError("code 长度不能超过 2000")

        region = payload.get("base_region", "")
        if region not in ("通用", "四号谷地", "武陵"):
            raise BlueprintAPIError(
                f"base_region 必须是 通用/四号谷地/武陵 之一，当前: {region}"
            )

        srv = payload.get("server_region", "")
        if srv not in ("cn", "global"):
            raise BlueprintAPIError(
                f"server_region 必须是 cn/global 之一，当前: {srv}"
            )

        status = payload.get("status", "draft")
        if status not in ("draft", "published"):
            raise BlueprintAPIError(
                f"status 必须是 draft/published 之一，当前: {status}"
            )

        desc = payload.get("description") or ""
        if len(str(desc)) > 5000:
            raise BlueprintAPIError("description 最多 5000 字")

        imgs = payload.get("images") or []
        if len(imgs) > 9:
            raise BlueprintAPIError("images 最多 9 张")

    # ============================================================
    # 资源释放
    # ============================================================
    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()