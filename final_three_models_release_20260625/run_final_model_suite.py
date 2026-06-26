from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the final three-model suite")
    parser.add_argument("--hotel-id", default=None)
    parser.add_argument("--sim-hour", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mare = _load_module(BASE / "01_MARE_Final" / "run_final_mare.py", "final_mare")
    director = _load_module(BASE / "02_Director_Final" / "run_final_director.py", "final_director")
    selfacq = _load_module(BASE / "03_SelfACQ_Final" / "run_final_selfacq.py", "final_selfacq")

    mare_path = mare.run_final_mare(hotel_id=args.hotel_id, sim_hour=args.sim_hour)
    director_path = director.run_final_director(hotel_id=args.hotel_id, sim_hour=args.sim_hour)
    selfacq_path = selfacq.run_final_selfacq(hotel_id=args.hotel_id, sim_hour=args.sim_hour)

    print("Final three-model suite complete.")
    print(f"MARE output: {mare_path}")
    print(f"Director output: {director_path}")
    print(f"SelfACQ output: {selfacq_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
