"""
alibaba_cloud_tool.py — Alibaba Cloud International Integration
================================================================
Covers:
  1. Qwen3 LLM  — Model Studio (OpenAI-compatible, intl endpoint)
  2. OSS         — Object Storage Service (oss2 SDK)
  3. Translation — Machine Translation REST API (ZH↔EN)
  4. Quotation   — CloudQuotation financial data feeds

Env vars needed in .env
------------------------
ALIBABA_CLOUD_ACCESS_KEY_ID      = <RAM user AccessKey ID>
ALIBABA_CLOUD_ACCESS_KEY_SECRET  = <RAM user AccessKey Secret>
DASHSCOPE_API_KEY                = <Model Studio API key>
OSS_BUCKET                       = insightbridge-oss   (or any bucket name)
OSS_ENDPOINT                     = oss-ap-southeast-1.aliyuncs.com
ALIYUN_TRANSLATE_REGION          = ap-southeast-1
CLOUDQUOTATION_ENDPOINT          = https://cloudquotation.alibabacloud.com (if subscribed)

All fields are optional — missing vars degrade gracefully per capability.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

# ── helpers ────────────────────────────────────────────────────────────────────

def _ak_id()  -> str: return os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID",  "")
def _ak_sec() -> str: return os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET","")
def _ds_key() -> str: return os.getenv("DASHSCOPE_API_KEY", "")
def _oss_bucket()   -> str: return os.getenv("OSS_BUCKET",   "insightbridge-oss")
def _oss_endpoint() -> str: return os.getenv("OSS_ENDPOINT", "oss-ap-southeast-1.aliyuncs.com")
def _translate_region() -> str: return os.getenv("ALIYUN_TRANSLATE_REGION","ap-southeast-1")


# ══════════════════════════════════════════════════════════════════════════════
#  1 · Qwen3 LLM Tool  (Model Studio — OpenAI-compatible intl endpoint)
# ══════════════════════════════════════════════════════════════════════════════

# International endpoint — same OpenAI client works by overriding base_url
_QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

# ── Qwen3 开源模型 API 名称（2026-05 确认，dashscope-intl 可用）──────────────
# Open-source weights (可在 HuggingFace/ModelScope 下载原始权重)
# MoE = Mixture of Experts；Dense = 全量激活稠密模型
QWEN_MODELS = {
    # ── Qwen3.6（最新，云端专有，非开源权重）──────────────────
    "qwen3.6-max":   "qwen3.6-max-preview",  # 旗舰，最强推理（预览版）
    "qwen3.6-plus":  "qwen3.6-plus",         # 均衡性价比
    "qwen3.6-flash": "qwen3.6-flash",        # 最快最便宜

    # ── Qwen3 开源模型 (Open Source) ──────────────────────────
    "qwen3-235b":    "qwen3-235b-a22b",      # MoE 235B总/22B激活，最强开源
    "qwen3-30b":     "qwen3-30b-a3b",        # MoE 30B总/3B激活，高性价比
    "qwen3-32b":     "qwen3-32b",            # Dense 32B，推理稳定
    "qwen3-14b":     "qwen3-14b",            # Dense 14B，轻量
    "qwen3-8b":      "qwen3-8b",             # Dense 8B，最省成本

    # ── 管理版别名（自动指向最新稳定版）──────────────────────
    "max":           "qwen-max",             # 自动追踪最新 Max
    "plus":          "qwen-plus",            # 自动追踪最新 Plus（推荐默认）
    "turbo":         "qwen-turbo",           # 最快响应
    "long":          "qwen-long",            # 1M token 超长上下文

    # ── 专用模型 ──────────────────────────────────────────────
    "coder":         "qwen-coder-plus",      # 代码生成
    "vl":            "qwen-vl-max",          # 视觉语言（图片理解）
}


class QwenChatInput(BaseModel):
    prompt:       str   = Field(description="User prompt / query for Qwen3")
    system:       str   = Field(default="You are a helpful financial analysis assistant with expertise in Asian markets, Chinese economy, and global macro.",
                                description="System prompt")
    model_alias:  str   = Field(default="qwen3-32b",
                                description=(
                                    "Model alias — open source: qwen3-235b | qwen3-30b | qwen3-32b | qwen3-14b | qwen3-8b; "
                                    "latest cloud: qwen3.6-max | qwen3.6-plus | qwen3.6-flash; "
                                    "managed: max | plus | turbo | long | coder | vl"
                                ))
    temperature:  float = Field(default=0.3)
    max_tokens:   int   = Field(default=2048)
    stream:       bool  = Field(default=False)


class QwenChatTool(BaseTool):
    """Call Alibaba Cloud Qwen3 LLM via Model Studio (international OpenAI-compat endpoint).
    Best for: Chinese market analysis, APAC macro, CN regulatory intelligence,
    bilingual (ZH/EN) reasoning, code generation."""

    name:        str = "QwenChatTool"
    description: str = (
        "Call Alibaba Cloud Qwen3 (qwen-max / qwen-plus / qwen-turbo / qwen-long) "
        "via the international Model Studio endpoint. "
        "Actions: chat with Chinese/APAC market intelligence. "
        "Excellent for: CN macro analysis, APAC hospitality data, ZH-EN bilingual tasks."
    )
    args_schema: Type[BaseModel] = QwenChatInput

    def _run(self, prompt: str, system: str = "You are a helpful financial analysis assistant.",
             model_alias: str = "plus", temperature: float = 0.3,
             max_tokens: int = 2048, stream: bool = False) -> dict:

        api_key = _ds_key()
        if not api_key:
            return {"error": "DASHSCOPE_API_KEY not set. "
                             "Create an API key at: https://modelstudio.console.alibabacloud.com/"}

        model_id = QWEN_MODELS.get(model_alias, model_alias)

        try:
            from openai import OpenAI

            client = OpenAI(
                api_key=api_key,
                base_url=_QWEN_BASE_URL,
            )

            # Qwen3 所有模型（开源 + 云端）非流式调用必须显式设置 enable_thinking=False
            # 如需思维链（CoT），改为 True 并切换 stream=True
            qwen3_models = {
                "qwen3-235b-a22b", "qwen3-30b-a3b", "qwen3-32b",
                "qwen3-14b", "qwen3-8b", "qwen3.6-max-preview",
                "qwen3.6-plus", "qwen3.6-flash", "qwen-max", "qwen-plus",
            }
            extra_body = {"enable_thinking": False} if model_id in qwen3_models else {}

            response = client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
                extra_body=extra_body if extra_body else None,
            )

            content = response.choices[0].message.content
            usage   = {
                "prompt_tokens":     response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens":      response.usage.total_tokens,
            }

            return {
                "model":   model_id,
                "backend": "alibaba_model_studio",
                "content": content,
                "usage":   usage,
            }

        except Exception as e:
            return {"error": str(e), "model": model_id, "backend": "alibaba_model_studio"}


# ══════════════════════════════════════════════════════════════════════════════
#  2 · OSS Tool  (Object Storage Service)
# ══════════════════════════════════════════════════════════════════════════════

class OSSTool_Input(BaseModel):
    action:  str = Field(description=(
        "'list' — list objects in bucket (prefix optional). "
        "'read' — download object and return text content. "
        "'write' — upload text content as object. "
        "'delete' — delete object. "
        "'bucket_info' — show bucket metadata."
    ))
    key:     str = Field(default="", description="OSS object key (path inside bucket)")
    content: str = Field(default="", description="Text content for 'write' action")
    prefix:  str = Field(default="", description="Key prefix filter for 'list' action")
    bucket:  str = Field(default="", description="Override OSS_BUCKET env var (optional)")


class OSSTool(BaseTool):
    """Read/write Alibaba Cloud OSS (Object Storage Service).
    Used as secondary data persistence layer alongside Google Cloud Storage."""

    name:        str = "OSSTool"
    description: str = (
        "Read/write Alibaba Cloud OSS object storage. "
        "Actions: list | read | write | delete | bucket_info. "
        "Use for: storing analysis reports, caching market data snapshots, "
        "model artifacts backup."
    )
    args_schema: Type[BaseModel] = OSSTool_Input

    def _run(self, action: str, key: str = "", content: str = "",
             prefix: str = "", bucket: str = "") -> dict:

        if not _ak_id() or not _ak_sec():
            return {"error": "ALIBABA_CLOUD_ACCESS_KEY_ID / SECRET not set."}

        bucket_name = bucket or _oss_bucket()
        endpoint    = _oss_endpoint()

        try:
            import oss2

            auth   = oss2.Auth(_ak_id(), _ak_sec())
            bucket_obj = oss2.Bucket(auth, endpoint, bucket_name)

            action = action.lower().strip()

            if action == "bucket_info":
                info = bucket_obj.get_bucket_info()
                return {
                    "bucket":        bucket_name,
                    "location":      info.location,
                    "storage_class": info.storage_class,
                    "creation_date": str(info.creation_date),
                }

            elif action == "list":
                objects = []
                for obj in oss2.ObjectIterator(bucket_obj, prefix=prefix, max_keys=200):
                    objects.append({
                        "key":           obj.key,
                        "size_bytes":    obj.size,
                        "last_modified": str(obj.last_modified),
                    })
                return {"bucket": bucket_name, "prefix": prefix, "count": len(objects), "objects": objects}

            elif action == "read":
                if not key:
                    return {"error": "'key' required for read action"}
                result = bucket_obj.get_object(key)
                data   = result.read().decode("utf-8", errors="replace")
                return {"key": key, "size_bytes": len(data), "content": data}

            elif action == "write":
                if not key:
                    return {"error": "'key' required for write action"}
                data = content.encode("utf-8")
                bucket_obj.put_object(key, data)
                return {"key": key, "bytes_written": len(data), "bucket": bucket_name, "status": "ok"}

            elif action == "delete":
                if not key:
                    return {"error": "'key' required for delete action"}
                bucket_obj.delete_object(key)
                return {"key": key, "bucket": bucket_name, "status": "deleted"}

            else:
                return {"error": f"Unknown action '{action}'. Use: list|read|write|delete|bucket_info"}

        except ImportError:
            return {"error": "oss2 not installed. Run: pip install oss2"}
        except Exception as e:
            return {"error": str(e), "bucket": bucket_name, "action": action}


# ══════════════════════════════════════════════════════════════════════════════
#  3 · Machine Translation Tool  (Alibaba Cloud — ZH↔EN)
# ══════════════════════════════════════════════════════════════════════════════

class TranslateInput(BaseModel):
    text:        str = Field(description="Text to translate")
    source_lang: str = Field(default="zh",
                             description="Source language code: zh | en | ja | ko | de | fr | es | pt | ar | ru")
    target_lang: str = Field(default="en",
                             description="Target language code: zh | en | ja | ko | de | fr | es | pt | ar | ru")
    domain:      str = Field(default="general",
                             description="Translation domain: general | ecommerce | medicine | law | machinery")


class AliyunTranslateTool(BaseTool):
    """Translation tool — primary: Alibaba Cloud MT; fallback: Qwen3 bilingual LLM.
    Handles: Chinese financial news, regulatory docs, hotel reviews, market commentary.
    Auto-falls-back to Qwen3 if alimt service not yet activated."""

    name:        str = "AliyunTranslateTool"
    description: str = (
        "Translate text between zh/en/ja/ko/de/fr/es/pt/ar/ru. "
        "Primary: Alibaba Cloud MT API. Fallback: Qwen3-32B bilingual model. "
        "Use for: CN market news → EN, hotel reviews, regulatory filings, policy docs."
    )
    args_schema: Type[BaseModel] = TranslateInput

    def _run(self, text: str, source_lang: str = "zh",
             target_lang: str = "en", domain: str = "general") -> dict:

        # ── Path 1: Alibaba Cloud MT REST API ─────────────────────────────
        if _ak_id() and _ak_sec():
            result = self._rest_translate(text, source_lang, target_lang, domain)
            # Only use MT result if translation is non-empty and no error
            if "error" not in result and result.get("translated"):
                return result
            # else: MT not activated or empty — fall through to Qwen3

        # ── Path 2: Qwen3 bilingual fallback ──────────────────────────────
        return self._qwen_translate(text, source_lang, target_lang)

    def _qwen_translate(self, text: str, source_lang: str, target_lang: str) -> dict:
        """Use Qwen3-32B for translation (fallback when alimt not activated)."""
        lang_names = {
            "zh": "Chinese", "en": "English", "ja": "Japanese",
            "ko": "Korean",  "de": "German",  "fr": "French",
            "es": "Spanish", "pt": "Portuguese", "ar": "Arabic", "ru": "Russian",
        }
        src = lang_names.get(source_lang, source_lang)
        tgt = lang_names.get(target_lang, target_lang)

        api_key = _ds_key()
        if not api_key:
            return {"error": "Neither alimt nor DASHSCOPE_API_KEY available for translation."}

        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=_QWEN_BASE_URL)
            resp = client.chat.completions.create(
                model="qwen3-32b",
                messages=[
                    {"role": "system", "content":
                        f"You are a professional translator. Translate the user's text from {src} to {tgt}. "
                        f"Output ONLY the translated text, no explanations or annotations."},
                    {"role": "user", "content": text},
                ],
                temperature=0.1,
                max_tokens=2048,
                extra_body={"enable_thinking": False},
            )
            translated = resp.choices[0].message.content.strip()
            return {
                "source_lang": source_lang,
                "target_lang": target_lang,
                "original":    text[:200] + "..." if len(text) > 200 else text,
                "translated":  translated,
                "backend":     "qwen3-32b (alimt fallback)",
                "word_count":  len(text.split()),
            }
        except Exception as e:
            return {"error": str(e), "backend": "qwen3-fallback"}

    def _rest_translate(self, text: str, source_lang: str,
                        target_lang: str, domain: str) -> dict:
        """Fallback: direct REST call with request signing."""
        import hashlib
        import hmac
        import base64
        import uuid
        import urllib.parse
        import urllib.request

        params = {
            "Action":          "TranslateGeneral",
            "Version":         "2018-10-12",
            "AccessKeyId":     _ak_id(),
            "Timestamp":       time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "SignatureMethod":  "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce":   str(uuid.uuid4()),
            "Format":          "JSON",
            "FormatType":      "text",
            "SourceLanguage":  source_lang,
            "TargetLanguage":  target_lang,
            "SourceText":      text,
            "Scene":           domain,
        }

        # Build canonical query string
        sorted_params = sorted(params.items())
        encoded = urllib.parse.urlencode(sorted_params)
        string_to_sign = (
            "GET&"
            + urllib.parse.quote("/", safe="")
            + "&"
            + urllib.parse.quote(encoded, safe="")
        )

        key = (_ak_sec() + "&").encode("utf-8")
        sig = base64.b64encode(
            hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1).digest()
        ).decode()

        params["Signature"] = sig
        url = "https://mt.aliyuncs.com/?" + urllib.parse.urlencode(params)

        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
                translated = data.get("Data", {}).get("Translated", "")
                return {
                    "source_lang": source_lang,
                    "target_lang": target_lang,
                    "translated":  translated,
                    "backend":     "rest",
                }
        except Exception as e2:
            return {"error": str(e2), "backend": "rest"}


# ══════════════════════════════════════════════════════════════════════════════
#  4 · CloudQuotation Tool  (financial market data — subscription required)
# ══════════════════════════════════════════════════════════════════════════════

class CloudQuotationInput(BaseModel):
    action:  str  = Field(default="snapshot",
                          description=(
                              "'snapshot' — latest price/OHLCV for a symbol. "
                              "'kline'    — candlestick data (1m/5m/15m/1h/1d). "
                              "'depth'    — order book depth. "
                              "'list'     — list available symbols/exchanges."
                          ))
    symbol:  str  = Field(default="", description="Market symbol, e.g. 'AAPL.US' | 'BTC/USDT' | '000001.SZ'")
    period:  str  = Field(default="1d", description="Kline period: 1m|5m|15m|1h|4h|1d")
    limit:   int  = Field(default=20,   description="Number of candles for kline")
    exchange:str  = Field(default="",   description="Exchange filter for list action")


class CloudQuotationTool(BaseTool):
    """Alibaba Cloud CloudQuotation — ultra-low-latency financial market data.
    Covers: A-shares (SSE/SZSE), HK stocks, US stocks, crypto, futures, FX.
    NOTE: Requires active CloudQuotation subscription in Alibaba Cloud console."""

    name:        str = "CloudQuotationTool"
    description: str = (
        "Fetch financial market data from Alibaba Cloud CloudQuotation. "
        "Actions: snapshot | kline | depth | list. "
        "Covers: CN A-shares, HK stocks, US equities, crypto, forex, futures. "
        "Ultra-low latency (co-located with exchange feeds). "
        "Requires CloudQuotation subscription."
    )
    args_schema: Type[BaseModel] = CloudQuotationInput

    # CloudQuotation OpenAPI endpoint
    _CQ_ENDPOINT = "cloudquotation.cn-shanghai.aliyuncs.com"
    _CQ_VERSION  = "2023-01-01"

    def _run(self, action: str = "snapshot", symbol: str = "",
             period: str = "1d", limit: int = 20, exchange: str = "") -> dict:

        if not _ak_id() or not _ak_sec():
            return {"error": "ALIBABA_CLOUD_ACCESS_KEY_ID / SECRET not set."}

        try:
            # Try alibabacloud SDK first
            return self._cq_sdk(action, symbol, period, limit, exchange)
        except ImportError:
            return self._cq_rest(action, symbol, period, limit, exchange)
        except Exception as e:
            error_msg = str(e)
            if "NotEnabled" in error_msg or "InvalidProduct" in error_msg:
                return {
                    "error": "CloudQuotation not subscribed.",
                    "action": "Subscribe at: https://www.alibabacloud.com/en/product/cloudquotation",
                    "hint": "Free trial available for basic market data feeds.",
                }
            return {"error": error_msg, "action": action, "symbol": symbol}

    def _cq_sdk(self, action: str, symbol: str,
                period: str, limit: int, exchange: str) -> dict:
        """Attempt using alibabacloud-cloudquotation SDK."""
        # Generic approach — use openapi directly
        from alibabacloud_tea_openapi import models as open_api_models
        from alibabacloud_tea_util import models as util_models

        cfg = open_api_models.Config(
            access_key_id=_ak_id(),
            access_key_secret=_ak_sec(),
            endpoint=self._CQ_ENDPOINT,
        )

        # Map action → API Action
        action_map = {
            "snapshot": "GetStockRealtime",
            "kline":    "GetKLineData",
            "depth":    "GetOrderBook",
            "list":     "ListStocks",
        }
        api_action = action_map.get(action.lower(), "GetStockRealtime")

        query = {}
        if symbol:  query["Symbol"]   = symbol
        if exchange: query["Exchange"] = exchange
        if action == "kline":
            query["Period"] = period
            query["Limit"]  = str(limit)

        # Build and send request
        params = open_api_models.Params(
            action=api_action,
            version=self._CQ_VERSION,
            protocol="HTTPS",
            method="GET",
            auth_type="AK",
            style="RPC",
            pathname="/",
            req_body_type="json",
            body_type="json",
        )

        from alibabacloud_tea_openapi.client import Client as OpenApiClient
        client = OpenApiClient(cfg)
        request = open_api_models.OpenApiRequest(query=query)
        runtime = util_models.RuntimeOptions()

        response = client.call_api(params, request, runtime)
        body = response.get("body", {})
        return {"action": action, "symbol": symbol, "data": body, "backend": "alicloud_sdk"}

    def _cq_rest(self, action: str, symbol: str,
                 period: str, limit: int, exchange: str) -> dict:
        """Fallback REST implementation for CloudQuotation."""
        import hashlib, hmac, base64, uuid, urllib.parse, urllib.request

        action_map = {
            "snapshot": "GetStockRealtime",
            "kline":    "GetKLineData",
            "depth":    "GetOrderBook",
            "list":     "ListStocks",
        }
        api_action = action_map.get(action.lower(), "GetStockRealtime")

        params = {
            "Action":          api_action,
            "Version":         self._CQ_VERSION,
            "AccessKeyId":     _ak_id(),
            "Timestamp":       time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "SignatureMethod":  "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce":   str(uuid.uuid4()),
            "Format":          "JSON",
        }
        if symbol:  params["Symbol"]   = symbol
        if exchange: params["Exchange"] = exchange
        if action == "kline":
            params["Period"] = period
            params["Limit"]  = str(limit)

        sorted_params = sorted(params.items())
        encoded = urllib.parse.urlencode(sorted_params)
        string_to_sign = (
            "GET&"
            + urllib.parse.quote("/", safe="")
            + "&"
            + urllib.parse.quote(encoded, safe="")
        )

        key = (_ak_sec() + "&").encode("utf-8")
        sig = base64.b64encode(
            hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1).digest()
        ).decode()

        params["Signature"] = sig
        url = f"https://{self._CQ_ENDPOINT}/?" + urllib.parse.urlencode(params)

        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
                return {"action": action, "symbol": symbol, "data": data, "backend": "rest"}
        except Exception as e:
            return {"error": str(e), "action": action, "symbol": symbol}


# ══════════════════════════════════════════════════════════════════════════════
#  Quick test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    print("=== Qwen3 Test ===")
    qwen = QwenChatTool()
    result = qwen._run(
        prompt="用一句话解释为什么中国央行最近降准对A股市场的影响。",
        model_alias="plus",
        temperature=0.3,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))

    print("\n=== OSS List Test ===")
    oss = OSSTool()
    print(oss._run(action="list", prefix="insightbridge/"))

    print("\n=== Translation Test ===")
    tr = AliyunTranslateTool()
    print(tr._run(text="上证指数今日大涨，创下年内新高。", source_lang="zh", target_lang="en"))
