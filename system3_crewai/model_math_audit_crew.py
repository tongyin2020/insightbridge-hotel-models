#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from crewai import Agent, Crew, Process, Task
from dotenv import load_dotenv

from agents import _make_llm, _perplexity_llm
from tools.wolfram_tool import WolframAlphaTool


BASE_DIR = Path(__file__).resolve().parent.parent
REPORT_DIR = BASE_DIR / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)
CREW_STORE_DIR = BASE_DIR / ".crewai_storage"
CREW_STORE_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env", override=True)
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")

FILES_TO_AUDIT = [
    BASE_DIR / "hotel_collector" / "dsec_loader.py",
    BASE_DIR / "system2_claude_simulation" / "pricing_engine.py",
    BASE_DIR / "system2_claude_simulation" / "recommendations.py",
    BASE_DIR / "system2_claude_simulation" / "objective_modes.py",
    BASE_DIR / "system2_claude_simulation" / "run_simulation.py",
    BASE_DIR / "system1_chatgpt_harness" / "mare_engine" / "api" / "app" / "services" / "pricing_engine.py",
    BASE_DIR / "system1_chatgpt_harness" / "mare_engine" / "api" / "app" / "services" / "policy_engine.py",
    BASE_DIR / "system1_chatgpt_harness" / "mare_engine" / "api" / "data" / "model_weights.json",
    BASE_DIR / "system2_claude_simulation" / "data" / "model_weights.json",
]


def _read_file_block(path: Path, max_chars: int = 20000) -> str:
    text = path.read_text(encoding="utf-8")
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    return head + "\n\n# ... truncated ...\n\n" + tail


def _recent_anomaly_summary() -> str:
    chunks: list[str] = []
    dbs = [
        ("S2", BASE_DIR / "system2_claude_simulation" / "results.db"),
        ("S3", BASE_DIR / "system3_crewai" / "crewai_results.db"),
    ]
    for label, db_path in dbs:
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            total = conn.execute("SELECT COUNT(*) FROM hourly_runs").fetchone()[0]
            exc = conn.execute("SELECT COUNT(*) FROM hourly_runs WHERE anomaly LIKE '%EXCEPTION%'").fetchone()[0]
            crit = conn.execute("SELECT COUNT(*) FROM hourly_runs WHERE anomaly LIKE '%CRITICAL%'").fetchone()[0]
            warn = conn.execute("SELECT COUNT(*) FROM hourly_runs WHERE anomaly LIKE '%WARN%'").fetchone()[0]
            top_rows = conn.execute(
                """
                SELECT anomaly, COUNT(*) c
                FROM hourly_runs
                WHERE anomaly IS NOT NULL AND TRIM(anomaly) != ''
                GROUP BY anomaly
                ORDER BY c DESC
                LIMIT 10
                """
            ).fetchall()
            conn.close()
        except Exception as exc_err:
            chunks.append(f"## {label}\nFailed to read anomaly summary: {exc_err}")
            continue

        lines = [f"## {label}", f"total={total}, exception={exc}, critical={crit}, warn={warn}"]
        for anomaly, count in top_rows:
            lines.append(f"- {count} :: {anomaly}")
        chunks.append("\n".join(lines))
    return "\n\n".join(chunks)


def build_context() -> str:
    sections: list[str] = []
    for path in FILES_TO_AUDIT:
        sections.append(f"\n## FILE: {path}\n```python\n{_read_file_block(path)}\n```")

    sections.append(
        "\n## AUDIT FOCUS\n"
        "The user asked specifically for a math / model-logic audit after major June 7-8 changes.\n"
        "Distinguish carefully between:\n"
        "1. Real algorithmic flaws or inconsistent formulas.\n"
        "2. Expected guardrail warnings under adversarial/extreme simulation scenarios.\n"
        "3. Pure program/runtime bugs (mention briefly only if they directly affect model logic).\n"
        "\n"
        "Important business assumptions to verify:\n"
        "- Base price now prefers latest MHA ADR, then DSEC ADR, then OTA estimate fallback.\n"
        "- Floor / ceiling now come from DSEC historical bounds by market group: mass (3★+4★) vs luxury (5★).\n"
        "- MHA occupancy is now used as a demand-strength signal and in weights.\n"
        "- competitor_deviation threshold is 20%.\n"
        "- gm_approval threshold is 20%.\n"
        "- Frequent guardrail triggers do not automatically mean code is broken.\n"
        "\n"
        "Recent anomaly summary:\n"
        f"{_recent_anomaly_summary()}\n"
    )
    return "\n".join(sections)


def main() -> int:
    import crewai.memory.storage.kickoff_task_outputs_storage as kickoff_store
    import crewai.utilities.paths as crew_paths

    crew_paths.db_storage_path = lambda: str(CREW_STORE_DIR)
    kickoff_store.db_storage_path = lambda: str(CREW_STORE_DIR)

    llm_wolfram = _make_llm("gpt-4o-mini", "OPENAI_API_KEY", "deepseek/deepseek-chat", "DEEPSEEK_API_KEY")
    llm_deepseek = _make_llm("deepseek/deepseek-chat", "DEEPSEEK_API_KEY", "gpt-4o-mini", "OPENAI_API_KEY")
    llm_crosscheck = _perplexity_llm() or _make_llm(
        "perplexity/sonar-pro", "PERPLEXITY_API_KEY", "gpt-4o-mini", "OPENAI_API_KEY"
    )
    llm = llm_wolfram or llm_deepseek or llm_crosscheck
    if llm is None:
        raise RuntimeError("No valid LLM key found for model math audit.")

    wolfram_tool = WolframAlphaTool()
    context_blob = build_context()

    wolfram_agent = Agent(
        role="Wolfram Math Verification Auditor",
        goal=(
            "Use exact calculation checks to verify whether the pricing formulas, floors/ceilings, "
            "demand normalization, and threshold logic are numerically coherent."
        ),
        backstory=(
            "You are a quantitative verifier. You prefer exact arithmetic, sanity checks, z-score reasoning, "
            "and threshold consistency over vague commentary."
        ),
        tools=[wolfram_tool],
        llm=llm_wolfram or llm,
        verbose=True,
        allow_delegation=False,
    )

    deepseek_agent = Agent(
        role="DeepSeek Algorithm Logic Auditor",
        goal=(
            "Inspect cross-file algorithm consistency across MARE, Director, SelfACQ, MHA/DSEC fusion, "
            "and identify real logic flaws that could destabilize model behavior."
        ),
        backstory=(
            "You review revenue-management and demand models with attention to hidden inconsistencies, "
            "bad weighting, double counting, and guardrail misuse."
        ),
        llm=llm_deepseek or llm,
        verbose=True,
        allow_delegation=False,
    )

    crosscheck_agent = Agent(
        role="Perplexity Cross-Check Auditor",
        goal=(
            "Cross-check the prior two audits, reject false positives, and decide which findings are truly "
            "likely to affect model outputs or create misleading error volume."
        ),
        backstory=(
            "You are a careful reviewer. You separate expected simulation noise from genuine model design defects."
        ),
        llm=llm_crosscheck or llm,
        verbose=True,
        allow_delegation=False,
    )

    writer_agent = Agent(
        role="Final Model Audit Writer",
        goal=(
            "Produce a concise final report in markdown with three sections only: confirmed math/logic issues, "
            "expected-not-a-bug warnings, and priority recommendations."
        ),
        backstory="You write executive-ready technical audit summaries with findings first.",
        llm=llm_crosscheck or llm,
        verbose=True,
        allow_delegation=False,
    )

    t1 = Task(
        description=(
            "Audit the provided code context with special focus on formulas and numeric consistency. "
            "Use the Wolfram tool whenever exact calculation or sanity checking helps. "
            "Check especially: floor/ceiling construction, market-group averaging, demand z-score logic, "
            "MHA vs DSEC role split, threshold logic for competitor_deviation and gm_approval, and whether "
            "the formulas could systematically generate false alarms.\n\n"
            + context_blob
        ),
        expected_output="Markdown findings list focused on numeric / mathematical verification.",
        agent=wolfram_agent,
    )

    t2 = Task(
        description=(
            "Audit the same code context for algorithmic consistency. "
            "Check whether the same business logic is implemented consistently across System 1, System 2, and System 3; "
            "whether signals are double counted; whether weights or guardrails could cause unstable outputs; and whether "
            "any recent June 7-8 changes likely introduced logic regressions.\n\n"
            + context_blob
        ),
        expected_output="Markdown findings list focused on algorithm and model-logic issues.",
        agent=deepseek_agent,
    )

    t3 = Task(
        description=(
            "Cross-check the two prior outputs against the code context. "
            "Keep only likely-true findings. Explicitly mark which frequent anomalies are expected outcomes of "
            "stress testing rather than bugs. If a prior finding looks weak or speculative, reject it."
        ),
        expected_output="Markdown list of confirmed findings and rejected false positives.",
        agent=crosscheck_agent,
        context=[t1, t2],
    )

    t4 = Task(
        description=(
            "Write the final report. "
            "Section 1: confirmed math/logic issues only. "
            "Section 2: expected guardrail/stress-test warnings that should not be mistaken for bugs. "
            "Section 3: top-priority recommendations in order. "
            "Be concrete, concise, and avoid generic advice."
        ),
        expected_output="Final markdown audit report.",
        agent=writer_agent,
        context=[t1, t2, t3],
    )

    crew = Crew(
        agents=[wolfram_agent, deepseek_agent, crosscheck_agent, writer_agent],
        tasks=[t1, t2, t3, t4],
        process=Process.sequential,
        verbose=True,
    )
    result = crew.kickoff()
    report_path = REPORT_DIR / f"model_math_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    report_path.write_text(str(result), encoding="utf-8")
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
