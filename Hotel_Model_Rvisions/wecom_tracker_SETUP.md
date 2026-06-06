# WeCom 访客追踪系统配置指南

## 一、在企业微信后台创建"网页应用"

1. 打开 https://work.weixin.qq.com/wework_admin/
2. 进入 **应用管理 → 应用 → 创建应用**
3. 选择 **自建应用**
4. 填写：
   - 应用名称：`InsightBridge Tracker`
   - 应用Logo：随意
   - 可见范围：选你自己（或管理员）
5. 创建后，记录：
   - **AgentId**（应用页面有显示）
   - **Secret**（点"Secret"旁的查看按钮，发到手机确认）
6. 在应用设置中 → **网页授权及JS-SDK** → 填入可信域名：
   ```
   track.insightbridge.global
   ```
   （不需要加 https://，直接填域名）

## 二、获取企业ID

1. 企微管理后台 → **我的企业** → 最底部
2. 复制 **企业ID**（格式：`ww` + 16位字母数字）

## 三、配置 .env 文件

在 `/Users/tongyin/Desktop/Hotel Model Rvisions/.env` 中添加：

```env
# 企业微信访客追踪
WECOM_CORP_ID=ww你的企业ID
WECOM_TRACKER_AGENT_ID=你的AgentId（纯数字）
WECOM_TRACKER_SECRET=你的应用Secret
TRACKER_BASE_URL=https://track.insightbridge.global
```

## 四、配置 DNS

在 Cloudflare 中添加 A 记录：
```
track.insightbridge.global  →  你的服务器IP
```
（可以和 app.insightbridge.global 指向同一台阿里云服务器）

## 五、在服务器上运行

```bash
pip install fastapi uvicorn aiosqlite httpx python-dotenv
uvicorn wecom_tracker:app --host 0.0.0.0 --port 8001
```

配置 Nginx 反代（在阿里云服务器的 nginx.conf 中添加）：
```nginx
server {
    listen 443 ssl;
    server_name track.insightbridge.global;
    
    # SSL 证书（用 certbot 申请）
    ssl_certificate /etc/letsencrypt/live/track.insightbridge.global/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/track.insightbridge.global/privkey.pem;
    
    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## 六、使用方式

### 发给客户的追踪链接（复制直接发）

| 内容 | 追踪链接 |
|------|---------|
| 东南亚OTA报告 | `https://track.insightbridge.global/r/ota-report` |
| AI清算白皮书 | `https://track.insightbridge.global/r/ai-reckoning` |
| HBS案例研究 | `https://track.insightbridge.global/r/hbs-case-study` |
| HBS案例（中文）| `https://track.insightbridge.global/r/hbs-case-cn` |
| 核心代码理论 | `https://track.insightbridge.global/r/core-code-theory` |
| Intelligence Vol.01 | `https://track.insightbridge.global/r/intelligence-vol01` |
| 市场情报报告 | `https://track.insightbridge.global/r/market-report` |

### 查看访客日志
浏览器打开：`https://track.insightbridge.global/admin/visits`

### 查看全部追踪链接
浏览器打开：`https://track.insightbridge.global/admin/links`

## 七、收到推送效果

当客户在企微中点击追踪链接时，你会收到：

```
👁️ 访客提醒
张三 · 收益总监 正在阅读

📄 东南亚OTA预订成本分析

🕐 14:23　　🔗 追踪链接
```

## 注意事项

- 此功能**仅对通过企微内置浏览器打开链接的访客有效**
- 普通 Chrome/Safari 浏览器访问不会被识别
- 企微内部成员可识别完整信息（姓名、职位、部门）
- 外部联系人（已添加为企微外部联系人）显示为"外部访客"
