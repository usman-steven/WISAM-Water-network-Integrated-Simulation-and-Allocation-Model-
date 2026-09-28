"""Command-line entry point for a WISAM model run.

The data loader is the single source of truth for all model inputs, including
precomputed runoff series. This script only parses run options, calls ModelAPI,
prints a compact summary and exports the result workbook.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from api.interface import ModelAPI
from config import TimeConfig
from management.results_builder import (
    BASIN,
    CITY,
    DEMAND,
    MAX_SHORT_RATE,
    SHORTAGE,
    SUPPLY,
)
from scenario import get_model_scenario, scenario_table

SUMMARY_RENAME = {
    BASIN: "basin",
    CITY: "city",
    DEMAND: "annual_demand_wan_m3",
    SUPPLY: "annual_supply_wan_m3",
    SHORTAGE: "annual_shortage_wan_m3",
    MAX_SHORT_RATE: "max_monthly_shortage_rate",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run WISAM.")
    parser.add_argument("--data-dir", default="data", help="Input data directory.")
    parser.add_argument("--output", default="output/results.xlsx", help="Result workbook path.")
    parser.add_argument("--start-year", type=int, default=None)
    parser.add_argument("--end-year", type=int, default=None)
    parser.add_argument(
        "--model-scenario",
        default="",
        help="Canonical scenario id or alias, e.g. 1/natural, 2/historical, s3-1/current2024, s3-4/long_series.",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="Print canonical model scenarios and exit.",
    )
    parser.add_argument(
        "--preset",
        default="full",
        choices=["full", "natural"],
        help="Run preset: full water network or natural hydrology only.",
    )
    return parser.parse_args()


def progress(current_step: int, total_steps: int, message: str) -> None:
    pct = current_step / max(1, total_steps) * 100.0
    print(f"\r  progress: {pct:5.1f}% {message}    ", end="", flush=True)


def print_run_summary(results: dict) -> None:
    basin_total = results.get("summary_basin_total")
    city_total = results.get("summary_city_total")
    transfer_summary = results.get("transfer_summary")
    reservoir_summary = results.get("reservoir_summary")
    ecology_summary = results.get("ecology_summary")
    audit_summary = results.get("audit_summary")

    print("\n\n" + "=" * 60)
    print("Run summary")
    print("=" * 60)

    if isinstance(basin_total, pd.DataFrame) and not basin_total.empty:
        cols = [c for c in [BASIN, DEMAND, SUPPLY, SHORTAGE] if c in basin_total.columns]
        if cols:
            table = basin_total[cols].copy()
            if DEMAND in table.columns and SUPPLY in table.columns:
                table["satisfaction_rate"] = (
                    table[SUPPLY] / table[DEMAND].replace(0, 1)
                ).round(3)
            table = table.rename(columns=SUMMARY_RENAME)
            print("\nBasin water balance:")
            print(table.round(1).to_string(index=False))

    if isinstance(city_total, pd.DataFrame) and not city_total.empty:
        if CITY in city_total.columns and SHORTAGE in city_total.columns:
            agg_map = {SHORTAGE: "sum"}
            if MAX_SHORT_RATE in city_total.columns:
                agg_map[MAX_SHORT_RATE] = "max"
            city_rank = (
                city_total.groupby(CITY, as_index=False)
                .agg(agg_map)
                .sort_values(SHORTAGE, ascending=False)
                .head(10)
            )
            city_rank = city_rank.rename(columns=SUMMARY_RENAME)
            print("\nTop 10 cities by shortage:")
            print(city_rank.round(3).to_string(index=False))

    if isinstance(transfer_summary, pd.DataFrame) and not transfer_summary.empty:
        print(f"\nTransfer projects reported: {len(transfer_summary)}")

    if isinstance(reservoir_summary, pd.DataFrame) and not reservoir_summary.empty:
        print(f"Reservoirs reported: {len(reservoir_summary)}")

    if isinstance(ecology_summary, pd.DataFrame) and not ecology_summary.empty:
        print(f"Ecological control sections reported: {len(ecology_summary)}")

    if isinstance(audit_summary, pd.DataFrame) and not audit_summary.empty:
        balance_items = [
            "allocation_balance_error",
            "demand_balance_error",
            "consumption_balance_error",
            "transfer_balance_error",
        ]
        balance = audit_summary[audit_summary["item"].isin(balance_items)].copy()
        if not balance.empty:
            print("\nBalance audit:")
            print(balance[["item", "period_sum", "max_month", "min_month"]].to_string(index=False))


def main() -> None:
    args = parse_args()
    if args.list_scenarios:
        print(scenario_table().to_string(index=False))
        return

    data_dir = Path(args.data_dir)
    if not (data_dir / "units.csv").exists():
        raise FileNotFoundError(f"Missing required file: {data_dir / 'units.csv'}")

    defaults = TimeConfig()
    start_year = args.start_year
    end_year = args.end_year
    scenario_name = ""
    display_preset = args.preset
    if args.model_scenario:
        spec = get_model_scenario(args.model_scenario)
        scenario_name = spec.id
        display_preset = spec.run_preset
        start_year = start_year if start_year is not None else spec.start_year
        end_year = end_year if end_year is not None else spec.end_year
    else:
        start_year = start_year if start_year is not None else defaults.start_year
        end_year = end_year if end_year is not None else defaults.end_year

    print("=" * 60)
    print("WISAM water network simulation")
    print("=" * 60)
    print(
        f"data={args.data_dir}, years={start_year}-{end_year}, "
        f"preset={display_preset}, model_scenario={scenario_name or '-'}"
    )

    api = ModelAPI().setup(
        data_dir=args.data_dir,
        start_year=start_year,
        end_year=end_year,
        run_preset=args.preset,
        model_scenario=args.model_scenario or None,
    )
    results = api.run(progress_cb=progress)
    print_run_summary(results)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    api.export(str(output_path))
    print(f"\nSaved workbook: {output_path}")


if __name__ == "__main__":
    main()
