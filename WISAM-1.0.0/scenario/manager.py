"""Scenario runner and comparison helpers for HydroNet-Alloc.

ScenarioManager keeps scenario orchestration thin: each scenario uses ModelAPI
for setup/run, while this module focuses on cross-scenario comparison.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from api.interface import ModelAPI
from config import ModelConfig
from scenario.catalog import build_model_config, get_model_scenario
from management.results_builder import (
    BASIN,
    CITY,
    DEMAND,
    SECTOR,
    SHORTAGE,
    SUPPLY,
)

SCENARIO = "scenario"
SATISFACTION_RATE = "satisfaction_rate"
CITY_OUT = "city"
SECTOR_OUT = "sector"
BASIN_OUT = "basin"
DEMAND_OUT = "annual_demand_wan_m3"
SUPPLY_OUT = "annual_supply_wan_m3"
SHORTAGE_OUT = "annual_shortage_wan_m3"


class ScenarioManager:
    """Manage multiple model scenarios and compare their outputs."""

    def __init__(self):
        self.scenarios: Dict[str, dict] = {}
        self.results: Dict[str, dict] = {}

    def add(
        self,
        name: str,
        config: ModelConfig,
        data_dir: str = "data",
        description: str = "",
    ) -> None:
        """Register one scenario.

        ``name`` is the durable scenario id used in comparison tables and
        exported filenames.
        """
        if not name:
            raise ValueError("Scenario name cannot be empty.")
        self.scenarios[name] = {
            "config": config,
            "data_dir": data_dir,
            "description": description,
            "api": None,
        }

    def add_from_catalog(
        self,
        scenario: str,
        data_dir: str = "data",
        name: Optional[str] = None,
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
        **config_overrides,
    ) -> None:
        """Register a canonical catalog scenario.

        This keeps future scenario studies from duplicating period, process,
        and fixed-year policy settings in notebook cells or one-off scripts.
        """
        spec = get_model_scenario(scenario)
        config = build_model_config(
            spec.id,
            start_year=start_year,
            end_year=end_year,
            **config_overrides,
        )
        self.add(
            name or spec.id,
            config=config,
            data_dir=data_dir,
            description=spec.description,
        )

    def run(self, name: str, progress_cb=None) -> dict:
        """Run one registered scenario."""
        if name not in self.scenarios:
            raise ValueError(f"Scenario '{name}' is not registered.")

        scenario = self.scenarios[name]
        print(f"\n{'=' * 60}")
        print(f"Running scenario: {name}")
        if scenario["description"]:
            print(f"Description: {scenario['description']}")
        print(f"{'=' * 60}")

        api = ModelAPI().setup_with_config(
            scenario["config"],
            data_dir=scenario["data_dir"],
        )
        results = api.run(progress_cb=progress_cb)

        scenario["api"] = api
        self.results[name] = results
        return results

    def run_all(self, progress_cb=None) -> None:
        """Run all registered scenarios in registration order."""
        for name in self.scenarios:
            self.run(name, progress_cb=progress_cb)

    def compare(self, metric: str = SHORTAGE) -> Optional[pd.DataFrame]:
        """Compare one city-sector metric across scenarios."""
        if not self.results:
            print("No scenario results to compare.")
            return None

        frames = []
        group_cols = [CITY, SECTOR]
        for name, result in self.results.items():
            summary = result.get("summary")
            if not isinstance(summary, pd.DataFrame) or summary.empty:
                continue
            if metric not in summary.columns:
                continue
            frame = summary.groupby(group_cols, as_index=False)[metric].sum()
            frames.append(frame.rename(columns={metric: name}))

        if not frames:
            return None

        comparison = frames[0]
        for frame in frames[1:]:
            comparison = comparison.merge(frame, on=group_cols, how="outer")

        scenario_names = list(self.results.keys())
        if len(scenario_names) >= 2:
            base = scenario_names[0]
            for scenario_name in scenario_names[1:]:
                if base in comparison.columns and scenario_name in comparison.columns:
                    comparison[f"{scenario_name}_vs_{base}"] = (
                        comparison[scenario_name] - comparison[base]
                    )
        return comparison.rename(columns={CITY: CITY_OUT, SECTOR: SECTOR_OUT})

    def compare_by_basin(self) -> Optional[pd.DataFrame]:
        """Return basin-level demand, supply, shortage and satisfaction."""
        if not self.results:
            return None

        records = []
        for name, result in self.results.items():
            basin_total = result.get("summary_basin_total")
            if not isinstance(basin_total, pd.DataFrame) or basin_total.empty:
                continue
            for _, row in basin_total.iterrows():
                demand = float(row.get(DEMAND, 0.0))
                supply = float(row.get(SUPPLY, 0.0))
                records.append(
                    {
                        SCENARIO: name,
                        BASIN_OUT: row.get(BASIN, ""),
                        DEMAND_OUT: demand,
                        SUPPLY_OUT: supply,
                        SHORTAGE_OUT: float(row.get(SHORTAGE, 0.0)),
                        SATISFACTION_RATE: round(supply / max(1.0, demand), 3),
                    }
                )

        if not records:
            return None
        return pd.DataFrame(records)

    def export_all(self, output_dir: str = "output") -> None:
        """Export each scenario workbook and the default comparison table."""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        for name, scenario in self.scenarios.items():
            api = scenario.get("api")
            if api is not None:
                api.export(str(output_path / f"{name}_results.xlsx"))

        comparison = self.compare()
        if comparison is not None:
            comparison.to_excel(output_path / "scenario_comparison.xlsx", index=False)
