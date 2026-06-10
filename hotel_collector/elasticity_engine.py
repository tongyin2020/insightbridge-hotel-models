"""
InsightBridge Price Elasticity Engine  v1.0
============================================
第二阶段：静态澳门基线驱动的 RevPAR / 轻量 TRevPAR 最优化
第三阶段：接入 MARE 新自学习层，按市场档位做小幅在线校准

核心逻辑
---------
  price_premium       = (candidate_price - market_price) / market_price
  predicted_occupancy = base_occ × (1 - elasticity × price_premium)
  RevPAR              = candidate_price × predicted_occupancy
  TRevPAR(light)      = (candidate_price + ancillary_per_occ) × predicted_occupancy

枚举 [floor, ceiling] 内所有价格点（步长 10 MOP），
默认选轻量 TRevPAR（房费 + 非房贡献）最大的价格。

弹性系数分层（模拟阶段，基于澳门市场同类研究）
------------------------------------------------
  3★ 全澳门        ≈ 0.82  (散客/OTA为主，高度价格敏感)
  4★ 半岛/内港/新口岸 ≈ 0.63  (商旅混合市场)
  4★ 路氹/氹仔     ≈ 0.55  (路氹城效应，目的地属性)
  5★ 半岛          ≈ 0.42  (豪华商旅，品牌忠诚度高)
  5★ 路氹赌场度假村 ≈ 0.28  (一体化目的地，高粘性)

季节乘数（需求越旺，弹性越低——旺季涨价客户流失更少）
  super_peak  × 0.45   (农历新年/五一黄金周/澳门格兰披治大赛车)
  peak        × 0.65   (节假日/长周末)
  normal      × 1.00
  low         × 1.30   (淡季，客户选择余地大)
"""

from __future__ import annotations
import logging
import json
import os
from pathlib import Path
from typing import NamedTuple
from functools import lru_cache

log = logging.getLogger("elasticity")
_V6_PROFILE_PATH = Path(__file__).resolve().parent.parent / "共用_HROS_V6引擎" / "hotel_profiles_v6.json"
_MACAU_ANCILLARY_PATH = Path(__file__).resolve().parent / "macau_ancillary_profiles.json"
_MACAU_REVENUE_PROFILE_PATH = Path(__file__).resolve().parent / "macau_static_revenue_profiles.json"

# ── 星级价格护栏（MOP）────────────────────────────────────────────────────────
_PRICE_FLOOR   = {3: 680,  4: 750,  5: 1200}
# 调优(2026-06-01): 3★ 1600→1250 (DSEC历史最高价约1200，+5%缓冲)
#                  4★ 3500→2000 (澳门4★豪华上限；路氹最高端亦不超2000)
#                  5★ 保持8000（高端度假村特大活动可到此水平）
# 修复(2026-06-02): 3★ 420→680 (防止OTA低价拉低搜索起点; DSEC 3★ ADR ~950×0.70=665→取整680)
_PRICE_CEILING = {3: 1250, 4: 2000, 5: 8000}

_DEFAULT_ANCILLARY_RATIO = 0.45
_DEFAULT_ANCILLARY_MARGIN = 0.30
_DEFAULT_ELASTICITY = 1.0
_DEFAULT_MAX_PRICE_PREMIUM = 0.0
_DEFAULT_OPTIMAL_OCCUPANCY = 0.80

try:
    from mare_ml_layer import choose_adjustments as _ml_choose_adjustments
    _ML_OK = True
except Exception:
    _ML_OK = False
    def _ml_choose_adjustments(*args, **kwargs):
        return None

try:
    from mare_ml_layer import MareMAMLAdapter as _MareMAMLAdapter
    from maml_reserved import build_maml_metadata as _build_maml_metadata
    _MAML_RESERVED_OK = True
except Exception:
    _MAML_RESERVED_OK = False
    _MareMAMLAdapter = None
    def _build_maml_metadata(**kwargs):
        return {
            "maml_reserved": False,
            "maml_layer4_enabled": False,
            "maml_fast_adapt_used": False,
            "maml_market_tier": "unknown",
            "maml_feature_schema_version": "v1.0",
            "maml_profile_name": "unknown",
            "maml_state_version": 0,
            "maml_meta_hotel_id_hash": None,
            "maml_readiness": {
                "hotel_count": 0,
                "market_tier_count": 0,
                "new_hotels_30d": 0,
                "activation_thresholds": {
                    "hotel_count": 200,
                    "market_tier_count": 3,
                    "new_hotels_30d": 5,
                },
                "layer4_ready": False,
                "layer4_enabled": False,
            },
            "ml_layer_active": False,
        }

# 按现有76酒店名单，对澳门5★综合度假村做显式识别。
_INTEGRATED_RESORT_IDS = {
    "MAC_5DX_WYNN_002",  # 永利皇宫
    "MAC_5DX_GLPA_009",  # 上葡京综合度假村
    "MAC_5DX_MGMC_010",  # 美狮美高梅
    "MAC_5ST_VENE_013",  # 威尼斯人
    "MAC_5ST_GALX_014",  # 银河
    "MAC_5ST_CONA_015",  # 康莱德
    "MAC_5ST_MORP_016",  # 摩珀斯
    "MAC_5ST_STMR_017",  # 瑞吉
    "MAC_5ST_RITZ_018",  # 丽思卡尔顿
    "MAC_5ST_JWMR_019",  # JW万豪
    "MAC_5ST_OKUR_020",  # 大仓
    "MAC_5ST_BANY_021",  # 悦榕庄
    "MAC_5ST_HYAT_022",  # 君悦
    "MAC_5ST_ANDA_023",  # 安达仕
    "MAC_5ST_LGRD_024",  # 伦敦人名汇
    "MAC_5ST_BROD_032",  # 百老汇酒店
    "MAC_5ST_TRSN_040",  # 新濠天地翠湖
}


@lru_cache(maxsize=1)
def _load_v6_profiles() -> dict[str, dict]:
    try:
        return json.loads(_V6_PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _load_macau_ancillary_profiles() -> dict[str, dict]:
    try:
        raw = json.loads(_MACAU_ANCILLARY_PATH.read_text(encoding="utf-8"))
        return raw.get("profiles") or {}
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _load_macau_revenue_profiles() -> dict[str, dict]:
    try:
        raw = json.loads(_MACAU_REVENUE_PROFILE_PATH.read_text(encoding="utf-8"))
        return raw.get("profiles") or {}
    except Exception:
        return {}


class ElasticityResult(NamedTuple):
    optimal_price:        float   # RevPAR 最优推荐价 (MOP)
    predicted_occupancy:  float   # 该价格下预测入住率 [0,1]
    predicted_revpar:     float   # 预测 RevPAR (MOP)
    baseline_revpar:      float   # 以市场价出售时的 RevPAR 基准
    predicted_trevpar:    float   # 预测 TRevPAR (房费+非房)
    baseline_trevpar:     float   # 基准 TRevPAR
    true_lift_pct:        float   # (optimal_trevpar - baseline_trevpar) / baseline_trevpar
    revpar_lift_pct:      float   # (optimal_revpar - baseline_revpar) / baseline_revpar
    elasticity_used:      float   # 本次使用的弹性系数
    elasticity_profile:   str     # 采用的澳门静态弹性档位
    max_price_premium:    float   # 相对市场价的最高溢价上限
    optimal_occupancy:    float   # 收益最大化目标入住率
    ml_enabled:           bool    # 是否启用 MARE 自学习层
    ml_elasticity_multiplier: float  # 对静态弹性的学习修正
    ml_premium_delta:     float   # 对价格溢价上限的学习修正
    ml_occupancy_delta:   float   # 对最优入住率目标的学习修正
    ml_state_version:     int     # 自学习状态版本
    ancillary_profile:    str     # 采用的澳门总贡献参数档位
    ancillary_ratio_used: float   # 当前使用的非房/房费比
    ancillary_margin_used: float  # 当前使用的非房毛利率
    ancillary_per_occ:    float   # 每卖出1间夜对应的非房贡献(MOP)
    data_source:          str     # "simulated" | "fitted_pms"
    search_steps:         int     # 枚举了多少个价格点
    maml_reserved:        bool    # 是否已预留 Layer 4 MAML 接口
    maml_layer4_enabled:  bool    # 当前是否启用 Layer 4（v3.2 固定 False）
    maml_fast_adapt_used: bool    # 当前是否用了 fast_adapt（v3.2 固定 False）
    maml_market_tier:     str     # 未来元学习所属市场段
    maml_feature_schema_version: str  # 统一特征 schema 版本
    maml_profile_name:    str     # 当前 MARE 画像名称
    maml_state_version:   int     # MAML 预留层使用的状态版本
    maml_meta_hotel_id_hash: str | None  # 未来跨酒店匿名标识
    maml_readiness:       dict    # Layer 4 启用条件状态
    ml_layer_active:      bool    # 当前在线自学习层是否生效


class ElasticityEngine:
    """
    价格弹性引擎。

    先使用澳门静态基线，再叠加 MARE 自学习层的小幅修正。
    """

    def __init__(self):
        self._data_source = "macau_static"

    # ──────────────────────────────────────────────────────────────────────────
    # 公共 API
    # ──────────────────────────────────────────────────────────────────────────

    def optimize(
        self,
        candidate_price:  float,
        market_price:     float,
        star:             int,
        district:         str   = "NAPE",
        demand_level:     str   = "NORMAL",
        season:           str   = "normal",
        hotel_id:         str   = None,
    ) -> ElasticityResult:
        """
        在价格护栏范围内枚举，返回轻量 TRevPAR 最优价格及预测入住率。

        参数
        ----
        candidate_price : MARE 引擎推荐价（作为搜索起点参考）
        market_price    : 当前市场均价（竞争对手基准）
        star            : 酒店星级 (3/4/5)
        district        : 区域代码 (COT/TAIPA/NAPE/INNER/PENINSULA)
        demand_level    : 需求档位 (HIGH/NORMAL/LOW)
        season          : 季节 (super_peak/peak/normal/low)
        hotel_id        : 酒店ID（有真实弹性系数时优先使用）
        """
        if candidate_price <= 0:
            candidate_price = float(_PRICE_FLOOR.get(star, 420))

        if market_price <= 0:
            market_price = candidate_price
        if market_price <= 0:
            market_price = float(_PRICE_FLOOR.get(star, 420))

        (
            elasticity_profile,
            elasticity,
            max_price_premium,
            optimal_occupancy,
            ml_enabled,
            ml_elasticity_multiplier,
            ml_premium_delta,
            ml_occupancy_delta,
            ml_state_version,
        ) = self._get_revenue_profile(
            star=star,
            hotel_id=hotel_id,
            season=season,
            demand_level=demand_level,
        )
        maml_adapter = _MareMAMLAdapter(elasticity_profile) if _MareMAMLAdapter else None
        maml_meta = _build_maml_metadata(
            hotel_id=hotel_id,
            star=star,
            profile_name=elasticity_profile,
            state_version=ml_state_version,
            ml_enabled=ml_enabled,
            model=maml_adapter if maml_adapter else type("FallbackModel", (), {"get_feature_schema": lambda self: {"version": "v1.0"}})(),
        )
        base_occ   = optimal_occupancy
        hotel_anchor = self._get_hotel_anchor(hotel_id)

        floor_p = _PRICE_FLOOR.get(star, 420)
        ceil_p  = _PRICE_CEILING.get(star, 8000)

        # 搜索范围：价格下限允许适度折价；价格上限严格遵守静态溢价表
        search_lo = max(floor_p, market_price * 0.70)
        search_hi = min(ceil_p, market_price * (1.0 + max_price_premium))
        if search_hi < search_lo:
            search_lo = max(floor_p, search_hi * 0.85)

        ancillary_profile, ancillary_ratio, ancillary_margin = self._get_ancillary_profile(star, hotel_id)
        ancillary_per_occ = market_price * ancillary_ratio * ancillary_margin

        best_objective = -1.0
        best_revpar = -1.0
        best_trevpar = -1.0
        best_price  = market_price
        best_occ    = base_occ
        steps       = 0

        price = search_lo
        while price <= search_hi + 1:
            premium = (price - market_price) / market_price
            occ_raw  = max(0.10, base_occ * (1.0 - elasticity * premium))
            # 超过最优入住率后，边际收益递减，因此目标函数只计算到最优点为止。
            occ_effective = min(occ_raw, optimal_occupancy)
            revpar  = price * occ_raw
            trevpar = price * occ_effective + ancillary_per_occ * occ_effective
            if trevpar > best_objective:
                best_objective = trevpar
                best_revpar = revpar
                best_trevpar = trevpar
                best_price  = price
                best_occ    = occ_raw
            price += 10
            steps += 1

        best_price = round(best_price / 10) * 10

        # 基准：以市场价出售的 RevPAR
        baseline_revpar = market_price * base_occ
        baseline_trevpar = baseline_revpar + ancillary_per_occ * base_occ
        total_lift = ((best_trevpar - baseline_trevpar) / baseline_trevpar
                      if baseline_trevpar > 0 else 0.0)
        revpar_lift = ((best_revpar - baseline_revpar) / baseline_revpar
                       if baseline_revpar > 0 else 0.0)

        return ElasticityResult(
            optimal_price       = best_price,
            predicted_occupancy = round(best_occ, 4),
            predicted_revpar    = round(best_revpar, 1),
            baseline_revpar     = round(baseline_revpar, 1),
            predicted_trevpar   = round(best_trevpar, 1),
            baseline_trevpar    = round(baseline_trevpar, 1),
            true_lift_pct       = round(total_lift * 100, 2),
            revpar_lift_pct     = round(revpar_lift * 100, 2),
            elasticity_used     = round(elasticity, 4),
            elasticity_profile  = elasticity_profile,
            max_price_premium   = round(max_price_premium, 4),
            optimal_occupancy   = round(optimal_occupancy, 4),
            ml_enabled          = ml_enabled,
            ml_elasticity_multiplier = round(ml_elasticity_multiplier, 4),
            ml_premium_delta    = round(ml_premium_delta, 4),
            ml_occupancy_delta  = round(ml_occupancy_delta, 4),
            ml_state_version    = ml_state_version,
            ancillary_profile   = ancillary_profile,
            ancillary_ratio_used= round(ancillary_ratio, 4),
            ancillary_margin_used=round(ancillary_margin, 4),
            ancillary_per_occ   = round(ancillary_per_occ, 1),
            data_source         = self._data_source,
            search_steps        = steps,
            maml_reserved       = bool(maml_meta.get("maml_reserved")),
            maml_layer4_enabled = bool(maml_meta.get("maml_layer4_enabled")),
            maml_fast_adapt_used = bool(maml_meta.get("maml_fast_adapt_used")),
            maml_market_tier    = str(maml_meta.get("maml_market_tier") or "unknown"),
            maml_feature_schema_version = str(maml_meta.get("maml_feature_schema_version") or "v1.0"),
            maml_profile_name   = str(maml_meta.get("maml_profile_name") or elasticity_profile),
            maml_state_version  = int(maml_meta.get("maml_state_version") or ml_state_version),
            maml_meta_hotel_id_hash = maml_meta.get("maml_meta_hotel_id_hash"),
            maml_readiness      = dict(maml_meta.get("maml_readiness") or {}),
            ml_layer_active     = bool(maml_meta.get("ml_layer_active")),
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 内部辅助
    # ──────────────────────────────────────────────────────────────────────────

    def _get_revenue_profile(
        self,
        *,
        star: int,
        hotel_id: str | None,
        season: str,
        demand_level: str,
    ) -> tuple[str, float, float, float, bool, float, float, float, int]:
        profile_name = self._resolve_revenue_profile(star, hotel_id, season, demand_level)
        profile = _load_macau_revenue_profiles().get(profile_name, {})
        elasticity = float(profile.get("elasticity") or _DEFAULT_ELASTICITY)
        max_premium = float(profile.get("max_price_premium") or _DEFAULT_MAX_PRICE_PREMIUM)
        optimal_occ = float(profile.get("optimal_occupancy") or _DEFAULT_OPTIMAL_OCCUPANCY)
        ml_enabled = False
        ml_elasticity_multiplier = 1.0
        ml_premium_delta = 0.0
        ml_occupancy_delta = 0.0
        ml_state_version = 0

        if _ML_OK and os.getenv("MARE_ML_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}:
            deterministic = os.getenv("MARE_ML_DETERMINISTIC", "0").strip().lower() in {"1", "true", "yes", "on"}
            try:
                decision = _ml_choose_adjustments(profile_name, deterministic=deterministic)
                if decision is not None:
                    ml_enabled = True
                    ml_elasticity_multiplier = float(decision.elasticity_multiplier)
                    ml_premium_delta = float(decision.premium_delta)
                    ml_occupancy_delta = float(decision.occupancy_delta)
                    ml_state_version = int(decision.state_version)
                    elasticity = max(0.10, min(3.00, elasticity * ml_elasticity_multiplier))
                    max_premium = max(-0.10, min(0.60, max_premium + ml_premium_delta))
                    optimal_occ = max(0.60, min(0.95, optimal_occ + ml_occupancy_delta))
            except Exception as exc:
                log.warning(f"[MARE-ML] 自学习层降级为静态基线: {exc}")

        return (
            profile_name,
            elasticity,
            max_premium,
            optimal_occ,
            ml_enabled,
            ml_elasticity_multiplier,
            ml_premium_delta,
            ml_occupancy_delta,
            ml_state_version,
        )

    def _get_hotel_anchor(self, hotel_id: str | None) -> float | None:
        if not hotel_id:
            return None
        profile = _load_v6_profiles().get(hotel_id)
        if not profile:
            return None
        try:
            anchor = float(profile.get("baseline_adr") or 0.0)
            return anchor or None
        except Exception:
            return None

    def _get_ancillary_profile(self, star: int, hotel_id: str | None) -> tuple[str, float, float]:
        profile_name = self._resolve_macau_profile(star, hotel_id)
        profile = _load_macau_ancillary_profiles().get(profile_name, {})
        ratio = float(profile.get("non_room_to_room_ratio") or _DEFAULT_ANCILLARY_RATIO)
        margin = float(profile.get("ancillary_gross_margin") or _DEFAULT_ANCILLARY_MARGIN)
        return profile_name, ratio, margin

    def _resolve_macau_profile(self, star: int, hotel_id: str | None) -> str:
        if int(star or 0) <= 3:
            return "3star"
        if int(star or 0) == 4:
            return "4star"
        if hotel_id and hotel_id in _INTEGRATED_RESORT_IDS:
            return "5star_integrated_resort"
        return "5star_non_casino"

    def _resolve_revenue_profile(
        self,
        star: int,
        hotel_id: str | None,
        season: str,
        demand_level: str,
    ) -> str:
        season_key = (season or "normal").lower()
        demand_key = (demand_level or "NORMAL").upper()

        if int(star or 0) >= 5:
            if hotel_id and hotel_id in _INTEGRATED_RESORT_IDS:
                if season_key in ("super_peak", "peak"):
                    return "5star_resort_leisure_peak"
                if season_key in ("low",) or demand_key == "LOW":
                    return "5star_resort_leisure_low"
                return "5star_resort_leisure_shoulder"
            return "5star_business_mice"

        if int(star or 0) == 4:
            if season_key in ("super_peak", "peak"):
                return "4star_leisure_peak"
            if season_key in ("low",) or demand_key == "LOW":
                return "4star_leisure_low"
            return "4star_leisure_shoulder"

        if season_key in ("super_peak", "peak"):
            return "3star_ota_peak"
        if season_key in ("low",) or demand_key == "LOW":
            return "3star_ota_low"
        return "3star_ota_shoulder"

# ── 模块级单例（三个系统共用同一实例）────────────────────────────────────────
_engine = ElasticityEngine()


def get_engine() -> ElasticityEngine:
    """获取全局弹性引擎单例"""
    return _engine


def optimize_price(
    candidate_price: float,
    market_price:    float,
    star:            int,
    district:        str  = "NAPE",
    demand_level:    str  = "NORMAL",
    season:          str  = "normal",
    hotel_id:        str  = None,
) -> ElasticityResult:
    """快捷函数：直接调用全局引擎的 optimize()"""
    return _engine.optimize(
        candidate_price=candidate_price,
        market_price=market_price,
        star=star,
        district=district,
        demand_level=demand_level,
        season=season,
        hotel_id=hotel_id,
    )
