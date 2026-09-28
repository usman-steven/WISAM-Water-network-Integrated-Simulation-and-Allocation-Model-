"""Run the WISAM API with deterministic synthetic input data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import numpy as np
import pandas as pd

PACKAGE = Path(__file__).resolve().parents[1]
EXAMPLE = Path(__file__).resolve().parent
DATA = EXAMPLE / "data"
OUTPUT = EXAMPLE / "output"
os.environ["WISAM_OUTPUT_DIR"] = str(OUTPUT)
sys.path.insert(0, str(PACKAGE))

from api.interface import ModelAPI
from config import ModelConfig, TimeConfig, RunoffMethod, ETMethod, NodeType
from management.results_builder import ANNUAL_DEMAND, ANNUAL_SUPPLY, ANNUAL_SHORTAGE


def write_table(relative: str, rows: list[dict] | pd.DataFrame) -> None:
    path = DATA / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def generate_inputs() -> None:
    """Write deterministic synthetic tables; regenerate the same values each run."""
    write_table("units.csv", [
        {"uid": "SYN_A", "uname": "Synthetic upstream", "cid": "SYN_CITY_A",
         "cname": "Synthetic city A", "wid": "SYN_ZONE", "wname": "Synthetic river",
         "area": 100.0, "is_mainstream": 0, "basin": "Synthetic basin",
         "province": "Synthetic province", "x": 1.0, "y": 1.0},
        {"uid": "SYN_B", "uname": "Synthetic downstream", "cid": "SYN_CITY_B",
         "cname": "Synthetic city B", "wid": "SYN_ZONE", "wname": "Synthetic river",
         "area": 80.0, "is_mainstream": 0, "basin": "Synthetic basin",
         "province": "Synthetic province", "x": 1.1, "y": 0.9},
    ])
    write_table("topology.csv", [{"from_uid": "SYN_A", "to_uid": "SYN_B"}])
    dates = pd.date_range("2000-01-01", periods=24, freq="MS")
    precipitation = np.array([55, 60, 70, 85, 100, 120, 130, 110, 90, 70, 60, 50] * 2, dtype=float)
    precipitation[12:] *= 0.45
    for name, a, b in [
        ("precip", precipitation, precipitation * 0.8),
        ("pet", np.full(24, 40.0), np.full(24, 40.0)),
        ("temp_mean", np.full(24, 15.0), np.full(24, 15.0)),
    ]:
        write_table(f"climate/{name}.csv", pd.DataFrame({
            "date": dates.strftime("%Y-%m-%d"), "SYN_A": a, "SYN_B": b,
        }))
    write_table("hydro_params.csv", [
        {"unit_id": uid, "runoff_coeff": 0.45, "et_reduction": 0.5,
         "baseflow_index": 0.2, "eco_priority": 1, "is_high_altitude": 0}
        for uid in ("SYN_A", "SYN_B")
    ])
    write_table("demand/annual_demand.csv", [
        {"city_code": city, "year": year, "domestic": domestic,
         "industrial": industrial, "agricultural": agriculture, "ecological": 60.0}
        for year in (2000, 2001)
        for city, domestic, industrial, agriculture in (
            ("SYN_CITY_A", 300.0, 300.0, 1140.0),
            ("SYN_CITY_B", 360.0, 480.0, 1500.0),
        )
    ])
    write_table("gw_params.csv", [
        {"city_code": city, "gw_exploitable_annual": allowance,
         "initial_over": 0.0, "gw_over_decay": 0.98}
        for city, allowance in (("SYN_CITY_A", 150.0), ("SYN_CITY_B", 240.0))
    ])
    write_table("infra/transfers.csv", [{
        "id": "SYN_TRANSFER", "name": "Synthetic transfer", "source_node_id": "SB_SYN_A",
        "source_type": "RIVER", "intake_capacity": 40.0, "annual_plan": 360.0,
        "loss_rate": 0.05, "priority": 1, "start_year": 1900, "end_year": 0,
        "dynamic_plan": "uniform", "status": "built", "scenario_group": "all",
        "min_source_ratio": 0.0, "min_source_flow": 0.0,
        "receiving_nodes": json.dumps({"DM_SYN_B": 1.0}),
        "monthly_plan": ",".join(["30"] * 12),
    }])


def main() -> None:
    generate_inputs()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    config = ModelConfig(
        name="WISAM synthetic example",
        time=TimeConfig(start_year=2000, end_year=2001),
        runoff_method=RunoffMethod.COEFFICIENT, et_method=ETMethod.INPUT,
        enable_snowmelt=0, enable_gw_overexploit=0, enable_dynamic_demand=0,
        enable_reservoirs=0, enable_lakes=0, enable_diversions=0,
        enable_transfers=1, enable_demands=1, enable_return_flows=1,
        enable_groundwater_allocation=1, enable_water_rights=0,
        tennant_level="fair", eco_warmup_years=0,
        local_surface_access_ratio=0.35,
    )
    # WISAM_OUTPUT_DIR is set before the first model import, isolating every
    # optional generated-table lookup to this example's output directory.
    forbidden_generated = {"calibrated_params.csv", "runoff_monthly.csv", "baseflow_monthly.csv", "gw_recharge_monthly.csv"}
    assert not any(path.name in forbidden_generated for path in OUTPUT.rglob("*.csv")), "Keep the example output free of precomputed hydrology/calibration inputs."
    api = ModelAPI().setup_with_config(config, data_dir=str(DATA))
    results = api.run()

    annual = results["summary_city_annual"]
    values = annual[[ANNUAL_DEMAND, ANNUAL_SUPPLY, ANNUAL_SHORTAGE]].to_numpy(dtype=float)
    assert values.size and np.isfinite(values).all(), "Annual accounting must be finite."
    assert (values >= -1e-8).all(), "Demand, supply and shortage must be nonnegative."
    assert np.allclose(values[:, 0], values[:, 1] + values[:, 2], atol=0.15, rtol=0), "Demand = supply + shortage."
    assert float(annual[ANNUAL_SHORTAGE].sum()) > 0, "Dry synthetic conditions must expose shortages."
    yearly_shortage = annual.groupby("year")[ANNUAL_SHORTAGE].sum()
    assert yearly_shortage.loc[2001] > yearly_shortage.loc[2000], "The synthetic dry year must have more shortage."

    monthly_rows = []
    for node in api.simulator.network.get_nodes(NodeType.DEMAND):
        for sector, demand in node.demands.items():
            supply, shortage = node.allocation[sector], node.shortage[sector]
            assert np.isfinite([demand, supply, shortage]).all()
            assert min(np.min(demand), np.min(supply), np.min(shortage)) >= -1e-8
            assert np.allclose(demand, supply + shortage, atol=1e-7, rtol=0)
            for date, d, a, s in zip(config.time.time_index, demand, supply, shortage):
                monthly_rows.append({"date": date.date().isoformat(), "node_id": node.id,
                                     "sector": sector, "demand_wan_m3": d,
                                     "supply_wan_m3": a, "shortage_wan_m3": s})
    pd.DataFrame(monthly_rows).to_csv(OUTPUT / "synthetic_demand_monthly.csv", index=False, encoding="utf-8-sig")

    audit = results["audit_monthly"]
    balance_columns = ["allocation_balance_error", "demand_balance_error", "transfer_balance_error"]
    errors = audit[balance_columns].to_numpy(dtype=float)
    assert len(audit) == 24 and np.isfinite(errors).all()
    assert np.abs(errors).max() < 1e-6, "Monthly numerical accounting must close."
    source_mix = results["source_mix_city_annual"]
    assert np.isfinite(source_mix[["groundwater", "transfer", "total_supply"]].to_numpy(dtype=float)).all()
    assert float(source_mix["groundwater"].sum()) > 0, "Groundwater must be exercised."
    assert float(source_mix["transfer"].sum()) > 0, "The synthetic transfer must be exercised."

    hydro_rows = []
    for node in api.simulator.network.get_nodes(NodeType.SUBBASIN):
        assert not node.use_precomputed, "No precomputed hydrology may enter the demonstration."
        for date, runoff, recharge in zip(config.time.time_index, node.total_runoff, node.gw_recharge):
            assert np.isfinite([runoff, recharge]).all() and runoff >= 0 and recharge >= 0
            hydro_rows.append({"date": date.date().isoformat(), "unit_id": node.unit_id,
                               "runoff_wan_m3": runoff, "recharge_wan_m3": recharge})
    pd.DataFrame(hydro_rows).to_csv(OUTPUT / "synthetic_hydrology.csv", index=False, encoding="utf-8-sig")
    for key in ("summary_city_annual", "source_mix_city_annual", "audit_monthly", "transfer_summary"):
        results[key].to_csv(OUTPUT / f"{key}.csv", index=False, encoding="utf-8-sig")
    report = {
        "data_origin": "Deterministic synthetic example data.",
        "python_version": sys.version.split()[0], "numpy_version": np.__version__, "pandas_version": pd.__version__,
        "months": 24, "hydrological_units": 2, "demand_nodes": 2, "transfer_projects": 1,
        "demand_wan_m3": float(annual[ANNUAL_DEMAND].sum()),
        "supply_wan_m3": float(annual[ANNUAL_SUPPLY].sum()),
        "shortage_wan_m3": float(annual[ANNUAL_SHORTAGE].sum()),
        "groundwater_supply_wan_m3": float(source_mix["groundwater"].sum()),
        "transfer_supply_wan_m3": float(source_mix["transfer"].sum()),
        "annual_shortage_wan_m3": {str(year): float(amount) for year, amount in yearly_shortage.items()},
        "max_monthly_accounting_residual_wan_m3": float(np.abs(errors).max()),
        "assertions": "passed",
    }
    (OUTPUT / "verification.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
