from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


class _MissingMAREML(Exception):
    pass


def _import_or_explain():
    try:
        from mare_ml.model_inference import MAREDemandInference
        return MAREDemandInference
    except Exception as exc:
        raise _MissingMAREML(
            "Unable to load mare_ml.model_inference. "
            "Install lightgbm and train a first model before enabling MARE_USE_ML=1."
        ) from exc


_cached = None


def _get_class():
    global _cached
    if _cached is None:
        _cached = _import_or_explain()
    return _cached


class MAREDemandInference:
    @classmethod
    def singleton(cls):
        return _get_class().singleton()
