from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

BASE = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026")
FINAL = BASE / "final_three_models_release_20260625"

TARGETS = {
    "MARE": FINAL / "01_MARE_Final" / "output_samples",
    "Director": FINAL / "02_Director_Final" / "output_samples",
    "SelfACQ": FINAL / "03_SelfACQ_Final" / "output_samples",
}


def latest_json(path: Path) -> Path | None:
    files = sorted(path.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def fmt_age(path: Path | None) -> str:
    if path is None:
        return "missing"
    minutes = (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds() / 60.0
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes:.1f}m"
    return f"{minutes / 60.0:.1f}h"


def summarize(path: Path | None) -> tuple[str, str]:
    if path is None:
        return "missing", "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        summary = payload.get("summary", {})
        return str(summary.get("samples", "N/A")), str(summary.get("anomalies", "N/A"))
    except Exception:
        return "unknown", "unknown"


def main() -> int:
    print("InsightBridge Final Three Models Check")
    print("=" * 60)
    all_ok = True
    for name, folder in TARGETS.items():
        latest = latest_json(folder)
        samples, anomalies = summarize(latest)
        print(f"[{name}]")
        print(f"latest_output: {latest.name if latest else 'missing'}")
        print(f"output_age: {fmt_age(latest)}")
        print(f"samples: {samples}")
        print(f"anomalies: {anomalies}")
        if latest is None:
            all_ok = False
        print("-" * 60)
    print("Overall:", "Final three models ready." if all_ok else "At least one final model has no output yet.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
