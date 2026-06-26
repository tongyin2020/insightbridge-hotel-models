from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

BASE = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026")
FINAL = BASE / "final_three_models_release_20260625"
REPORTS = BASE / "reports"
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
OUT = REPORTS / f"final_three_models_crewai_audit_{STAMP}.md"

for env_file in [
    BASE / ".env",
    FINAL / "embedded_runtime" / "system3_crewai" / ".env",
    FINAL / "embedded_runtime" / "system2_claude_simulation" / ".env",
]:
    if env_file.exists():
        load_dotenv(env_file, override=False)


def _snippet(path: Path, start: int = 1, end: int = 220) -> str:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    selected = lines[start - 1 : end]
    rendered = "\n".join(f"{i:04d}: {line}" for i, line in enumerate(selected, start))
    return f"\n### FILE: {path}\n```python\n{rendered}\n```\n"


def build_dossier() -> str:
    parts = []
    parts.append(
        """Project scope:
- Only audit the current final three-model runtime.
- Ignore deleted desktop legacy folders; old names inside embedded_runtime are compatibility wrappers unless they break current execution.

Local self-check results already observed:
- compileall passed for final_three_models_release_20260625
- full three-model check passed: 76 samples each, anomalies=0
- single real hotel smoke test passed using hotel_id MAC_3ST_EMPE_059
- invalid hotel_id previously returned samples=0 silently; this has just been fixed by raising ValueError in common_runtime.select_hotels()
- current external weakness is data fetch fallback: weather DNS unresolved, so weather and some live factors fall back to defaults/simulated values

Audit instructions:
- focus on real code/runtime bugs, logic inconsistencies, silent failure modes, missing validation, risky defaults, and operational fragility
- do not spend time critiquing deleted legacy framework
- report only concrete findings with file references
"""
    )
    files = [
        (FINAL / "common_runtime.py", 1, 220),
        (FINAL / "01_MARE_Final" / "run_final_mare.py", 1, 120),
        (FINAL / "02_Director_Final" / "run_final_director.py", 1, 120),
        (FINAL / "03_SelfACQ_Final" / "run_final_selfacq.py", 1, 120),
        (FINAL / "embedded_runtime" / "system2_claude_simulation" / "pricing_engine.py", 1, 260),
        (FINAL / "embedded_runtime" / "hotel_collector" / "hotel_data_collector.py", 1, 240),
    ]
    for path, start, end in files:
        parts.append(_snippet(path, start, end))
    return "\n".join(parts)


def main() -> int:
    from crewai import Agent, Crew, LLM, Process, Task

    REPORTS.mkdir(parents=True, exist_ok=True)
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        raise RuntimeError("OPENAI_API_KEY not found in loaded env files")

    llm = LLM(
        model="gpt-4o-mini",
        api_key=openai_key,
        temperature=0.1,
        max_tokens=3000,
        timeout=120,
    )

    dossier = build_dossier()

    runtime_agent = Agent(
        role="Runtime Auditor",
        goal="Find concrete runtime and validation bugs in the final three-model execution path.",
        backstory="You are a senior Python reliability engineer who prioritizes silent failure modes, broken assumptions, and execution-path defects.",
        llm=llm,
        verbose=False,
        allow_delegation=False,
    )
    pricing_agent = Agent(
        role="Pricing Logic Auditor",
        goal="Find mathematical or pricing-logic mistakes in MARE and Director runtime logic.",
        backstory="You are a revenue-management quant auditor focused on model logic, guardrails, and incorrect fallbacks.",
        llm=llm,
        verbose=False,
        allow_delegation=False,
    )
    data_agent = Agent(
        role="Data Pipeline Auditor",
        goal="Find data-source, ETL, and signal-quality bugs that could distort outputs.",
        backstory="You are a hotel-data systems auditor who checks how real signals degrade, fall back, or silently go stale.",
        llm=llm,
        verbose=False,
        allow_delegation=False,
    )
    synthesis_agent = Agent(
        role="Lead Code Reviewer",
        goal="Merge the other auditors' findings into one concise, evidence-based audit report.",
        backstory="You are the final reviewer. You keep only concrete findings and label residual risks separately from true bugs.",
        llm=llm,
        verbose=False,
        allow_delegation=False,
    )

    task1 = Task(
        description=(
            dossier
            + "\nProduce up to 5 concrete runtime/program bugs or validation defects. "
              "Each item must include severity, file path, and short explanation."
        ),
        expected_output="A concise list of runtime findings with file references.",
        agent=runtime_agent,
        markdown=True,
    )
    task2 = Task(
        description=(
            dossier
            + "\nProduce up to 5 concrete pricing/math/logic findings. "
              "Only include issues that can plausibly distort business output."
        ),
        expected_output="A concise list of pricing or mathematical findings with file references.",
        agent=pricing_agent,
        markdown=True,
    )
    task3 = Task(
        description=(
            dossier
            + "\nProduce up to 5 concrete data/signal/operational findings. "
              "Distinguish between code bugs and external dependency weakness."
        ),
        expected_output="A concise list of data-pipeline findings with file references.",
        agent=data_agent,
        markdown=True,
    )
    task4 = Task(
        description=(
            "Combine the three prior audit outputs into one final report. "
            "Order findings by severity. Separate true bugs from residual external risks. "
            "If no major code bugs remain, say that explicitly."
        ),
        expected_output="Final markdown audit report with Findings, Residual Risks, and Verdict sections.",
        agent=synthesis_agent,
        context=[task1, task2, task3],
        markdown=True,
        output_file=str(OUT),
    )

    crew = Crew(
        agents=[runtime_agent, pricing_agent, data_agent, synthesis_agent],
        tasks=[task1, task2, task3, task4],
        process=Process.sequential,
        verbose=False,
        memory=False,
        planning=False,
        output_log_file=str(REPORTS / f"final_three_models_crewai_audit_run_{STAMP}.log"),
    )

    result = crew.kickoff()
    print(result)
    print(f"\nAudit report saved to: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
