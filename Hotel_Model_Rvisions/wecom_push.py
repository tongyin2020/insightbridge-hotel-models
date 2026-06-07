"""
企业微信机器人主动推送工具
============================
用法：
  python3 wecom_push.py "消息内容"          → 推送给所有已注册会话
  echo "消息内容" | python3 wecom_push.py  → 从 stdin 读取后推送
  python3 wecom_push.py --listen             → 监听模式，收到消息自动记录 chatid/userid

集成示例：
  from wecom_push import push_markdown
  push_markdown("## 日报\n内容...")
"""

from __future__ import annotations
import asyncio
import json
import os
import sys
import time
import uuid
import logging
from pathlib import Path

try:
    import websockets
except ImportError:
    print("请先安装: pip3 install websockets")
    sys.exit(1)

# ── 配置 ───────────────────────────────────────────────────────────────────
WS_URL    = "wss://openws.work.weixin.qq.com"
BOT_ID    = "aib-HmJZMjgSNiK9Li9d6dRg9hP4Mh8Y_-Q"
SECRET    = "ybNhjAFOawLKXdh5l46nLQWgdSExa8TvR7338MIIOia"
HEARTBEAT = 30        # 秒
CHATS_FILE = Path(__file__).parent / "wecom_chats.json"  # 记录已知 chatid/userid

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wecom_push")

# ── 已知会话管理 ────────────────────────────────────────────────────────────
def load_chats() -> list[dict]:
    """加载已知会话列表 [{chatid, chat_type, label}]"""
    if CHATS_FILE.exists():
        return json.loads(CHATS_FILE.read_text())
    return []

def save_chat(chatid: str, chat_type: str, userid: str = "", label: str = ""):
    chats = load_chats()
    existing = {c["chatid"] for c in chats}
    if chatid not in existing:
        chats.append({"chatid": chatid, "chat_type": chat_type,
                       "userid": userid, "label": label or chatid})
        CHATS_FILE.write_text(json.dumps(chats, ensure_ascii=False, indent=2))
        log.info(f"新会话已记录: {label or chatid} ({chat_type})")

# ── WebSocket 核心 ──────────────────────────────────────────────────────────
async def _subscribe(ws):
    req_id = str(uuid.uuid4())[:8]
    await ws.send(json.dumps({
        "cmd": "aibot_subscribe",
        "headers": {"req_id": req_id},
        "body": {"bot_id": BOT_ID, "secret": SECRET}
    }))
    resp = json.loads(await ws.recv())
    if resp.get("errcode") != 0:
        raise RuntimeError(f"订阅失败: {resp.get('errmsg')}")
    log.info("✅ 企业微信机器人连接成功")

async def _heartbeat(ws):
    while True:
        await asyncio.sleep(HEARTBEAT)
        req_id = str(uuid.uuid4())[:8]
        try:
            await ws.send(json.dumps({"cmd": "ping", "headers": {"req_id": req_id}}))
        except Exception:
            break

async def _send_markdown(ws, chatid: str, chat_type: int, content: str):
    req_id = str(uuid.uuid4())[:8]
    await ws.send(json.dumps({
        "cmd": "aibot_send_msg",
        "headers": {"req_id": req_id},
        "body": {
            "chatid": chatid,
            "chat_type": chat_type,
            "msgtype": "markdown",
            "markdown": {"content": content}
        }
    }))
    resp = json.loads(await ws.recv())
    if resp.get("errcode") == 0:
        log.info(f"✅ 消息发送成功 → {chatid}")
    else:
        log.warning(f"⚠️  发送失败 {chatid}: {resp.get('errmsg')}")

# ── 监听模式：自动记录所有发消息的用户/群 ──────────────────────────────────
async def _listen_mode():
    log.info("监听模式启动 — 向机器人发消息以自动注册会话 ID")
    log.info("按 Ctrl+C 停止")
    async with websockets.connect(WS_URL) as ws:
        await _subscribe(ws)
        hb_task = asyncio.create_task(_heartbeat(ws))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                cmd = msg.get("cmd", "")
                body = msg.get("body", {})

                if cmd == "aibot_msg_callback":
                    chat_type_str = body.get("chattype", "single")
                    chat_type_int = 2 if chat_type_str == "group" else 1
                    chatid = body.get("chatid") or body.get("from", {}).get("userid", "")
                    userid = body.get("from", {}).get("userid", "")
                    save_chat(chatid, chat_type_str, userid)
                    text = body.get("text", {}).get("content", "")
                    log.info(f"收到消息 [{chat_type_str}] {chatid}: {text[:50]}")

                elif cmd == "aibot_event_callback":
                    etype = body.get("event", {}).get("eventtype", "")
                    if etype == "enter_chat":
                        userid = body.get("from", {}).get("userid", "")
                        save_chat(userid, "single", userid, f"用户_{userid[:8]}")
        finally:
            hb_task.cancel()

# ── 推送模式 ───────────────────────────────────────────────────────────────
async def _push_to_all(content: str):
    chats = load_chats()
    if not chats:
        log.warning("⚠️  没有已知会话。请先运行监听模式并向机器人发送一条消息。")
        log.warning("    python3 wecom_push.py --listen")
        return

    async with websockets.connect(WS_URL) as ws:
        await _subscribe(ws)
        for chat in chats:
            chat_type_int = 2 if chat["chat_type"] == "group" else 1
            await _send_markdown(ws, chat["chatid"], chat_type_int, content)
            await asyncio.sleep(0.5)

# ── 公共接口（供其他脚本 import 使用）────────────────────────────────────────
def push_markdown(content: str):
    """同步包装器，供 import 调用"""
    asyncio.run(_push_to_all(content))

def push_to_chat(chatid: str, chat_type: int, content: str):
    """推送到指定会话"""
    async def _run():
        async with websockets.connect(WS_URL) as ws:
            await _subscribe(ws)
            await _send_markdown(ws, chatid, chat_type, content)
    asyncio.run(_run())

# ── 命令行入口 ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--help":
        print(__doc__)
        sys.exit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == "--listen":
        asyncio.run(_listen_mode())
    else:
        if len(sys.argv) >= 2:
            content = " ".join(sys.argv[1:])
        else:
            content = sys.stdin.read().strip()
            if not content:
                print(__doc__)
                sys.exit(0)
        asyncio.run(_push_to_all(content))
