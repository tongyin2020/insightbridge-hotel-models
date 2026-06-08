#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from model_refinement import (
    EXTREME_CATEGORIES,
    compute_dual_score,
    parse_pct_text,
)
from system2_claude_simulation.data_fetchers.scenario_engine import SCENARIO_MAP

BASE_DIR = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026")
REPORTS_DIR = BASE_DIR / "reports"
SYS2_DB = BASE_DIR / "system2_claude_simulation" / "results.db"
SYS3_DB = BASE_DIR / "system3_crewai" / "crewai_results.db"
SYS1_DIR = BASE_DIR / "hotel_model_staging_output"


def _scenario_band(category: str) -> str:
    return "extreme" if category in EXTREME_CATEGORIES else "normal"


def _heuristics(model_family: str, system_name: str) -> tuple[float, float, float]:
    real = 0.80 if system_name == "S1" else 0.74 if system_name == "S2" else 0.79
    explain = {"MARE": 0.86, "DIRECTOR": 0.92, "SELFACQ": 0.84}[model_family]
    cost = {"S1": 0.88, "S2": 0.90, "S3": 0.82}[system_name]
    return real, explain, cost


def _append_bucket(
    buckets: dict[tuple[str, str, str], list[dict]],
    system_name: str,
    model_family: str,
    category: str,
    *,
    uplift_pct: float,
    anomaly_text: str,
) -> None:
    buckets[(system_name, model_family, _scenario_band(category))].append(
        {
            "uplift_pct": uplift_pct,
            "failure": 1 if "EXCEPTION" in anomaly_text else 0,
            "anomaly": 1 if anomaly_text.strip() else 0,
        }
    )


def load_s2_s3_rows(path: Path, system_name: str, buckets: dict[tuple[str, str, str], list[dict]]) -> None:
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT model_type, input_json, output_json, exp_lift, anomaly FROM hourly_runs").fetchall()
    conn.close()
    for model_type, input_json, output_json, exp_lift, anomaly in rows:
        try:
            input_obj = json.loads(input_json or "{}")
        except Exception:
            input_obj = {}
        try:
            output_obj = json.loads(output_json or "{}")
        except Exception:
            output_obj = {}

        scenario_name = input_obj.get("scenario") or output_obj.get("scenario")
        category = output_obj.get("scenario_category") or input_obj.get("scenario_category")
        if not category and scenario_name in SCENARIO_MAP:
            category = SCENARIO_MAP[scenario_name].category
        category = category or "normal"

        if "MARE" in model_type:
            model_family = "MARE"
            uplift_pct = parse_pct_text(output_obj.get("expected_revenue_lift") or exp_lift)
        elif "DIRECTOR" in model_type:
            model_family = "DIRECTOR"
            uplift_pct = parse_pct_text(output_obj.get("expected_revenue_lift") or exp_lift)
        else:
            model_family = "SELFACQ"
            ota_profit = float(output_obj.get("ota_net_profit") or output_obj.get("ota_net_revenue") or 0.0)
            direct_profit = float(output_obj.get("direct_net_profit_after_cac") or output_obj.get("direct_net_revenue") or 0.0)
            uplift_pct = ((direct_profit - ota_profit) / ota_profit * 100.0) if ota_profit > 0 else 0.0

        _append_bucket(buckets, system_name, model_family, category, uplift_pct=uplift_pct, anomaly_text=anomaly or "")


def load_s1_rows(buckets: dict[tuple[str, str, str], list[dict]]) -> None:
    jsonls = sorted(SYS1_DIR.glob("run_*.jsonl"))
    if not jsonls:
        return
    latest = jsonls[-1]
    for line in latest.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        model = row.get("model", "")
        scenario = row.get("scenario") or {}
        if isinstance(scenario, str):
            scenario_name = scenario
            category = SCENARIO_MAP.get(scenario_name).category if scenario_name in SCENARIO_MAP else "normal"
        else:
            scenario_name = scenario.get("name")
            category = scenario.get("category")
            if not category and scenario_name in SCENARIO_MAP:
                category = SCENARIO_MAP[scenario_name].category
        category = category or "normal"
        issues = row.get("issues") or []
        anomaly_text = "; ".join(str(x) for x in issues)
        result = row.get("result") or {}

        if model == "mare":
            model_family = "MARE"
            uplift_pct = parse_pct_text(result.get("expected_revenue_lift"))
        elif model == "director":
            model_family = "DIRECTOR"
            uplift_pct = parse_pct_text(result.get("expected_revenue_lift"))
        else:
            model_family = "SELFACQ"
            ota_profit = float(result.get("ota_net_profit") or result.get("ota_net_revenue") or 0.0)
            direct_profit = float(result.get("direct_net_profit_after_cac") or result.get("direct_net_revenue") or 0.0)
            uplift_pct = ((direct_profit - ota_profit) / ota_profit * 100.0) if ota_profit > 0 else 0.0

        _append_bucket(buckets, "S1", model_family, category, uplift_pct=uplift_pct, anomaly_text=anomaly_text)


def build_report() -> str:
    buckets: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    load_s2_s3_rows(SYS2_DB, "S2", buckets)
    load_s2_s3_rows(SYS3_DB, "S3", buckets)
    load_s1_rows(buckets)

    lines = [
        "# InsightBridge 三模型双评分卡",
        "",
        f"生成时间：{datetime.now():%Y-%m-%d %H:%M}",
        "",
        "| System | Model | 场景带 | 样本 | 总分 | 收益提升 | 失败率 | 异常率 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]

    ranking = []
    for (system_name, model_family, band), items in sorted(buckets.items()):
        sample_size = len(items)
        uplift = sum(x["uplift_pct"] for x in items) / max(sample_size, 1)
        failure = sum(x["failure"] for x in items) / max(sample_size, 1) * 100.0
        anomaly = sum(x["anomaly"] for x in items) / max(sample_size, 1) * 100.0
        real_ratio, explain_ratio, cost_ratio = _heuristics(model_family, system_name)
        total_score = compute_dual_score(
            profit_uplift_pct=uplift,
            failure_rate_pct=failure,
            anomaly_rate_pct=anomaly,
            real_data_ratio=real_ratio,
            explainability_ratio=explain_ratio,
            runtime_cost_ratio=cost_ratio,
        )
        lines.append(
            f"| {system_name} | {model_family} | {band} | {sample_size} | {total_score:.2f} | "
            f"{uplift:.2f}% | {failure:.2f}% | {anomaly:.2f}% |"
        )
        ranking.append((total_score, system_name, model_family, band))

    lines.extend(
        [
            "",
            "## 当前最佳组合",
            "",
        ]
    )
    for idx, (score, system_name, model_family, band) in enumerate(sorted(ranking, reverse=True)[:6], start=1):
        lines.append(f"{idx}. `{system_name} {model_family} {band}` — {score:.2f}")

    return "\n".join(lines) + "\n"


def main() -> int:
    report = build_report()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = REPORTS_DIR / f"three_model_dual_scorecard_{stamp}.md"
    latest = REPORTS_DIR / "three_model_dual_scorecard_latest.md"
    out_path.write_text(report, encoding="utf-8")
    latest.write_text(report, encoding="utf-8")
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
