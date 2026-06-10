from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import json


@dataclass
class MAMLParams:
    """Portable parameter container for future Layer 4 meta-learning."""

    param_dict: dict = field(default_factory=dict)
    feature_schema_version: str = "v1.0"
    market_tier: str = "unknown"
    n_training_samples: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "param_dict": self.param_dict,
                "feature_schema_version": self.feature_schema_version,
                "market_tier": self.market_tier,
                "n_training_samples": self.n_training_samples,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, payload: str) -> "MAMLParams":
        return cls(**json.loads(payload))


class MAMLCompatibleModel(ABC):
    """Common interface reserved for future MAML Layer 4 integration."""

    @abstractmethod
    def get_params(self) -> MAMLParams:
        raise NotImplementedError

    @abstractmethod
    def set_params(self, params: MAMLParams) -> None:
        raise NotImplementedError

    def fast_adapt(self, support_set: list, n_steps: int = 5) -> "MAMLCompatibleModel":
        raise NotImplementedError(
            "MAML fast_adapt is reserved only. "
            "Enable when hotel_count >= 200 or market_tiers >= 3 or new_hotels_30d >= 5."
        )

    def get_feature_schema(self) -> dict:
        return {
            "version": "v1.0",
            "features": [
                "nationality",
                "booking_channel",
                "visit_frequency",
                "length_of_stay",
                "total_spend_mop",
                "guest_type",
                "age_band",
                "lead_time_days",
                "adr_tier",
                "loyalty_tier",
                "gaming_flag",
                "payment_method",
            ],
            "derived": [
                "spend_per_night",
                "is_high_value",
                "is_oversea",
                "is_digital_native",
                "is_gaming_segment",
                "channel_loyalty_match",
            ],
        }
