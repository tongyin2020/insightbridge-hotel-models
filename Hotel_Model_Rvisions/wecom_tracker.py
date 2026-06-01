"""
企业微信访客追踪系统 — InsightBridge Visitor Intelligence
=========================================================
功能：
1. 通过企微 OAuth2 识别访客身份（谁在看哪篇报告）
2. 实时企微推送通知（"张三正在阅读您的白皮书"）
3. 访客行为日志（SQLite）

前提配置（须先完成，见下方 SETUP.md）：
  在 .env 文件或环境变量中设置：
    WECOM_CORP_ID=ww...          ← 企业微信管理后台 → 我的企业 → 企业ID
    WECOM_TRACKER_AGENT_ID=...   ← 网页应用的 AgentId
    WECOM_TRACKER_SECRET=...     ← 网页应用的 Secret

运行方式：
  pip install fastapi uvicorn aiosqlite httpx
  uvicorn wecom_tracker:app --host 0.0.0.0 --port 8001 --reload

追踪链接格式（发给客户的链接）：
  https://track.insightbridge.global/r/ota-report
  https://track.insightbridge.global/r/hbs-case-study
  https://track.insightbridge.global/r/intelligence-vol01
  （全部 slug 见下方 PAGES 字典）
"""

from __future__ import annotations
import os
import asyncio
import sqlite3
import threading
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, quote

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from dotenv import load_dotenv

# ── 环境变量 ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env", override=True)

CORP_ID       = os.getenv("WECOM_CORP_ID", "")          # 企业ID，ww开头
AGENT_ID      = os.getenv("WECOM_TRACKER_AGENT_ID", "") # 网页应用 AgentId
AGENT_SECRET  = os.getenv("WECOM_TRACKER_SECRET", "")   # 网页应用 Secret

# 追踪服务自己的公网域名（须在企微网页应用中配置为可信域名）
TRACKER_BASE  = os.getenv("TRACKER_BASE_URL", "https://track.insightbridge.global")
CALLBACK_URL  = f"{TRACKER_BASE}/oauth2/callback"

# 企微 API 基础地址
WECOM_API     = "https://qyapi.weixin.qq.com/cgi-bin"

# ── 内容页面映射 ────────────────────────────────────────────────────────────
PAGES = {
    # slug → (目标URL, 友好标题)
    "ota-report":            ("https://insightbridge.global/media/IB_OTA_Booking_Cost_SEAsia_HN.pdf",
                              "东南亚OTA预订成本分析"),
    "ai-reckoning":          ("https://insightbridge.global/media/HTR_Whitepaper_InsightBridge_AI_Reckoning.pdf",
                              "AI清算：酒店业拐点白皮书"),
    "ai-theatre":            ("https://insightbridge.global/media/IB_AI_Theatre_TravelTech_PW.pdf",
                              "AI剧场：旅游科技洞察"),
    "strategic-verticalism": ("https://insightbridge.global/media/IB_Strategic_Verticalism_HN.pdf",
                              "战略垂直主义"),
    "vision2030-mare":       ("https://insightbridge.global/media/IB_Vision2030_MARE_HN.pdf",
                              "Vision 2030：MARE框架"),
    "vision2030-revenue":    ("https://insightbridge.global/media/IB_Vision2030_Revenue_Management.pdf",
                              "Vision 2030：收益管理"),
    "hbs-case-study":        ("https://insightbridge.global/publications/HBS_Case_Study_PUBLICATION_GRADE.pdf",
                              "HBS案例研究"),
    "hbs-case-cn":           ("https://insightbridge.global/publications/HBS_Case_Study_CHINESE.pdf",
                              "HBS案例研究（中文版）"),
    "core-code-theory":      ("https://insightbridge.global/publications/Core_Code_Theory_AMR.pdf",
                              "核心代码理论（AMR）"),
    "dpr-plc":               ("https://insightbridge.global/publications/DPR_PLC_Neural_Financial_Model.pdf",
                              "神经网络财务模型"),
    "mba-crisis":            ("https://insightbridge.global/publications/MBA_Crisis_Performance_UI_vs_Core_Code.pdf",
                              "MBA危机绩效研究"),
    "intelligence-vol01":    ("https://insightbridge.global/intelligence-vol01.html",
                              "Intelligence Vol.01：定价革命"),
    "market-report":         ("https://insightbridge.global/intelligence-market-report.html",
                              "全球市场情报报告"),
    "home":                  ("https://insightbridge.global/",
                              "InsightBridge Global 官网"),
}

# ── SQLite 访客日志 ──────────────────────────────────────────────────────────
DB_PATH = BASE_DIR / "visitor_log.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS visits (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT    NOT NULL,
            slug       TEXT    NOT NULL,
            page_title TEXT    NOT NULL,
            userid     TEXT,
            name       TEXT,
            department TEXT,
            position   TEXT,
            mobile     TEXT,
            ip         TEXT,
            user_agent TEXT
        )
    """)
    conn.commit()
    conn.close()

def log_visit(slug: str, page_title: str, userid: str, name: str,
              department: str, position: str, mobile: str, ip: str, ua: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO visits (ts, slug, page_title, userid, name, department, position, mobile, ip, user_agent)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (datetime.now().isoformat(), slug, page_title, userid, name,
          department, position, mobile, ip, ua))
    conn.commit()
    conn.close()

# ── 企微 API 工具 ────────────────────────────────────────────────────────────
_access_token_cache: dict = {"token": "", "expires_at": 0}

async def get_access_token() -> str:
    """获取企微 access_token（带缓存）"""
    now = time.time()
    if _access_token_cache["token"] and now < _access_token_cache["expires_at"] - 60:
        return _access_token_cache["token"]
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{WECOM_API}/gettoken",
            params={"corpid": CORP_ID, "corpsecret": AGENT_SECRET},
            timeout=10
        )
        data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"获取token失败: {data.get('errmsg')}")
    _access_token_cache["token"]      = data["access_token"]
    _access_token_cache["expires_at"] = now + data.get("expires_in", 7200)
    return _access_token_cache["token"]

async def get_user_by_code(code: str) -> dict:
    """用 OAuth2 code 换取用户信息"""
    token = await get_access_token()
    # Step1: code → userid
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{WECOM_API}/auth/getuserinfo",
            params={"access_token": token, "code": code},
            timeout=10
        )
        data = r.json()
    if data.get("errcode") != 0:
        return {"userid": "unknown", "name": "外部访客", "department": "", "position": "", "mobile": ""}
    userid = data.get("UserId") or data.get("OpenId", "外部访客")
    if not data.get("UserId"):
        # 外部联系人，只有 OpenId
        return {"userid": userid, "name": "外部访客", "department": "", "position": "", "mobile": ""}
    # Step2: userid → 详细信息
    async with httpx.AsyncClient() as client:
        r2 = await client.get(
            f"{WECOM_API}/user/get",
            params={"access_token": token, "userid": userid},
            timeout=10
        )
        udata = r2.json()
    name       = udata.get("name", userid)
    department = str(udata.get("department", ""))
    position   = udata.get("position", "")
    mobile     = udata.get("mobile", "")
    return {"userid": userid, "name": name, "department": department,
            "position": position, "mobile": mobile}

# ── WeCom 推送（复用 wecom_push.py 逻辑）──────────────────────────────────────
def _push_visit_notification(name: str, position: str, page_title: str, slug: str):
    """非阻塞推送访客通知"""
    def _run():
        try:
            import sys
            sys.path.insert(0, str(BASE_DIR))
            from wecom_push import push_markdown
            ts = datetime.now().strftime("%H:%M")
            dept_str = f" · {position}" if position else ""
            content = (
                f"## 👁️ 访客提醒\n"
                f"**{name}**{dept_str} 正在阅读\n\n"
                f"> 📄 **{page_title}**\n\n"
                f"🕐 {ts}　　🔗 [追踪链接](https://track.insightbridge.global/r/{slug})"
            )
            push_markdown(content)
        except Exception as e:
            print(f"⚠️  推送失败: {e}")
    threading.Thread(target=_run, daemon=True).start()

# ── FastAPI 应用 ─────────────────────────────────────────────────────────────
app = FastAPI(title="InsightBridge Visitor Tracker", docs_url=None, redoc_url=None)
init_db()

# ── 路由 1：追踪入口 → 跳转 WeCom OAuth2 ─────────────────────────────────────
@app.get("/r/{slug}")
async def track_redirect(slug: str, request: Request):
    """
    对外分享的追踪链接入口。
    当访客在企微里点击此链接时，会先走 OAuth2 识别身份。
    """
    if slug not in PAGES:
        raise HTTPException(status_code=404, detail="页面不存在")

    if not CORP_ID or not AGENT_ID:
        # 未配置企微应用，直接跳转（降级模式）
        dest_url, _ = PAGES[slug]
        return RedirectResponse(dest_url)

    # 构造 OAuth2 授权 URL
    # scope=snsapi_base：静默授权（内部成员）
    # scope=snsapi_privateinfo：可获取手机号等（需用户同意，且须使用自建应用）
    oauth_url = (
        "https://open.weixin.qq.com/connect/oauth2/authorize?"
        + urlencode({
            "appid":         CORP_ID,
            "redirect_uri":  f"{CALLBACK_URL}?slug={slug}",
            "response_type": "code",
            "scope":         "snsapi_base",
            "state":         slug,
            "agentid":       AGENT_ID,
        })
        + "#wechat_redirect"
    )
    return RedirectResponse(oauth_url)

# ── 路由 2：OAuth2 回调 ──────────────────────────────────────────────────────
@app.get("/oauth2/callback")
async def oauth2_callback(request: Request, code: str = "", slug: str = "", state: str = ""):
    """
    企微 OAuth2 回调。识别用户身份，记录日志，发推送，然后跳转真实页面。
    """
    effective_slug = slug or state or "home"
    dest_url, page_title = PAGES.get(effective_slug, ("https://insightbridge.global/", "官网"))

    if not code:
        return RedirectResponse(dest_url)

    # 获取用户信息
    try:
        user = await get_user_by_code(code)
    except Exception as e:
        print(f"⚠️  获取用户信息失败: {e}")
        user = {"userid": "unknown", "name": "访客", "department": "", "position": "", "mobile": ""}

    # 记录日志
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    log_visit(
        slug=effective_slug,
        page_title=page_title,
        userid=user["userid"],
        name=user["name"],
        department=user["department"],
        position=user["position"],
        mobile=user["mobile"],
        ip=ip, ua=ua
    )
    print(f"✅ [{datetime.now().strftime('%H:%M:%S')}] {user['name']} ({user['userid']}) → {page_title}")

    # 发推送通知（非阻塞）
    if user["userid"] not in ("unknown", "外部访客"):
        _push_visit_notification(
            name=user["name"],
            position=user.get("position", ""),
            page_title=page_title,
            slug=effective_slug
        )

    # 跳转真实页面
    return RedirectResponse(dest_url)

# ── 路由 3：访客日志查看（内部用）─────────────────────────────────────────────
@app.get("/admin/visits", response_class=HTMLResponse)
async def admin_visits(limit: int = 50):
    """查看最近访客记录（访问 /admin/visits）"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT ts, name, position, page_title, userid, ip FROM visits ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()

    rows_html = "".join(
        f"<tr><td>{r[0][:16]}</td><td><b>{r[1]}</b></td><td>{r[2]}</td>"
        f"<td>{r[3]}</td><td style='color:#888;font-size:12px'>{r[4]}</td><td>{r[5]}</td></tr>"
        for r in rows
    )
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
    <title>访客日志 — InsightBridge</title>
    <style>body{{font-family:sans-serif;padding:24px;background:#fafaf8}}
    table{{border-collapse:collapse;width:100%}}
    th,td{{border:1px solid #ddd;padding:8px 12px;text-align:left}}
    th{{background:#1a3a5c;color:#fff}}tr:nth-child(even){{background:#f5f3ef}}</style></head>
    <body><h2>👁️ InsightBridge 访客日志</h2>
    <p style="color:#888">最近 {limit} 条记录</p>
    <table><tr><th>时间</th><th>姓名</th><th>职位</th><th>阅读内容</th><th>UserID</th><th>IP</th></tr>
    {rows_html}</table></body></html>"""

# ── 路由 4：追踪链接列表（内部用）────────────────────────────────────────────
@app.get("/admin/links", response_class=HTMLResponse)
async def admin_links():
    """查看所有可用追踪链接"""
    rows_html = "".join(
        f"<tr><td><code>{slug}</code></td><td>{title}</td>"
        f"<td><a href='{TRACKER_BASE}/r/{slug}' target='_blank'>{TRACKER_BASE}/r/{slug}</a></td></tr>"
        for slug, (_, title) in PAGES.items()
    )
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
    <title>追踪链接 — InsightBridge</title>
    <style>body{{font-family:sans-serif;padding:24px;background:#fafaf8}}
    table{{border-collapse:collapse;width:100%}}
    th,td{{border:1px solid #ddd;padding:8px 12px}}
    th{{background:#1a3a5c;color:#fff}}tr:nth-child(even){{background:#f5f3ef}}
    code{{background:#eee;padding:2px 6px;border-radius:3px}}</style></head>
    <body><h2>🔗 InsightBridge 追踪链接</h2>
    <p>将下方链接发给客户，当对方在企微中打开时即可识别身份</p>
    <table><tr><th>Slug</th><th>内容标题</th><th>追踪链接（复制发送）</th></tr>
    {rows_html}</table></body></html>"""

# ── 健康检查 ─────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "corp_configured": bool(CORP_ID and AGENT_ID)}
