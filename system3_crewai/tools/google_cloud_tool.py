"""
google_cloud_tool.py — Google Cloud 全套服务 CrewAI 工具
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

5 个服务，5 个 CrewAI 工具：

  BigQueryTool        → SQL查询/写入，数据分析，酒店/市场/交易数据集
  CloudStorageTool    → 文件上传/下载/列表，报告存档，模型文件
  VertexAITool        → Gemini 2.0 文本+视觉推理，文档分析，策略建议
  FirestoreTool       → 实时数据库：持仓状态、信号历史、CRM、事件记录
  GoogleSheetsTool    → 读写 Google Sheets，仪表盘输出，报告分发

认证：Google ADC（Application Default Credentials）
项目：serious-sylph-495713-h7

环境变量：
  GOOGLE_APPLICATION_CREDENTIALS  → ADC 文件路径
  GCP_PROJECT_ID                   → serious-sylph-495713-h7
  GCP_REGION                       → us-central1
  BIGQUERY_DATASET                 → insightbridge_data
  GCS_BUCKET                       → insightbridge-storage
  VERTEX_AI_MODEL                  → gemini-1.5-flash
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import os
import json
import time
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── 全局配置 ────────────────────────────────────────────────────────
_PROJECT   = os.getenv("GCP_PROJECT_ID",   "serious-sylph-495713-h7")
_REGION    = os.getenv("GCP_REGION",       "us-central1")
_DATASET   = os.getenv("BIGQUERY_DATASET", "insightbridge_data")
_BUCKET    = os.getenv("GCS_BUCKET",       "insightbridge-storage")
_VERTEX_M  = os.getenv("VERTEX_AI_MODEL",  "gemini-2.0-flash")
_ADC_PATH  = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
)
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", _ADC_PATH)


# ══════════════════════════════════════════════════════════════════
#  1. BigQuery 工具
# ══════════════════════════════════════════════════════════════════

class BQInput(BaseModel):
    action: str = Field(
        default="query",
        description=(
            "'query'  — 执行 SQL 查询（返回结果 JSON）\n"
            "'write'  — 写入数据到 BigQuery 表\n"
            "'create' — 创建数据集或表\n"
            "'list'   — 列出数据集下所有表\n"
            "'schema' — 查询指定表的 Schema"
        )
    )
    sql: str = Field(default="", description="SQL 查询语句（action=query 时使用）")
    table: str = Field(default="", description="表名，格式：dataset.table 或 table（自动加默认 dataset）")
    rows: str = Field(default="", description="JSON 格式行数据列表，如 '[{\"col1\":1,\"col2\":\"a\"}]'")
    schema: str = Field(default="", description="建表 schema JSON，如 '[{\"name\":\"id\",\"type\":\"STRING\"}]'")
    limit: int = Field(default=100, description="query 返回最大行数（默认100）")


class BigQueryTool(BaseTool):
    name: str        = "BigQueryTool"
    description: str = (
        "Google BigQuery 数据仓库工具。\n"
        "• query  — 执行 SQL 分析（酒店定价、市场趋势、交易记录等）\n"
        "• write  — 批量写入数据行\n"
        "• create — 创建数据集 / 表\n"
        "• list   — 列出所有表\n"
        "• schema — 查看表结构\n\n"
        "项目: serious-sylph-495713-h7  默认数据集: insightbridge_data"
    )
    args_schema: type[BaseModel] = BQInput

    def _run(self, action: str = "query", sql: str = "", table: str = "",
             rows: str = "", schema: str = "", limit: int = 100) -> str:
        try:
            from google.cloud import bigquery
            client = bigquery.Client(project=_PROJECT)

            # 标准化表名
            if table and "." not in table:
                table = f"{_DATASET}.{table}"

            if action == "query":
                if not sql:
                    return json.dumps({"error": "需要提供 sql 参数"})
                # 自动加 LIMIT
                q = sql.rstrip(";")
                if "limit" not in q.lower():
                    q += f" LIMIT {limit}"
                job = client.query(q)
                rows_out = []
                for row in job.result():
                    rows_out.append(dict(row))
                return json.dumps({
                    "action":     "query",
                    "rows":       rows_out,
                    "count":      len(rows_out),
                    "bytes_billed": job.total_bytes_billed,
                }, ensure_ascii=False, indent=2)

            elif action == "write":
                if not table or not rows:
                    return json.dumps({"error": "需要 table 和 rows 参数"})
                data = json.loads(rows)
                errors = client.insert_rows_json(table, data)
                return json.dumps({
                    "action":  "write",
                    "table":   table,
                    "written": len(data),
                    "errors":  errors or [],
                }, ensure_ascii=False)

            elif action == "create":
                if not table:
                    return json.dumps({"error": "需要 table 参数（dataset 或 dataset.table）"})
                if "." not in table:
                    # 创建 dataset
                    ds = bigquery.Dataset(f"{_PROJECT}.{table}")
                    ds.location = _REGION
                    try:
                        client.create_dataset(ds, exists_ok=True)
                        return json.dumps({"action": "create_dataset", "dataset": table, "status": "OK"})
                    except Exception as e:
                        return json.dumps({"error": str(e)})
                else:
                    if not schema:
                        return json.dumps({"error": "建表需要 schema 参数"})
                    schema_def = json.loads(schema)
                    bq_schema = [
                        bigquery.SchemaField(f["name"], f.get("type", "STRING"),
                                             mode=f.get("mode", "NULLABLE"))
                        for f in schema_def
                    ]
                    tbl_ref = bigquery.Table(f"{_PROJECT}.{table}", schema=bq_schema)
                    client.create_table(tbl_ref, exists_ok=True)
                    return json.dumps({"action": "create_table", "table": table,
                                       "columns": len(bq_schema), "status": "OK"})

            elif action == "list":
                ds = table.split(".")[0] if table and "." in table else _DATASET
                tables = list(client.list_tables(f"{_PROJECT}.{ds}"))
                return json.dumps({
                    "action":  "list",
                    "dataset": ds,
                    "tables":  [t.table_id for t in tables],
                    "count":   len(tables),
                }, ensure_ascii=False)

            elif action == "schema":
                if not table:
                    return json.dumps({"error": "需要 table 参数"})
                tbl = client.get_table(f"{_PROJECT}.{table}")
                return json.dumps({
                    "action": "schema",
                    "table":  table,
                    "schema": [
                        {"name": f.name, "type": f.field_type, "mode": f.mode}
                        for f in tbl.schema
                    ],
                    "rows_approx": tbl.num_rows,
                }, ensure_ascii=False, indent=2)

            return json.dumps({"error": f"未知 action: {action}"})

        except Exception as e:
            logger.error(f"[BigQueryTool] {e}", exc_info=True)
            return json.dumps({"error": str(e)}, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════
#  2. Cloud Storage 工具
# ══════════════════════════════════════════════════════════════════

class GCSInput(BaseModel):
    action: str = Field(
        default="list",
        description=(
            "'list'         — 列出 bucket 中的文件\n"
            "'upload_text'  — 上传文本内容为文件\n"
            "'download'     — 下载文件内容（返回文本）\n"
            "'delete'       — 删除文件\n"
            "'create_bucket'— 创建新 bucket\n"
            "'url'          — 生成签名 URL（7天有效）"
        )
    )
    path: str  = Field(default="", description="GCS 路径，如 reports/2026-05-08.json 或 bucket/path")
    content: str = Field(default="", description="upload_text 时的文件内容")
    prefix: str  = Field(default="", description="list 时的前缀过滤，如 'reports/'")
    bucket: str  = Field(default="", description="bucket 名称（默认用 GCS_BUCKET 环境变量）")


class CloudStorageTool(BaseTool):
    name: str        = "CloudStorageTool"
    description: str = (
        "Google Cloud Storage 对象存储工具。\n"
        "• list          — 列出文件（支持前缀过滤）\n"
        "• upload_text   — 上传报告/JSON/CSV 到 GCS\n"
        "• download      — 读取文件内容\n"
        "• delete        — 删除文件\n"
        "• create_bucket — 创建 bucket\n"
        "• url           — 生成公开访问 URL\n\n"
        f"默认 bucket: insightbridge-storage  项目: serious-sylph-495713-h7"
    )
    args_schema: type[BaseModel] = GCSInput

    def _run(self, action: str = "list", path: str = "", content: str = "",
             prefix: str = "", bucket: str = "") -> str:
        try:
            from google.cloud import storage
            client = storage.Client(project=_PROJECT)
            bucket_name = bucket or _BUCKET

            if action == "create_bucket":
                bkt = client.bucket(bucket_name)
                bkt.storage_class = "STANDARD"
                new_bkt = client.create_bucket(bkt, location=_REGION)
                return json.dumps({"action": "create_bucket",
                                   "bucket": bucket_name, "status": "created"})

            # 检查 bucket 是否存在，不存在则尝试创建
            try:
                bkt = client.get_bucket(bucket_name)
            except Exception:
                # bucket 不存在，自动创建
                try:
                    bkt = client.create_bucket(bucket_name, location=_REGION)
                    logger.info(f"[GCS] 自动创建 bucket: {bucket_name}")
                except Exception as e2:
                    return json.dumps({"error": f"bucket '{bucket_name}' 不存在且创建失败: {e2}"})

            if action == "list":
                blobs = list(client.list_blobs(bucket_name, prefix=prefix or None,
                                               max_results=100))
                return json.dumps({
                    "action":  "list",
                    "bucket":  bucket_name,
                    "prefix":  prefix or "(全部)",
                    "files":   [{"name": b.name, "size": b.size,
                                 "updated": str(b.updated)[:19]} for b in blobs],
                    "count":   len(blobs),
                }, ensure_ascii=False, indent=2)

            elif action == "upload_text":
                if not path or not content:
                    return json.dumps({"error": "需要 path 和 content 参数"})
                blob = bkt.blob(path)
                content_type = "application/json" if path.endswith(".json") else "text/plain"
                blob.upload_from_string(content, content_type=content_type)
                gcs_uri = f"gs://{bucket_name}/{path}"
                return json.dumps({"action": "upload_text", "uri": gcs_uri,
                                   "bytes": len(content.encode()), "status": "OK"})

            elif action == "download":
                if not path:
                    return json.dumps({"error": "需要 path 参数"})
                blob = bkt.blob(path)
                if not blob.exists():
                    return json.dumps({"error": f"文件不存在: {path}"})
                text = blob.download_as_text()
                return json.dumps({"action": "download", "path": path,
                                   "size": len(text), "content": text[:5000]
                                   + ("...(截断)" if len(text) > 5000 else "")})

            elif action == "delete":
                if not path:
                    return json.dumps({"error": "需要 path 参数"})
                blob = bkt.blob(path)
                blob.delete()
                return json.dumps({"action": "delete", "path": path, "status": "deleted"})

            elif action == "url":
                if not path:
                    return json.dumps({"error": "需要 path 参数"})
                blob = bkt.blob(path)
                # 公开访问 URL（需要 bucket 为 public 或生成签名URL）
                url = f"https://storage.googleapis.com/{bucket_name}/{path}"
                return json.dumps({"action": "url", "path": path,
                                   "public_url": url,
                                   "gcs_uri": f"gs://{bucket_name}/{path}"})

            return json.dumps({"error": f"未知 action: {action}"})

        except Exception as e:
            logger.error(f"[CloudStorageTool] {e}", exc_info=True)
            return json.dumps({"error": str(e)}, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════
#  3. Vertex AI / Gemini 工具
# ══════════════════════════════════════════════════════════════════

class VertexInput(BaseModel):
    action: str = Field(
        default="generate",
        description=(
            "'generate'     — 文本生成（Gemini 1.5 Flash）\n"
            "'analyze_doc'  — 分析文档/报告（传入文本，返回结构化分析）\n"
            "'market_brief' — 生成市场简报（输入信号数据，返回中文解读）\n"
            "'risk_assess'  — 风险评估（输入持仓/信号，输出风险建议）\n"
            "'list_models'  — 列出可用 Vertex AI 模型"
        )
    )
    prompt: str  = Field(default="", description="生成/分析的输入文本或问题")
    context: str = Field(default="", description="背景数据（JSON字符串），用于 market_brief / risk_assess")
    model: str   = Field(default="", description="指定模型（空=默认 gemini-1.5-flash）")
    temperature: float = Field(default=0.2, description="生成温度（0=确定性，1=创意性，默认0.2）")
    max_tokens: int    = Field(default=2048, description="最大输出 token 数")


_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")


def _build_full_prompt(action: str, prompt: str, context: str) -> str:
    """Construct the prompt string for each action type."""
    if action == "generate":
        return prompt
    elif action == "analyze_doc":
        return (
            "请对以下文档进行结构化分析，提取关键信息，用中文输出：\n\n"
            f"{prompt}\n\n"
            "输出格式：\n1. 核心主题\n2. 关键数据点\n3. 风险与机遇\n4. 可执行建议"
        )
    elif action == "market_brief":
        ctx = ""
        if context:
            try:
                ctx_data = json.loads(context)
                ctx = json.dumps(ctx_data, ensure_ascii=False, indent=2)
            except Exception:
                ctx = context
        return (
            "你是 InsightBridge Global 的首席市场分析师。"
            "根据以下市场信号数据，用专业简洁的中文生成一份市场简报（200字以内）：\n\n"
            f"信号数据：\n{ctx or prompt}\n\n"
            f"附加说明：{prompt if ctx else ''}\n\n"
            "输出：直接输出简报，不需要标题。"
        )
    elif action == "risk_assess":
        ctx = context or "{}"
        return (
            "你是量化风控专家。根据以下持仓和市场状态，"
            "用中文给出风险评估报告（150字以内），重点说明最大风险和建议操作：\n\n"
            f"持仓/信号数据：\n{ctx}\n\n"
            f"问题：{prompt or '请评估当前风险状况'}"
        )
    return prompt


class VertexAITool(BaseTool):
    name: str        = "VertexAITool"
    description: str = (
        "Google Gemini 文本生成与分析工具（双路后端：google.genai API Key + Vertex AI ADC）。\n"
        "• generate     — 自由文本生成（问答、摘要、翻译）\n"
        "• analyze_doc  — 长文档结构化分析（研究报告、合同、财务数据）\n"
        "• market_brief — 把信号 JSON 转化为中文市场简报\n"
        "• risk_assess  — 基于持仓和市场状态给出风险建议\n"
        "• list_models  — 列出可用模型\n\n"
        f"默认模型: gemini-2.0-flash  项目: serious-sylph-495713-h7"
    )
    args_schema: type[BaseModel] = VertexInput

    def _run(self, action: str = "generate", prompt: str = "", context: str = "",
             model: str = "", temperature: float = 0.2, max_tokens: int = 2048) -> str:
        model_id = model or _VERTEX_M

        if action == "list_models":
            return json.dumps({
                "available": [
                    "gemini-2.0-flash",
                    "gemini-1.5-pro",
                    "gemini-1.5-flash",
                    "gemini-2.0-flash-lite",
                ],
                "current": model_id,
                "backend": "google.genai SDK (GEMINI_API_KEY)" if _GEMINI_API_KEY else "Vertex AI ADC",
            }, ensure_ascii=False)

        full_prompt = _build_full_prompt(action, prompt, context)

        # ── 路径①：google.genai SDK（GEMINI_API_KEY，不需要 Vertex AI 项目权限）──
        if _GEMINI_API_KEY:
            try:
                from google import genai as _genai
                from google.genai import types as _gtypes
                client = _genai.Client(api_key=_GEMINI_API_KEY)
                # google.genai uses gemini-2.0-flash by default
                genai_model = model_id if model_id.startswith("gemini-") else "gemini-2.0-flash"
                cfg = _gtypes.GenerateContentConfig(
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                )
                resp = client.models.generate_content(
                    model=genai_model,
                    contents=full_prompt,
                    config=cfg,
                )
                text = resp.text if hasattr(resp, "text") else str(resp)
                return json.dumps({
                    "action":  action,
                    "model":   genai_model,
                    "backend": "google.genai",
                    "output":  text,
                }, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f"[VertexAITool] google.genai failed ({e}), trying Vertex AI…")

        # ── 路径②：Vertex AI SDK（ADC，需要项目中启用 Vertex AI API）────────
        try:
            import vertexai
            from vertexai.generative_models import GenerativeModel, GenerationConfig

            vertexai.init(project=_PROJECT, location=_REGION)
            gen_config = GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
            )
            gm = GenerativeModel(model_id)
            response = gm.generate_content(full_prompt, generation_config=gen_config)
            text = response.text if hasattr(response, "text") else str(response)

            return json.dumps({
                "action":  action,
                "model":   model_id,
                "backend": "vertex_ai",
                "output":  text,
            }, ensure_ascii=False, indent=2)

        except Exception as e:
            logger.error(f"[VertexAITool] Both backends failed: {e}", exc_info=True)
            return json.dumps({
                "error": str(e),
                "hint": (
                    "路径①(google.genai)需要有效的 GEMINI_API_KEY + 付费配额；"
                    "路径②(Vertex AI)需要在 GCP Console 启用 Vertex AI API。"
                ),
            }, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════
#  4. Firestore 工具
# ══════════════════════════════════════════════════════════════════

class FSInput(BaseModel):
    action: str = Field(
        default="read",
        description=(
            "'read'       — 读取文档（collection/doc_id）\n"
            "'write'      — 写入/覆盖文档\n"
            "'update'     — 更新文档字段（不覆盖其他字段）\n"
            "'delete'     — 删除文档\n"
            "'query'      — 查询 collection（支持简单过滤）\n"
            "'list_cols'  — 列出所有 collection\n"
            "'log_signal' — 快捷写入交易信号日志\n"
            "'log_trade'  — 快捷写入交易记录"
        )
    )
    collection: str = Field(default="signals", description="Firestore collection 名称")
    doc_id: str     = Field(default="", description="文档 ID（空=自动生成）")
    data: str       = Field(default="{}", description="JSON 格式数据")
    field: str      = Field(default="", description="query 时的过滤字段名")
    value: str      = Field(default="", description="query 时的过滤值")
    limit: int      = Field(default=20, description="query 返回最大条数")


class FirestoreTool(BaseTool):
    name: str        = "FirestoreTool"
    description: str = (
        "Google Firestore 实时 NoSQL 数据库工具。\n"
        "• read        — 读取指定文档\n"
        "• write       — 写入文档（自动时间戳）\n"
        "• update      — 局部更新字段\n"
        "• delete      — 删除文档\n"
        "• query       — 按字段过滤查询 collection\n"
        "• log_signal  — 快捷记录交易信号（自动 collection=signals）\n"
        "• log_trade   — 快捷记录成交记录（自动 collection=trades）\n\n"
        "常用 collection：signals / trades / positions / reports / crm_contacts\n"
        f"项目: serious-sylph-495713-h7"
    )
    args_schema: type[BaseModel] = FSInput

    def _run(self, action: str = "read", collection: str = "signals",
             doc_id: str = "", data: str = "{}", field: str = "",
             value: str = "", limit: int = 20) -> str:
        try:
            from google.cloud import firestore
            db = firestore.Client(project=_PROJECT)

            now_iso = datetime.now(timezone.utc).isoformat()[:19]

            if action == "list_cols":
                cols = list(db.collections())
                return json.dumps({
                    "collections": [c.id for c in cols],
                    "count": len(cols),
                }, ensure_ascii=False)

            col_ref = db.collection(collection)

            if action == "read":
                if not doc_id:
                    return json.dumps({"error": "read 需要 doc_id"})
                doc = col_ref.document(doc_id).get()
                if doc.exists:
                    return json.dumps({"action": "read", "id": doc_id,
                                       "data": doc.to_dict()},
                                      ensure_ascii=False, indent=2, default=str)
                return json.dumps({"action": "read", "id": doc_id, "exists": False})

            elif action == "write":
                doc_data = json.loads(data)
                doc_data["_updated_at"] = now_iso
                doc_data["_created_at"] = now_iso
                if doc_id:
                    col_ref.document(doc_id).set(doc_data)
                    rid = doc_id
                else:
                    ref = col_ref.add(doc_data)[1]
                    rid = ref.id
                return json.dumps({"action": "write", "id": rid,
                                   "collection": collection, "status": "OK"})

            elif action == "update":
                if not doc_id:
                    return json.dumps({"error": "update 需要 doc_id"})
                doc_data = json.loads(data)
                doc_data["_updated_at"] = now_iso
                col_ref.document(doc_id).update(doc_data)
                return json.dumps({"action": "update", "id": doc_id, "status": "OK"})

            elif action == "delete":
                if not doc_id:
                    return json.dumps({"error": "delete 需要 doc_id"})
                col_ref.document(doc_id).delete()
                return json.dumps({"action": "delete", "id": doc_id, "status": "deleted"})

            elif action == "query":
                if field and value:
                    # 尝试类型转换
                    try:
                        v = json.loads(value)
                    except Exception:
                        v = value
                    docs = col_ref.where(field, "==", v).limit(limit).stream()
                else:
                    docs = col_ref.limit(limit).stream()
                results = []
                for d in docs:
                    results.append({"id": d.id, "data": d.to_dict()})
                return json.dumps({
                    "action":     "query",
                    "collection": collection,
                    "filter":     f"{field}=={value}" if field else "无",
                    "results":    results,
                    "count":      len(results),
                }, ensure_ascii=False, indent=2, default=str)

            elif action == "log_signal":
                doc_data = json.loads(data)
                doc_data.update({
                    "_type":      "signal",
                    "_logged_at": now_iso,
                })
                ref = db.collection("signals").add(doc_data)[1]
                return json.dumps({"action": "log_signal", "id": ref.id,
                                   "status": "logged", "ts": now_iso})

            elif action == "log_trade":
                doc_data = json.loads(data)
                doc_data.update({
                    "_type":      "trade",
                    "_logged_at": now_iso,
                })
                ref = db.collection("trades").add(doc_data)[1]
                return json.dumps({"action": "log_trade", "id": ref.id,
                                   "status": "logged", "ts": now_iso})

            return json.dumps({"error": f"未知 action: {action}"})

        except Exception as e:
            logger.error(f"[FirestoreTool] {e}", exc_info=True)
            return json.dumps({"error": str(e)}, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════
#  5. Google Sheets 工具
# ══════════════════════════════════════════════════════════════════

class SheetsInput(BaseModel):
    action: str = Field(
        default="read",
        description=(
            "'read'       — 读取 Sheet 数据\n"
            "'write'      — 写入行数据（追加到末尾）\n"
            "'clear'      — 清空指定范围\n"
            "'overwrite'  — 覆盖指定范围\n"
            "'create'     — 创建新 Google Spreadsheet\n"
            "'list'       — 列出 Google Drive 中的 Sheets"
        )
    )
    sheet_id: str  = Field(default="", description="Google Sheets ID（URL中的长字符串）")
    sheet_name: str= Field(default="Sheet1", description="工作表名称（tab名）")
    range_a1: str  = Field(default="A1", description="A1 格式范围，如 'A1:E10' 或 'A1'")
    values: str    = Field(default="[]", description="JSON 二维数组，如 '[[\"col1\",\"val1\"],[\"col2\",\"val2\"]]'")
    title: str     = Field(default="InsightBridge Report", description="create 时的文件名")


class GoogleSheetsTool(BaseTool):
    name: str        = "GoogleSheetsTool"
    description: str = (
        "Google Sheets 读写工具。\n"
        "• read      — 读取 Sheet 数据（返回二维数组）\n"
        "• write     — 追加数据行到 Sheet 末尾\n"
        "• overwrite — 覆盖指定 A1 范围\n"
        "• clear     — 清空范围\n"
        "• create    — 创建新 Spreadsheet（返回 Sheet ID）\n"
        "• list      — 列出 Google Drive 中的 Sheets\n\n"
        "用途：每日报告输出、定价数据追踪、交易记录仪表盘"
    )
    args_schema: type[BaseModel] = SheetsInput

    def _get_service(self):
        """获取 Google Sheets API 客户端"""
        import google.auth
        from googleapiclient.discovery import build
        creds, _ = google.auth.default(
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]
        )
        sheets_svc = build("sheets", "v4", credentials=creds)
        drive_svc  = build("drive",  "v3", credentials=creds)
        return sheets_svc, drive_svc

    def _run(self, action: str = "read", sheet_id: str = "",
             sheet_name: str = "Sheet1", range_a1: str = "A1",
             values: str = "[]", title: str = "InsightBridge Report") -> str:
        try:
            sheets_svc, drive_svc = self._get_service()
            full_range = f"{sheet_name}!{range_a1}"

            if action == "create":
                body = {
                    "properties": {"title": title},
                    "sheets": [{"properties": {"title": sheet_name}}],
                }
                result = sheets_svc.spreadsheets().create(
                    body=body, fields="spreadsheetId,spreadsheetUrl"
                ).execute()
                sid = result["spreadsheetId"]
                return json.dumps({
                    "action":  "create",
                    "title":   title,
                    "id":      sid,
                    "url":     result.get("spreadsheetUrl"),
                    "note":    "记录此 ID 用于后续读写",
                }, ensure_ascii=False)

            elif action == "list":
                res = drive_svc.files().list(
                    q="mimeType='application/vnd.google-apps.spreadsheet'",
                    fields="files(id,name,modifiedTime)",
                    pageSize=20,
                    orderBy="modifiedTime desc",
                ).execute()
                files = res.get("files", [])
                return json.dumps({
                    "action": "list",
                    "count":  len(files),
                    "sheets": [{"id": f["id"], "name": f["name"],
                                "modified": f.get("modifiedTime", "")[:10]} for f in files],
                }, ensure_ascii=False, indent=2)

            if not sheet_id:
                return json.dumps({"error": "read/write/clear/overwrite 需要 sheet_id"})

            if action == "read":
                result = sheets_svc.spreadsheets().values().get(
                    spreadsheetId=sheet_id,
                    range=full_range,
                ).execute()
                rows = result.get("values", [])
                return json.dumps({
                    "action":    "read",
                    "range":     full_range,
                    "rows":      rows,
                    "row_count": len(rows),
                    "col_count": max((len(r) for r in rows), default=0),
                }, ensure_ascii=False, indent=2)

            elif action == "write":
                data = json.loads(values)
                body = {"values": data}
                result = sheets_svc.spreadsheets().values().append(
                    spreadsheetId=sheet_id,
                    range=full_range,
                    valueInputOption="USER_ENTERED",
                    body=body,
                ).execute()
                return json.dumps({
                    "action":   "write",
                    "updated":  result.get("updates", {}).get("updatedRows", 0),
                    "range":    result.get("updates", {}).get("updatedRange", ""),
                }, ensure_ascii=False)

            elif action == "overwrite":
                data = json.loads(values)
                body = {"values": data}
                result = sheets_svc.spreadsheets().values().update(
                    spreadsheetId=sheet_id,
                    range=full_range,
                    valueInputOption="USER_ENTERED",
                    body=body,
                ).execute()
                return json.dumps({
                    "action":   "overwrite",
                    "updated":  result.get("updatedRows", 0),
                    "range":    result.get("updatedRange", ""),
                }, ensure_ascii=False)

            elif action == "clear":
                sheets_svc.spreadsheets().values().clear(
                    spreadsheetId=sheet_id,
                    range=full_range,
                ).execute()
                return json.dumps({"action": "clear", "range": full_range, "status": "OK"})

            return json.dumps({"error": f"未知 action: {action}"})

        except Exception as e:
            logger.error(f"[GoogleSheetsTool] {e}", exc_info=True)
            return json.dumps({"error": str(e)}, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════
#  自检（python google_cloud_tool.py）
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    print("=" * 60)
    print("Google Cloud Tool — 连接自检")
    print(f"Project: {_PROJECT} | Region: {_REGION}")
    print("=" * 60)

    # 1. BigQuery
    print("\n[1] BigQueryTool — list tables")
    bq = BigQueryTool()
    r = json.loads(bq._run(action="list"))
    if "error" in r:
        print(f"  ⚠️  {r['error'][:80]}")
        print(f"  (数据集 '{_DATASET}' 可能尚未创建，这是正常的)")
    else:
        print(f"  ✅ 数据集 '{_DATASET}' 中有 {r['count']} 个表: {r['tables']}")

    # 2. Cloud Storage
    print("\n[2] CloudStorageTool — upload + list")
    gcs = CloudStorageTool()
    test_content = json.dumps({"test": True, "ts": datetime.now(timezone.utc).isoformat()[:19]})
    r = json.loads(gcs._run(action="upload_text", path="test/healthcheck.json", content=test_content))
    if "error" in r:
        print(f"  ⚠️  上传: {r['error'][:80]}")
    else:
        print(f"  ✅ 上传成功: {r['uri']}")
    r2 = json.loads(gcs._run(action="list", prefix="test/"))
    if "error" not in r2:
        print(f"  ✅ 列表: {r2['count']} 个文件")

    # 3. Vertex AI
    print("\n[3] VertexAITool — generate")
    va = VertexAITool()
    r = json.loads(va._run(action="generate",
                           prompt="用一句话介绍 InsightBridge Global 的业务。",
                           max_tokens=100))
    if "error" in r:
        print(f"  ⚠️  {r['error'][:80]}")
    else:
        print(f"  ✅ 模型={r['model']}")
        print(f"  输出: {r['output'][:120]}")

    # 4. Firestore
    print("\n[4] FirestoreTool — write + read")
    fs = FirestoreTool()
    test_data = json.dumps({"symbol": "ES", "signal": "LONG", "confidence": 72,
                            "source": "healthcheck"})
    r = json.loads(fs._run(action="write", collection="test_healthcheck", data=test_data))
    if "error" in r:
        print(f"  ⚠️  {r['error'][:80]}")
    else:
        doc_id = r["id"]
        print(f"  ✅ 写入成功 id={doc_id}")
        r2 = json.loads(fs._run(action="read", collection="test_healthcheck", doc_id=doc_id))
        print(f"  ✅ 读取成功: {r2.get('data',{}).get('signal')} @ {r2.get('data',{}).get('_created_at')}")

    # 5. Google Sheets
    print("\n[5] GoogleSheetsTool — list sheets")
    sh = GoogleSheetsTool()
    r = json.loads(sh._run(action="list"))
    if "error" in r:
        print(f"  ⚠️  {r['error'][:80]}")
    else:
        print(f"  ✅ Google Drive 中有 {r['count']} 个 Sheets")
        for s in r["sheets"][:3]:
            print(f"     - {s['name']} ({s['id'][:20]}...)")

    print("\n✅ Google Cloud Tool 自检完成")
