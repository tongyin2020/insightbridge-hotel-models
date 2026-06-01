"""Objective modes for Macau 4-5 star direct-booking strategy."""

from enum import Enum
from dataclasses import dataclass


class ObjectiveMode(str, Enum):
    MAXIMIZE_REVENUE = "maximize_revenue"
    MAXIMIZE_PROFIT = "maximize_profit"
    MAXIMIZE_REVPAR = "maximize_revpar"
    MAXIMIZE_DIRECT_MIX = "maximize_direct_mix"


@dataclass
class ObjectiveWeights:
    demand_weight: float
    inventory_weight: float
    competition_weight: float
    profit_weight: float
    direct_bias: float
    bundle_aggressiveness: float


OBJECTIVE_PROFILES = {
    ObjectiveMode.MAXIMIZE_REVENUE: ObjectiveWeights(
        demand_weight=1.10,
        inventory_weight=1.00,
        competition_weight=0.65,
        profit_weight=0.95,
        direct_bias=0.10,
        bundle_aggressiveness=0.80,
    ),
    ObjectiveMode.MAXIMIZE_PROFIT: ObjectiveWeights(
        demand_weight=0.85,
        inventory_weight=0.95,
        competition_weight=0.55,
        profit_weight=1.25,
        direct_bias=0.20,
        bundle_aggressiveness=0.60,
    ),
    ObjectiveMode.MAXIMIZE_REVPAR: ObjectiveWeights(
        demand_weight=1.00,
        inventory_weight=1.15,
        competition_weight=0.70,
        profit_weight=1.00,
        direct_bias=0.10,
        bundle_aggressiveness=0.95,
    ),
    ObjectiveMode.MAXIMIZE_DIRECT_MIX: ObjectiveWeights(
        demand_weight=0.80,
        inventory_weight=0.85,
        competition_weight=0.45,
        profit_weight=1.05,
        direct_bias=0.45,
        bundle_aggressiveness=1.15,
    ),
}


def get_objective_weights(mode: ObjectiveMode) -> ObjectiveWeights:
    return OBJECTIVE_PROFILES.get(mode, OBJECTIVE_PROFILES[ObjectiveMode.MAXIMIZE_REVENUE])


def apply_objective_adjustment(base_adjustments: dict, mode: ObjectiveMode) -> dict:
    w = get_objective_weights(mode)
    return {
        "demand": base_adjustments.get("demand", 0) * w.demand_weight,
        "inventory": base_adjustments.get("inventory", 0) * w.inventory_weight,
        "competition": base_adjustments.get("competition", 0) * w.competition_weight,
        "profit": base_adjustments.get("profit", 0) * w.profit_weight,
        "direct_bias": w.direct_bias,
        "bundle_aggressiveness": w.bundle_aggressiveness,
    }
