# -*- coding: utf-8 -*-
"""
探测蓝图平台的图片上传接口路径
运行: python probe_upload.py
"""

import os
import requests
import json

API_ROOT = "https://end-api.shallow.ink"
ACCOUNT = "zhhyylove8@163.com"
PASSWORD = "Yu.7198193."

# 构造一个小测试图片
TEST_IMG = "_probe_test.png"
try:
    from PIL import Image
    Image.new("RGB", (10, 10), color="red").save(TEST_IMG)
    print(f"[准备] 已生成测试图 {TEST_IMG}")
except ImportError:
    # 无 Pillow 时手写 1x1 红色 PNG
    import base64
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
        "nGP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    with open(TEST_IMG, "wb") as f:
        f.write(base64.b64decode(png_b64))
    print(f"[准备] 已生成测试图 {TEST_IMG} (1x1 base64)")


def login():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
    })
    r = s.post(
        f"{API_ROOT}/api/v1/auth/login",
        json={"account": ACCOUNT, "password": PASSWORD},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"[失败] 登录: {r.status_code} {r.text[:200]}")
        return None, None
    data = r.json()
    token = data["data"]["access_token"]
    print(f"[登录成功] token: {token[:30]}...")
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s, token


def try_endpoint(session, path, method="POST", field_name="file"):
    """尝试一个上传端点。返回 (成功, 状态码, 响应片段)"""
    url = f"{API_ROOT}{path}"
    try:
        with open(TEST_IMG, "rb") as f:
            files = {field_name: (os.path.basename(TEST_IMG), f, "image/png")}
            headers = {
                k: v for k, v in session.headers.items()
                if k.lower() != "content-type"
            }
            r = session.request(
                method, url, files=files, headers=headers, timeout=15,
            )
        # 404 = 端点不存在；其它情况表示端点存在
        return (r.status_code != 404, r.status_code, r.text[:200])
    except Exception as e:
        return (False, -1, str(e))


def main():
    session, _ = login()
    if session is None:
        return

    # 常见路径候选（覆盖 RESTful / 复数 / 复数+upload 等风格）
    candidates = [
        # 与登录接口同风格
        ("/api/v1/upload", "POST"),
        ("/api/v1/uploads", "POST"),
        ("/api/v1/upload/image", "POST"),
        ("/api/v1/upload/images", "POST"),
        ("/api/v1/upload/file", "POST"),
        ("/api/v1/upload/files", "POST"),

        # 资源命名
        ("/api/v1/files", "POST"),
        ("/api/v1/files/upload", "POST"),
        ("/api/v1/images", "POST"),
        ("/api/v1/images/upload", "POST"),
        ("/api/v1/assets", "POST"),
        ("/api/v1/assets/upload", "POST"),
        ("/api/v1/media", "POST"),
        ("/api/v1/media/upload", "POST"),
        ("/api/v1/attachments", "POST"),
        ("/api/v1/attachments/upload", "POST"),

        # 与业务相关
        ("/api/v1/blueprint/upload", "POST"),
        ("/api/v1/blueprints/upload", "POST"),
        ("/api/v1/blueprints/upload/image", "POST"),
        ("/api/v1/blueprints/images", "POST"),

        # 无 v1 前缀
        ("/api/upload", "POST"),
        ("/api/upload/image", "POST"),
        ("/upload", "POST"),
        ("/upload/image", "POST"),

        # 可能的第三方云存储代理
        ("/api/v1/oss/upload", "POST"),
        ("/api/v1/oss/policy", "GET"),
        ("/api/v1/upload/policy", "GET"),
        ("/api/v1/upload/token", "GET"),
    ]

    print()
    print("=" * 70)
    print("探测上传接口（404 = 不存在）")
    print("=" * 70)

    found = []
    for path, method in candidates:
        ok, status, body = try_endpoint(session, path, method)
        if ok:
            # 端点存在，再打印详细信息
            mark = "✅" if status in (200, 201) else "⚠️ "
            print(f"{mark} [{status}] {method} {path}")
            print(f"      → {body}")
            found.append((path, method, status, body))
        else:
            # 静默跳过 404
            if status != 404:
                print(f"❓ [{status}] {method} {path}: {body}")

    print()
    print("=" * 70)
    print(f"结果: 找到 {len(found)} 个候选端点")
    print("=" * 70)

    if not found:
        print("❌ 所有常见路径都没命中。请:")
        print("   1. 打开蓝图平台网站，按 F12 打开开发者工具")
        print("   2. 切换到 Network 标签")
        print("   3. 在平台上手动上传一张图片")
        print("   4. 找到那个上传请求，看它的 URL 和表单字段名")
        print("   5. 把 URL 路径和字段名告诉我")
    else:
        print("推荐使用第一个返回 200 的端点。")
        print()
        print("如需要，请在主程序 bili_gui.py 中修改:")
        print("  class ImageUploader:")
        print(f"      UPLOAD_ENDPOINT = \"{found[0][0]}\"")

    # 清理
    try:
        os.remove(TEST_IMG)
    except OSError:
        pass


if __name__ == "__main__":
    main()