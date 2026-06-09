#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from crewai import Agent, Crew, Process, Task
from dotenv import load_dotenv

from agents import _make_llm, _perplexity_llm


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
    BASE_DIR / "system2_claude_simulation" / "data_fetchers" / "real_data.py",
    BASE_DIR / "system2_claude_simulation" / "run_simulation.py",
    BASE_DIR / "system2_claude_simulation" / "pricing_engine.py",
    BASE_DIR / "system1_chatgpt_harness" / "mare_engine" / "api" / "app" / "services" / "policy_engine.py",
    BASE_DIR / "system3_crewai" / "main.py",
    BASE_DIR / "system1_chatgpt_harness" / "run_21d_harness.py",
]


def _read_file_block(path: Path, max_chars: int = 16000) -> str:
    text = path.read_text(encoding="utf-8")
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    return head + "\n\n# ... truncated ...\n\n" + tail


def build_context() -> str:
    sections = []
    for path in FILES_TO_AUDIT:
        sections.append(f"\n## FILE: {path}\n```python\n{_read_file_block(path)}\n```")
    sections.append(
        "\n## AUDIT FOCUS\n"
        "1. Runtime bugs, import/path issues, stale assumptions, dangerous side effects.\n"
        "2. Pricing/math logic issues in MHA current ADR + DSEC historical floor/ceiling integration.\n"
        "3. Risks from duplicate legacy folders, old automation, old launch agents, old push/report scripts.\n"
        "4. Only report real findings. If no finding, say so clearly.\n"
    )
    return "\n".join(sections)


def main() -> int:
    import crewai.memory.storage.kickoff_task_outputs_storage as kickoff_store
    import crewai.utilities.paths as crew_paths

    crew_paths.db_storage_path = lambda: str(CREW_STORE_DIR)
    kickoff_store.db_storage_path = lambda: str(CREW_STORE_DIR)

    llm_primary = _perplexity_llm() or _make_llm(
        "perplexity/sonar-pro", "PERPLEXITY_API_KEY", "deepseek/deepseek-chat", "DEEPSEEK_API_KEY"
    )
    llm_secondary = _make_llm("deepseek/deepseek-chat", "DEEPSEEK_API_KEY", "gpt-4o-mini", "OPENAI_API_KEY")
    llm_writer = _make_llm("gpt-4o-mini", "OPENAI_API_KEY", "perplexity/sonar-pro", "PERPLEXITY_API_KEY")
    llm = llm_primary or llm_secondary or llm_writer
    if llm is None:
        raise RuntimeError("No valid LLM key found for CrewAI code audit.")

    runtime_agent = Agent(
        role="Runtime Bug Auditor",
        goal="Find real runtime bugs, import/path problems, stale code paths, and likely crash points after the June 9 cleanup.",
        backstory="You review Python systems with a production reliability mindset. You do not invent issues.",
        llm=llm_primary or llm,
        verbose=True,
        allow_delegation=False,
    )
    logic_agent = Agent(
        role="Pricing Logic Auditor",
        goal="Check whether pricing logic, demand logic, and guardrails are mathematically and operationally coherent.",
        backstory="You audit revenue management systems, demand signals, and guardrail math carefully.",
        llm=llm_secondary or llm,
        verbose=True,
        allow_delegation=False,
    )
    cleanup_agent = Agent(
        role="Legacy Cleanup Auditor",
        goal="Identify legacy automation, duplicate folders, stale scripts, old learning shells, and old data paths that can interfere with the new system.",
        backstory="You focus on operational cleanliness and removal risk.",
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )
    writer_agent = Agent(
        role="Audit Report Writer",
        goal="Write a concise final audit report with findings first, then cleanup recommendations.",
        backstory="You synthesize technical audits into actionable engineering reports.",
        llm=llm_writer or llm,
        verbose=True,
        allow_delegation=False,
    )

    context_blob = build_context()

    t1 = Task(
        description=(
            "Audit the provided codebase context for runtime bugs. "
            "Return only concrete findings with file references and short explanations. "
            "If no real runtime bug is found, say 'No runtime bug found' and mention residual risk briefly.\n\n"
            + context_blob
        ),
        expected_output="Markdown findings list focused on runtime bugs and crash risks.",
        agent=runtime_agent,
    )
    t2 = Task(
        description=(
            "Audit the provided codebase context for pricing/math/logic issues, especially MHA current ADR, "
            "DSEC floor/ceiling, demand-state integration, and cross-system consistency. "
            "Only report real issues.\n\n"
            + context_blob
        ),
        expected_output="Markdown findings list focused on math, pricing, and business logic risks.",
        agent=logic_agent,
    )
    t3 = Task(
        description=(
            "Audit the provided codebase context and known legacy artifacts for cleanup risk. "
            "Decide what old folders, launch agents, pid files, logs, push scripts, and duplicate code can be safely deleted. "
            "Verify whether the old learning shells were actually cleaned out or whether any stale references remain. "
            "Separate safe-to-delete from should-keep.\n\n"
            + context_blob
        ),
        expected_output="Markdown list with 'safe to delete' and 'should keep' sections.",
        agent=cleanup_agent,
    )
    t4 = Task(
        description=(
            "Combine the three prior audit outputs into one final report. "
            "Order by severity. Findings first. Then cleanup recommendations. "
            "Be concise and concrete."
        ),
        expected_output="Final markdown audit report.",
        agent=writer_agent,
        context=[t1, t2, t3],
    )

    crew = Crew(
        agents=[runtime_agent, logic_agent, cleanup_agent, writer_agent],
        tasks=[t1, t2, t3, t4],
        process=Process.sequential,
        verbose=True,
    )
    result = crew.kickoff()
    report_path = REPORT_DIR / f"crewai_code_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    report_path.write_text(str(result), encoding="utf-8")
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
