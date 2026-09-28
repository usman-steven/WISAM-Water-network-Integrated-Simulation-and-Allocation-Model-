"""Public API for setting up, running and querying the HydroNet-Alloc model."""

from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from config import (
    AllocationMethod,
    ETMethod,
    ModelConfig,
    RunoffMethod,
    TimeConfig,
)
from data_io.loader import DataLoader
from engine.simulator import Simulator
from model_metadata import DEFAULT_NETWORK_NAME


class ModelAPI:
    """Thin facade around data loading, simulation and result access."""

    def __init__(self):
        self.config: Optional[ModelConfig] = None
        self.simulator: Optional[Simulator] = None
        self.results: Optional[dict] = None
        self._loader: Optional[DataLoader] = None

    def setup(
        self,
        data_dir: str = "data",
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
        runoff_method: str = "abcd",
        et_method: str = "input",
        allocation_method: str = "priority",
        enable_snowmelt: int = 1,
        enable_gw: int = 1,
        enable_dynamic_demand: int = 1,
        run_preset: str = "full",
        enable_reservoirs: Optional[int] = None,
        enable_lakes: Optional[int] = None,
        enable_diversions: Optional[int] = None,
        enable_transfers: Optional[int] = None,
        enable_demands: Optional[int] = None,
        enable_return_flows: Optional[int] = None,
        enable_groundwater_allocation: Optional[int] = None,
        model_switches: Optional[Dict[str, int]] = None,
        process_switches: Optional[Dict[str, int]] = None,
        tennant_level: str = "good",
        eco_priority: int = 1,
        eco_warmup_years: int = 5,
        model_scenario: Optional[str] = None,
        input_year_policies: Optional[Dict[str, str]] = None,
        fixed_input_years: Optional[Dict[str, int]] = None,
    ) -> "ModelAPI":
        """Configure and load a model instance.

        ``start_year`` and ``end_year`` default to :class:`TimeConfig`, which
        is currently 1951-2024. Pass explicit years for calibration or short
        scenario runs.
        """

        rm_map = {
            "abcd": RunoffMethod.ABCD,
            "gr2m": RunoffMethod.GR2M,
            "coefficient": RunoffMethod.COEFFICIENT,
        }
        et_map = {
            "input": ETMethod.INPUT,
            "hargreaves": ETMethod.HARGREAVES,
            "penman": ETMethod.PENMAN_MONTEITH,
        }
        am_map = {
            "priority": AllocationMethod.PRIORITY,
            "proportional": AllocationMethod.PROPORTIONAL,
        }

        if model_scenario:
            from scenario.catalog import build_model_config

            scenario_overrides = {
                "runoff_method": rm_map.get(runoff_method, RunoffMethod.ABCD),
                "et_method": et_map.get(et_method, ETMethod.INPUT),
                "allocation_method": am_map.get(
                    allocation_method,
                    AllocationMethod.PRIORITY,
                ),
                "tennant_level": tennant_level,
                "default_eco_priority": eco_priority,
                "eco_warmup_years": eco_warmup_years,
            }
            if model_switches:
                scenario_overrides["model_switches"] = model_switches
            if process_switches:
                scenario_overrides["process_switches"] = process_switches
            if input_year_policies:
                scenario_overrides["input_year_policies"] = input_year_policies
            if fixed_input_years:
                scenario_overrides["fixed_input_years"] = fixed_input_years
            self.config = build_model_config(
                model_scenario,
                start_year=start_year,
                end_year=end_year,
                **scenario_overrides,
            )
            self.config.set_process_switches(
                enable_reservoirs=enable_reservoirs,
                enable_lakes=enable_lakes,
                enable_diversions=enable_diversions,
                enable_transfers=enable_transfers,
                enable_demands=enable_demands,
                enable_return_flows=enable_return_flows,
                enable_groundwater_allocation=enable_groundwater_allocation,
            )
            return self._load_config(data_dir, self.config)

        time_config = TimeConfig()
        if start_year is not None:
            time_config.start_year = int(start_year)
        if end_year is not None:
            time_config.end_year = int(end_year)

        self.config = ModelConfig(
            name=DEFAULT_NETWORK_NAME,
            time=time_config,
            runoff_method=rm_map.get(runoff_method, RunoffMethod.ABCD),
            et_method=et_map.get(et_method, ETMethod.INPUT),
            allocation_method=am_map.get(
                allocation_method,
                AllocationMethod.PRIORITY,
            ),
            enable_snowmelt=enable_snowmelt,
            enable_gw_overexploit=enable_gw,
            enable_dynamic_demand=enable_dynamic_demand,
            tennant_level=tennant_level,
            default_eco_priority=eco_priority,
            eco_warmup_years=eco_warmup_years,
        )

        preset = ModelConfig.normalize_run_preset(run_preset)
        if preset != "full":
            self.config.apply_run_preset(preset)
        else:
            self.config.run_preset = preset

        self.config.set_process_switches(
            enable_reservoirs=enable_reservoirs,
            enable_lakes=enable_lakes,
            enable_diversions=enable_diversions,
            enable_transfers=enable_transfers,
            enable_demands=enable_demands,
            enable_return_flows=enable_return_flows,
            enable_groundwater_allocation=enable_groundwater_allocation,
        )
        if model_switches:
            self.config.set_process_switches(**model_switches)
        if process_switches:
            # Alias accepted by scenario files that use process-oriented names.
            self.config.set_process_switches(**process_switches)
        if input_year_policies:
            for domain, policy in input_year_policies.items():
                domain_key = self.config.normalize_input_domain(domain)
                self.config.set_input_year_policy(
                    domain_key,
                    policy,
                    fixed_year=(fixed_input_years or {}).get(domain_key),
                )
        if fixed_input_years:
            for domain, fixed_year in fixed_input_years.items():
                domain_key = self.config.normalize_input_domain(domain)
                policy = self.config.input_year_policies.get(domain_key, "fixed")
                self.config.set_input_year_policy(domain_key, policy, fixed_year=fixed_year)

        return self._load_config(data_dir, self.config)

    def setup_with_config(
        self,
        config: ModelConfig,
        data_dir: str = "data",
    ) -> "ModelAPI":
        """Load and run with an existing ModelConfig object.

        This is the preferred path for scenario studies because it preserves
        every configuration field, including future mutual-aid and resilience
        parameters that may not be exposed as setup keyword arguments yet.
        """
        return self._load_config(data_dir, config)

    def _load_config(self, data_dir: str, config: ModelConfig) -> "ModelAPI":
        self.config = config
        self._loader = DataLoader(data_dir, self.config)
        network = self._loader.load_all()

        self.simulator = Simulator(self.config)
        self.simulator.set_network(network)

        if self._loader.landuse_mod is not None:
            self.simulator.landuse_mod = self._loader.landuse_mod

        return self

    def run(self, progress_cb=None) -> dict:
        if self.simulator is None:
            raise RuntimeError("Call setup() before run().")
        self.results = self.simulator.run(progress_cb=progress_cb)
        return self.results

    def export(self, filepath: str = "results.xlsx"):
        if self.simulator is None:
            raise RuntimeError("Call run() before export().")
        self.simulator.export_results(filepath)

    def get_summary(self) -> Optional[pd.DataFrame]:
        if self.simulator:
            return self.simulator.get_unit_summary()
        return None

    def get_city_summary(self) -> Optional[pd.DataFrame]:
        if self.simulator:
            return self.simulator.get_city_summary()
        return None

    def get_basin_summary(self) -> Optional[pd.DataFrame]:
        if self.simulator:
            return self.simulator.get_basin_summary()
        return None

    def get_transfer_summary(self) -> Optional[pd.DataFrame]:
        if self.simulator:
            return self.simulator.get_transfer_summary()
        return None

    def get_source_mix_summary(self) -> Optional[pd.DataFrame]:
        if self.simulator:
            return self.simulator.get_source_mix_summary()
        return None

    def get_eco_baseflow_summary(self) -> Optional[pd.DataFrame]:
        """Return the ecological baseflow summary after a run."""
        if self.results:
            return self.results.get("eco_baseflow_summary")
        return None

    def get_demand_ts(self, node_id: str) -> Optional[pd.DataFrame]:
        if self.simulator:
            return self.simulator.get_demand_timeseries(node_id)
        return None

    def get_reservoir_ts(self, node_id: str) -> Optional[pd.DataFrame]:
        if self.simulator:
            return self.simulator.get_reservoir_timeseries(node_id)
        return None

    def get_subbasin_ts(self, node_id: str) -> Optional[pd.DataFrame]:
        if self.simulator:
            return self.simulator.get_subbasin_timeseries(node_id)
        return None

    def get_channel_ts(self, node_id: str) -> Optional[pd.DataFrame]:
        if self.simulator:
            return self.simulator.get_channel_timeseries(node_id)
        return None

    def get_node_list(self, node_type: Optional[str] = None) -> list:
        if self.simulator is None or self.simulator.network is None:
            return []
        nodes = []
        for nid, node in self.simulator.network.nodes.items():
            info = {
                "id": nid,
                "name": getattr(node, "name", ""),
                "type": node.node_type.value,
                "basin": getattr(node, "basin", ""),
                "province": getattr(node, "province", ""),
                "eco_priority": getattr(node, "eco_priority", -1),
            }
            if node_type is None or node.node_type.value == node_type:
                nodes.append(info)
        return nodes

    def get_network_summary(self) -> str:
        if self.simulator and self.simulator.network:
            return self.simulator.network.summary()
        return "Network is not loaded."
