"""
Google & Bing Sitemap Ping — 每次更新网站后运行此脚本
通知搜索引擎重新抓取 sitemap，无需 OAuth 认证
"""
import urllib.request
import urllib.parse

SITEMAPS = [
    "https://insightbridge.global/sitemap.xml",
    "https://intelligence.insightbridge.global/sitemap.xml",
]

PING_ENDPOINTS = [
    "https://www.google.com/ping?sitemap={}",
    "https://www.bing.com/ping?sitemap={}",
]

def ping(sitemap_url):
    encoded = urllib.parse.quote(sitemap_url, safe="")
    for endpoint in PING_ENDPOINTS:
        url = endpoint.format(encoded)
        engine = "Google" if "google" in url else "Bing"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                status = resp.status
            if status == 200:
                print(f"  ✅ {engine}: {sitemap_url}")
            else:
                print(f"  ⚠️  {engine} 返回 {status}: {sitemap_url}")
        except Exception as e:
            print(f"  ❌ {engine} 失败: {e}")

print("\n" + "="*55)
print("  Sitemap Ping — Google & Bing")
print("="*55)
for s in SITEMAPS:
    ping(s)
print("="*55)
print("  完成！搜索引擎将在数小时内重新抓取。\n")
