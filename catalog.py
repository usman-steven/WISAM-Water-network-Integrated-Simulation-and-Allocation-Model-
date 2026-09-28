"""Canonical model-scenario catalog for HydroNet-Alloc.

The catalog separates model meaning from ad hoc script parameters.  Each
scenario defines the process layer, simulation window, and how input years are
interpreted for hydrology, demand, engineering infrastructure, and managed
allocation calibration factors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from config import ModelConfig, TimeConfig


S3_GROUNDWATER_STORAGE_BUFFER_BY_BASIN = (
    "\u6d77\u6cb3=0.67;"
    "\u6dee\u6cb3=1.10;"
    "\u9ec4\u6cb3=4.00;"
    "\u957f\u6c5f=0.50"
)


REGULATED_TRANSFER_STORAGE_CONSTRAINT = {
    # Reservoir/lake-backed regulated transfers should be bounded by source
    # storage status in all allocation scenarios. S1 keeps transfers disabled.
    "enable_regulated_transfer_storage_constraint": 1,
}


@dataclass(frozen=True)
class ModelScenarioSpec:
    id: str
    order: int
    name: str
    layer: str
    description: str
    start_year: int
    end_year: int
    run_preset: str
    input_year_policies: dict[str, str] = field(default_factory=dict)
    fixed_input_years: dict[str, int] = field(default_factory=dict)
    process_switches: dict[str, int] = field(default_factory=dict)
    config_overrides: dict[str, object] = field(default_factory=dict)
    include_planned_infrastructure: int = 0
    active_infrastructure_scenario: str = "baseline"
    recommended_use: str = ""

    def build_config(
        self,
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
        **overrides,
    ) -> ModelConfig:
        """Create a ModelConfig for this scenario.

        ``start_year`` and ``end_year`` may be overridden for quick tests, but
        the scenario's input-year policies stay unchanged.
        """
        config = ModelConfig(
            time=TimeConfig(
                start_year=int(start_year if start_year is not None else self.start_year),
                end_year=int(end_year if end_year is not None else self.end_year),
            ),
            run_preset=self.run_preset,
            include_planned_infrastructure=self.include_planned_infrastructure,
            active_infrastructure_scenario=self.active_infrastructure_scenario,
        )
        config.set_model_scenario(
            self.id,
            name=self.name,
            layer=self.layer,
            description=self.description,
        )
        for domain, policy in self.input_year_policies.items():
            fixed_year = self.fixed_input_years.get(domain)
            config.set_input_year_policy(domain, policy, fixed_year=fixed_year)
        for domain, fixed_year in self.fixed_input_years.items():
            if domain not in self.input_year_policies:
                config.set_input_year_policy(domain, "fixed", fixed_year=fixed_year)
        if self.process_switches:
            config.set_process_switches(**self.process_switches)
        for key, value in self.config_overrides.items():
            if hasattr(config, key):
                setattr(config, key, value)
            else:
                raise AttributeError(f"Unknown ModelConfig scenario override: {key}")

        for key, value in overrides.items():
            if value is None:
                continue
            if key in {"process_switches", "model_switches"} and isinstance(value, dict):
                config.set_process_switches(**value)
            elif key == "input_year_policies" and isinstance(value, dict):
                for domain, policy in value.items():
                    config.set_input_year_policy(
                        domain,
                        policy,
                        fixed_year=config.fixed_input_years.get(
                            config.normalize_input_domain(domain)
                        ),
                    )
            elif key == "fixed_input_years" and isinstance(value, dict):
                for domain, fixed_year in value.items():
                    domain_key = config.normalize_input_domain(domain)
                    policy = config.input_year_policies.get(domain_key, "fixed")
                    config.set_input_year_policy(domain_key, policy, fixed_year=int(fixed_year))
            elif hasattr(config, key):
                setattr(config, key, value)
            else:
                raise AttributeError(f"Unknown ModelConfig override: {key}")
        return config


MODEL_SCENARIOS: dict[str, ModelScenarioSpec] = {
    "S1_natural_cycle": ModelScenarioSpec(
        id="S1_natural_cycle",
        order=1,
        name="自然水循环模拟",
        layer="natural_cycle",
        description="只模拟气象-产流-汇流-生态基流等自然水循环过程，不叠加取用水、水库、调水、退水等人类活动。",
        start_year=1951,
        end_year=2024,
        run_preset="natural",
        input_year_policies={
            "hydrology": "calendar",
            "demand": "calendar",
            "infrastructure": "calendar",
            "source_structure": "calendar",
            "unconventional": "calendar",
        },
        process_switches={
            "include_planned_infrastructure": 0,
            "enable_water_rights": 0,
        },
        include_planned_infrastructure=0,
        active_infrastructure_scenario="baseline",
        recommended_use="水文站率定、天然水资源量、生态基流基准、干旱/丰枯自然来水诊断。",
    ),
    "S2_historical_allocation": ModelScenarioSpec(
        id="S2_historical_allocation",
        order=2,
        name="历史水资源配置模拟",
        layer="historical_allocation",
        description="在自然水循环基础上复演1965-2024年实际用水、工程投运、地下水、非常规水、退水和黄河分水控制过程；供水差额作为历史闭合误差诊断，不作为缺水情景。",
        start_year=1965,
        end_year=2024,
        run_preset="full",
        input_year_policies={
            "hydrology": "calendar",
            "demand": "calendar",
            "infrastructure": "calendar",
            "source_structure": "calendar",
            "unconventional": "calendar",
        },
        process_switches={
            "include_planned_infrastructure": 0,
            "enable_water_rights": 1,
            "enable_historical_supply_closure": 1,
            "enable_historical_irrigation_canal_buffer": 1,
            "enable_gw_overexploit": 0,
        },
        config_overrides={
            **REGULATED_TRANSFER_STORAGE_CONSTRAINT,
            "water_rights_budget_mode": "annual",
            "water_rights_demand_accounting": "net_consumption",
        },
        include_planned_infrastructure=0,
        active_infrastructure_scenario="baseline",
        recommended_use="历史自然-社会过程复演、入海水量、地下水/地表水供水结构和人类活动影响归因；不用于缺水压力判断。",
    ),
    "S3-1_current_2024_baseline": ModelScenarioSpec(
        id="S3-1_current_2024_baseline",
        order=3,
        name="S3-1 2024现状年水网配置基础情景",
        layer="current_year_baseline",
        description="固定2024年自然来水、2024年水网、2024年用水、2024年非常规水、2024年配置校正关系和现状管理规则，用于先校核现状年水网配置结构。",
        start_year=2024,
        end_year=2024,
        run_preset="full",
        input_year_policies={
            "hydrology": "calendar",
            "demand": "fixed",
            "infrastructure": "fixed",
            "source_structure": "fixed",
            "unconventional": "fixed",
        },
        fixed_input_years={
            "demand": 2024,
            "infrastructure": 2024,
            "source_structure": 2024,
            "unconventional": 2024,
        },
        process_switches={
            "include_planned_infrastructure": 0,
            "enable_water_rights": 1,
            "enable_historical_supply_closure": 0,
        },
        config_overrides={
            **REGULATED_TRANSFER_STORAGE_CONSTRAINT,
        },
        include_planned_infrastructure=0,
        active_infrastructure_scenario="baseline",
        recommended_use="作为后续所有现状水网推演的基础对照，先校核2024年供水结构、调水工程利用、黄河分水和空间缺水分布。",
    ),
    "S3-2_ecological_baseflow_strict_2024": ModelScenarioSpec(
        id="S3-2_ecological_baseflow_strict_2024",
        order=4,
        name="S3-2 生态基流严格约束推演",
        layer="ecological_baseflow_stress",
        description="在S3-1现状年基础上提高蒙大拿法生态基流等级，单独评估生态基流约束对现状水网配置的影响。",
        start_year=2024,
        end_year=2024,
        run_preset="full",
        input_year_policies={
            "hydrology": "calendar",
            "demand": "fixed",
            "infrastructure": "fixed",
            "source_structure": "fixed",
            "unconventional": "fixed",
        },
        fixed_input_years={
            "demand": 2024,
            "infrastructure": 2024,
            "source_structure": 2024,
            "unconventional": 2024,
        },
        process_switches={
            "include_planned_infrastructure": 0,
            "enable_water_rights": 1,
            "enable_historical_supply_closure": 0,
        },
        config_overrides={
            **REGULATED_TRANSFER_STORAGE_CONSTRAINT,
            "tennant_level": "excellent",
        },
        include_planned_infrastructure=0,
        active_infrastructure_scenario="baseline",
        recommended_use="第一层压力推演：只改变生态基流保护等级，识别生态约束本身造成的供需压力变化。",
    ),
    "S3-3_groundwater_balance_2024": ModelScenarioSpec(
        id="S3-3_groundwater_balance_2024",
        order=5,
        name="S3-3 地下水采补平衡约束推演",
        layer="groundwater_balance_stress",
        description="在S3-2严格生态基流约束基础上启用地下水采补平衡，地下水供水受当年模拟补给预算控制。",
        start_year=2024,
        end_year=2024,
        run_preset="full",
        input_year_policies={
            "hydrology": "calendar",
            "demand": "fixed",
            "infrastructure": "fixed",
            "source_structure": "fixed",
            "unconventional": "fixed",
        },
        fixed_input_years={
            "demand": 2024,
            "infrastructure": 2024,
            "source_structure": 2024,
            "unconventional": 2024,
        },
        process_switches={
            "include_planned_infrastructure": 0,
            "enable_water_rights": 1,
            "enable_historical_supply_closure": 0,
        },
        config_overrides={
            **REGULATED_TRANSFER_STORAGE_CONSTRAINT,
            "tennant_level": "excellent",
            "groundwater_balance_mode": "recharge_balance",
            "groundwater_recharge_balance_factor": 1.0,
            "groundwater_balance_storage_buffer_ratio": 0.0,
            # Recharge balance is interpreted as a management constraint over
            # groundwater renewal plus basin-scale multi-year regulation
            # capacity, not a hard annual recharge-only cap.
            "groundwater_balance_storage_buffer_by_basin": S3_GROUNDWATER_STORAGE_BUFFER_BY_BASIN,
        },
        include_planned_infrastructure=0,
        active_infrastructure_scenario="baseline",
        recommended_use="第二层压力推演：在生态约束之上限制地下水长期超采，识别地下水管控对缺水空间格局的影响。",
    ),
    "S3-4_supply_side_long_series": ModelScenarioSpec(
        id="S3-4_supply_side_long_series",
        order=6,
        name="S3-4 供给侧长序列变化推演",
        layer="supply_side_long_series",
        description="在2024年现状水网、现状用水、严格生态基流和地下水采补平衡约束基础上，叠加1951-2024年自然来水序列，用于供给侧多年变化和连续干旱压力测试。",
        start_year=1951,
        end_year=2024,
        run_preset="full",
        input_year_policies={
            "hydrology": "calendar",
            "demand": "fixed",
            "infrastructure": "fixed",
            "source_structure": "fixed",
            "unconventional": "fixed",
        },
        fixed_input_years={
            "demand": 2024,
            "infrastructure": 2024,
            "source_structure": 2024,
            "unconventional": 2024,
        },
        process_switches={
            "include_planned_infrastructure": 0,
            "enable_water_rights": 1,
            "enable_historical_supply_closure": 0,
        },
        config_overrides={
            **REGULATED_TRANSFER_STORAGE_CONSTRAINT,
            "tennant_level": "excellent",
            "groundwater_balance_mode": "recharge_balance",
            "groundwater_recharge_balance_factor": 1.0,
            "groundwater_balance_storage_buffer_ratio": 0.0,
            "groundwater_balance_storage_buffer_by_basin": S3_GROUNDWATER_STORAGE_BUFFER_BY_BASIN,
            # Supply-side long-sequence tests should let persistent drought
            # reduce reservoir/lake-backed regulated transfer availability.
            # S3 means "current 2024 management rules + historical hydrology".
            # Apply the present Yellow River allocation constraint throughout
            # the hydrological sequence, instead of switching it on in 1987.
            "water_rights_apply_from_year": 1951,
        },
        include_planned_infrastructure=0,
        active_infrastructure_scenario="baseline",
        recommended_use="第三层压力推演：在生态约束和地下水管控之上，用长序列来水检验供给侧丰枯变化和连续干旱风险。",
    ),
    "WRR-S0_local_supply_reference_2024": ModelScenarioSpec(
        id="WRR-S0_local_supply_reference_2024",
        order=20,
        name="WRR-S0 local-supply reference",
        layer="wrr_local_supply_reference",
        description=(
            "WRR paper scenario: fixed 2024 demand, infrastructure, source "
            "structure and unconventional supply, with inter-basin transfer "
            "service disabled. Local surface water, reservoirs, lakes, "
            "groundwater, unconventional water and water-right controls are retained."
        ),
        start_year=2024,
        end_year=2024,
        run_preset="full",
        input_year_policies={
            "hydrology": "calendar",
            "demand": "fixed",
            "infrastructure": "fixed",
            "source_structure": "fixed",
            "unconventional": "fixed",
        },
        fixed_input_years={
            "demand": 2024,
            "infrastructure": 2024,
            "source_structure": 2024,
            "unconventional": 2024,
        },
        process_switches={
            "include_planned_infrastructure": 0,
            "enable_transfers": 0,
            "enable_water_rights": 1,
            "enable_historical_supply_closure": 0,
        },
        config_overrides={
            **REGULATED_TRANSFER_STORAGE_CONSTRAINT,
        },
        include_planned_infrastructure=0,
        active_infrastructure_scenario="baseline",
        recommended_use=(
            "WRR local-supply reference for estimating current transfer-network "
            "contribution relative to a no-transfer allocation state."
        ),
    ),
    "WRR-S1_planned_transfer_accounting_2024": ModelScenarioSpec(
        id="WRR-S1_planned_transfer_accounting_2024",
        order=21,
        name="WRR-S1 planned-transfer accounting",
        layer="wrr_planned_transfer_accounting",
        description=(
            "WRR paper scenario: planned transfer intake is treated as scheduled "
            "receiving-area supply after conveyance loss, without source-side "
            "hydrologic, storage or water-right constraints on transfer execution."
        ),
        start_year=2024,
        end_year=2024,
        run_preset="full",
        input_year_policies={
            "hydrology": "calendar",
            "demand": "fixed",
            "infrastructure": "fixed",
            "source_structure": "fixed",
            "unconventional": "fixed",
        },
        fixed_input_years={
            "demand": 2024,
            "infrastructure": 2024,
            "source_structure": 2024,
            "unconventional": 2024,
        },
        process_switches={
            "include_planned_infrastructure": 0,
            "enable_water_rights": 1,
            "enable_historical_supply_closure": 0,
        },
        config_overrides={
            **REGULATED_TRANSFER_STORAGE_CONSTRAINT,
            "transfer_service_mode": "planned_accounting",
        },
        include_planned_infrastructure=0,
        active_infrastructure_scenario="baseline",
        recommended_use=(
            "WRR planning-accounting benchmark for comparing planned transfer "
            "service with link-constrained transfer realization."
        ),
    ),
    "WRR-S2_current_link_constrained_transfer_2024": ModelScenarioSpec(
        id="WRR-S2_current_link_constrained_transfer_2024",
        order=22,
        name="WRR-S2 current link-constrained transfer network",
        layer="wrr_current_transfer_network",
        description=(
            "WRR paper scenario: fixed 2024 demand and current built transfer "
            "topology, with transfer service constrained by plan, capacity, "
            "source condition, operating rules, receiving topology and losses."
        ),
        start_year=2024,
        end_year=2024,
        run_preset="full",
        input_year_policies={
            "hydrology": "calendar",
            "demand": "fixed",
            "infrastructure": "fixed",
            "source_structure": "fixed",
            "unconventional": "fixed",
        },
        fixed_input_years={
            "demand": 2024,
            "infrastructure": 2024,
            "source_structure": 2024,
            "unconventional": 2024,
        },
        process_switches={
            "include_planned_infrastructure": 0,
            "enable_water_rights": 1,
            "enable_historical_supply_closure": 0,
        },
        config_overrides={
            **REGULATED_TRANSFER_STORAGE_CONSTRAINT,
        },
        include_planned_infrastructure=0,
        active_infrastructure_scenario="baseline",
        recommended_use=(
            "WRR current-network result for basin contribution, source mix and "
            "planned-intake-to-delivered-water diagnostics."
        ),
    ),
    "WRR-S3_long_sequence_current_network": ModelScenarioSpec(
        id="WRR-S3_long_sequence_current_network",
        order=23,
        name="WRR-S3 long-sequence current transfer network",
        layer="wrr_long_sequence_transfer_network",
        description=(
            "WRR paper scenario: 1951-2024 hydrology with fixed 2024 demand, "
            "current built infrastructure, source structure, unconventional "
            "supply and current transfer-link rules."
        ),
        start_year=1951,
        end_year=2024,
        run_preset="full",
        input_year_policies={
            "hydrology": "calendar",
            "demand": "fixed",
            "infrastructure": "fixed",
            "source_structure": "fixed",
            "unconventional": "fixed",
        },
        fixed_input_years={
            "demand": 2024,
            "infrastructure": 2024,
            "source_structure": 2024,
            "unconventional": 2024,
        },
        process_switches={
            "include_planned_infrastructure": 0,
            "enable_water_rights": 1,
            "enable_historical_supply_closure": 0,
        },
        config_overrides={
            **REGULATED_TRANSFER_STORAGE_CONSTRAINT,
            "water_rights_apply_from_year": 1951,
        },
        include_planned_infrastructure=0,
        active_infrastructure_scenario="baseline",
        recommended_use=(
            "WRR long-sequence diagnostic for hydrologic service windows under "
            "current demand and current transfer-network topology."
        ),
    ),
}

SCENARIO_ALIASES = {
    "1": "S1_natural_cycle",
    "s1": "S1_natural_cycle",
    "natural": "S1_natural_cycle",
    "natural_cycle": "S1_natural_cycle",
    "hydrology": "S1_natural_cycle",
    "hydro": "S1_natural_cycle",
    "2": "S2_historical_allocation",
    "s2": "S2_historical_allocation",
    "historical": "S2_historical_allocation",
    "historical_allocation": "S2_historical_allocation",
    "history": "S2_historical_allocation",
    "3": "S3-1_current_2024_baseline",
    "s3": "S3-1_current_2024_baseline",
    "s3_1": "S3-1_current_2024_baseline",
    "s31": "S3-1_current_2024_baseline",
    "s3_1_current_2024_baseline": "S3-1_current_2024_baseline",
    "s3_current_2024_baseline": "S3-1_current_2024_baseline",
    "current": "S3-1_current_2024_baseline",
    "current2024": "S3-1_current_2024_baseline",
    "status_quo": "S3-1_current_2024_baseline",
    "present": "S3-1_current_2024_baseline",
    "current_year": "S3-1_current_2024_baseline",
    "baseline2024": "S3-1_current_2024_baseline",
    "s3_2": "S3-2_ecological_baseflow_strict_2024",
    "s32": "S3-2_ecological_baseflow_strict_2024",
    "s3_2_ecological_baseflow_strict_2024": "S3-2_ecological_baseflow_strict_2024",
    "s4_ecological_baseflow_strict_2024": "S3-2_ecological_baseflow_strict_2024",
    "eco": "S3-2_ecological_baseflow_strict_2024",
    "ecological": "S3-2_ecological_baseflow_strict_2024",
    "ecological_baseflow": "S3-2_ecological_baseflow_strict_2024",
    "s3_3": "S3-3_groundwater_balance_2024",
    "s33": "S3-3_groundwater_balance_2024",
    "s3_3_groundwater_balance_2024": "S3-3_groundwater_balance_2024",
    "s5_groundwater_balance_2024": "S3-3_groundwater_balance_2024",
    "groundwater_balance": "S3-3_groundwater_balance_2024",
    "gw_balance": "S3-3_groundwater_balance_2024",
    "s3_4": "S3-4_supply_side_long_series",
    "s34": "S3-4_supply_side_long_series",
    "s3_4_supply_side_long_series": "S3-4_supply_side_long_series",
    "s3_current_2024_long_series": "S3-4_supply_side_long_series",
    "s6_supply_side_long_series": "S3-4_supply_side_long_series",
    "supply_side": "S3-4_supply_side_long_series",
    "long_series": "S3-4_supply_side_long_series",
    "current_network_long_series": "S3-4_supply_side_long_series",
    "wrr_s0": "WRR-S0_local_supply_reference_2024",
    "wrrs0": "WRR-S0_local_supply_reference_2024",
    "wrr_s0_local_supply_reference_2024": "WRR-S0_local_supply_reference_2024",
    "local_supply_reference": "WRR-S0_local_supply_reference_2024",
    "no_transfer": "WRR-S0_local_supply_reference_2024",
    "wrr_s1": "WRR-S1_planned_transfer_accounting_2024",
    "wrrs1": "WRR-S1_planned_transfer_accounting_2024",
    "wrr_s1_planned_transfer_accounting_2024": "WRR-S1_planned_transfer_accounting_2024",
    "planned_transfer_accounting": "WRR-S1_planned_transfer_accounting_2024",
    "planned_accounting": "WRR-S1_planned_transfer_accounting_2024",
    "wrr_s2": "WRR-S2_current_link_constrained_transfer_2024",
    "wrrs2": "WRR-S2_current_link_constrained_transfer_2024",
    "wrr_s2_current_link_constrained_transfer_2024": "WRR-S2_current_link_constrained_transfer_2024",
    "current_link_constrained_transfer": "WRR-S2_current_link_constrained_transfer_2024",
    "wrr_s3": "WRR-S3_long_sequence_current_network",
    "wrrs3": "WRR-S3_long_sequence_current_network",
    "wrr_s3_long_sequence_current_network": "WRR-S3_long_sequence_current_network",
    "wrr_long_sequence": "WRR-S3_long_sequence_current_network",
}


def normalize_scenario_id(name: str) -> str:
    key = str(name or "").strip()
    if key in MODEL_SCENARIOS:
        return key
    lowered = key.lower()
    for scenario_id in MODEL_SCENARIOS:
        if scenario_id.lower() == lowered:
            return scenario_id
    alias_key = lowered.replace("-", "_")
    if alias_key in SCENARIO_ALIASES:
        return SCENARIO_ALIASES[alias_key]
    allowed = ", ".join(spec.id for spec in list_model_scenarios())
    raise ValueError(f"Unknown model scenario '{name}'. Allowed: {allowed}")


def get_model_scenario(name: str) -> ModelScenarioSpec:
    return MODEL_SCENARIOS[normalize_scenario_id(name)]


def build_model_config(
    name: str,
    start_year: Optional[int] = None,
    end_year: Optional[int] = None,
    **overrides,
) -> ModelConfig:
    spec = get_model_scenario(name)
    return spec.build_config(start_year=start_year, end_year=end_year, **overrides)


def list_model_scenarios() -> list[ModelScenarioSpec]:
    return sorted(MODEL_SCENARIOS.values(), key=lambda item: item.order)


def scenario_table() -> pd.DataFrame:
    rows = []
    for spec in list_model_scenarios():
        rows.append(
            {
                "id": spec.id,
                "order": spec.order,
                "name": spec.name,
                "layer": spec.layer,
                "period": f"{spec.start_year}-{spec.end_year}",
                "run_preset": spec.run_preset,
                "input_year_policies": ";".join(
                    f"{k}={v}" for k, v in spec.input_year_policies.items()
                ),
                "fixed_input_years": ";".join(
                    f"{k}={v}" for k, v in spec.fixed_input_years.items()
                ),
                "description": spec.description,
                "recommended_use": spec.recommended_use,
            }
        )
    return pd.DataFrame(rows)
