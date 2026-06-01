"""
Google Indexing API — 自动提交两个网站的所有 URL
使用方法: python3 submit_to_google.py
"""

import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────────────────────────
SERVICE_ACCOUNT_FILE = Path(__file__).parent / "service_account.json"

SITEMAPS = [
    "https://insightbridge.global/sitemap.xml",
    "https://intelligence.insightbridge.global/sitemap.xml",
]

INDEXING_ENDPOINT = "https://indexing.googleapis.com/v3/urlNotifications:publish"

# ── 获取访问令牌 ───────────────────────────────────────────────────────────────
def get_access_token():
    import urllib.request, json, time, base64, hashlib, hmac

    creds = json.loads(SERVICE_ACCOUNT_FILE.read_text())

    # 构建 JWT
    import struct

    try:
        import google.oauth2.service_account as sa
        import google.auth.transport.requests as ga_req
        credentials = sa.Credentials.from_service_account_file(
            str(SERVICE_ACCOUNT_FILE),
            scopes=["https://www.googleapis.com/auth/indexing"]
        )
        credentials.refresh(ga_req.Request())
        return credentials.token
    except ImportError:
        pass

    # 手动 JWT（备用，当 google-auth 未安装时）
    import base64, json, time
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.backends import default_backend

        now = int(time.time())
        header = base64.urlsafe_b64encode(json.dumps({"alg":"RS256","typ":"JWT"}).encode()).rstrip(b"=")
        payload = base64.urlsafe_b64encode(json.dumps({
            "iss": creds["client_email"],
            "scope": "https://www.googleapis.com/auth/indexing",
            "aud": creds["token_uri"],
            "exp": now + 3600,
            "iat": now
        }).encode()).rstrip(b"=")

        message = header + b"." + payload
        key = serialization.load_pem_private_key(creds["private_key"].encode(), password=None, backend=default_backend())
        signature = base64.urlsafe_b64encode(key.sign(message, padding.PKCS1v15(), hashes.SHA256())).rstrip(b"=")
        jwt = (message + b"." + signature).decode()

        data = f"grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion={jwt}".encode()
        req = urllib.request.Request(creds["token_uri"], data=data, method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())["access_token"]
    except Exception as e:
        raise RuntimeError(f"无法获取访问令牌，请安装: pip install google-auth\n错误: {e}")


# ── 从 sitemap 提取 URL ────────────────────────────────────────────────────────
def get_urls_from_sitemap(sitemap_url):
    try:
        with urllib.request.urlopen(sitemap_url, timeout=10) as resp:
            tree = ET.parse(resp)
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = [loc.text.strip() for loc in tree.findall(".//s:loc", ns) if loc.text]
        return urls
    except Exception as e:
        print(f"  ⚠️  无法读取 sitemap {sitemap_url}: {e}")
        return []


# ── 提交 URL ──────────────────────────────────────────────────────────────────
def submit_url(url, token):
    data = json.dumps({"url": url, "type": "URL_UPDATED"}).encode()
    req = urllib.request.Request(
        INDEXING_ENDPOINT,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return True, result.get("urlNotificationMetadata", {}).get("latestUpdate", {}).get("notifyTime", "OK")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return False, body


# ── 主程序 ─────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "="*60)
    print("  Google Indexing API — InsightBridge 自动提交")
    print("="*60)

    print("\n正在获取访问令牌...")
    try:
        token = get_access_token()
        print("  ✅ 令牌获取成功")
    except Exception as e:
        print(f"  ❌ {e}")
        return

    total_ok = 0
    total_fail = 0

    for sitemap_url in SITEMAPS:
        print(f"\n📋 读取 sitemap: {sitemap_url}")
        urls = get_urls_from_sitemap(sitemap_url)
        print(f"  找到 {len(urls)} 个 URL")

        for url in urls:
            ok, msg = submit_url(url, token)
            status = "✅" if ok else "❌"
            print(f"  {status} {url}")
            if not ok:
                print(f"     错误: {msg[:120]}")
                total_fail += 1
            else:
                total_ok += 1
            time.sleep(0.5)  # 避免速率限制

    print("\n" + "="*60)
    print(f"  完成: ✅ {total_ok} 成功  ❌ {total_fail} 失败")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
