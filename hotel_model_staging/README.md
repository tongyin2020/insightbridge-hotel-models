# Hotel Model 21-Day Staging Harness

This package is meant to run on your Mac from a Python terminal before hotel pilot deployment.

It does four things:
- pulls public-market signals with Firecrawl
- pulls OTA prices with MakCorps
- runs the MARE model kernel directly
- runs the DirectorAI model kernel directly

It does not require standing up the full auth, DB, or frontend stack.

## What it tests

- normal scenarios
- weekend pickup
- festival surge
- soft demand
- competitor pressure
- high inventory pressure
- near sellout
- fairness stress
- low satisfaction conflict
- dirty data
- conflicting signals

The goal is pre-pilot hardening, not real-hotel calibration.

## Before you run

1. Make sure your Mac has Python 3.10+.
2. Put your three projects on disk.
3. Copy `.env.example` to `.env`.
4. Fill in:
   - `FIRECRAWL_API_KEY`
   - `MAKCORPS_API_KEY`
   - `AGENTOPS_API_KEY`
   - `MARE_REPO_PATH`
   - `DIRECTOR_REPO_PATH`

## Install

```bash
cd /path/to/hotel_model_staging
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Quick smoke run

This runs one cycle and exits.

```bash
python3 run_21d_harness.py --dry-run
```

## Full 21-day run

Default is once per hour for 21 days.

```bash
python3 run_21d_harness.py
```

## Shorter validation run

Run every 10 minutes for 6 cycles:

```bash
python3 run_21d_harness.py --cycles 6 --interval-seconds 600
```

## Output

The harness writes:
- `run_<timestamp>.jsonl`
- `summary_<timestamp>.json`

These go under `OUTPUT_DIR`, which defaults to `./hotel_model_staging_output`.

## Important limitations

- Firecrawl currently feeds public web signals, not internal hotel data.
- MakCorps feeds OTA price intelligence only.
- Occupancy, inventory, cancellation, CLV, churn, repurchase, and satisfaction are still scenario-based simulation inputs.
- The harness runs the two backend model kernels directly. It does not boot the frontend as part of the continuous run.

## If you want the frontend checked too

Use this separately during daytime review:

```bash
cd /path/to/insightbridge-frontend
npm install
npm run dev
```

Then compare its displayed price logic against the backend run logs.
