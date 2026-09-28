"""Canonical locations for HydroNet-Alloc generated files.

The standard deliverable lives under ``output/results/hydroalloc``. Files in
``output/cache`` are script inputs/intermediate products kept out of the output
root so the directory stays readable.
"""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT = Path(os.environ.get("WISAM_OUTPUT_DIR", str(ROOT / "output"))).expanduser().resolve()
CACHE = OUTPUT / "cache"

HYDRO_CACHE = CACHE / "hydro"
TOPOLOGY_CACHE = CACHE / "topology"
AUDIT_CACHE = CACHE / "audit"
OCEAN_CACHE = CACHE / "ocean"
GEODATA_CACHE = CACHE / "geodata"


HYDRO_FILES = {
    "calibrated_params.csv",
    "runoff_monthly.csv",
    "baseflow_monthly.csv",
    "gw_recharge_monthly.csv",
    "natural_calibration_targets_long_series.csv",
}

TOPOLOGY_FILES = {
    "allocation_topology_node_points_2024.csv",
    "allocation_supply_topology_detail_2024.csv",
    "allocation_return_flow_topology_detail_2024.csv",
    "allocation_topology_supply_link_counts_2024.csv",
    "allocation_topology_source_supply_totals_2024.csv",
    "allocation_topology_summary_2024.csv",
}

AUDIT_FILES = {
    "model_build_2024_source_compare.csv",
    "model_build_demand_topology.csv",
    "model_build_issues.csv",
    "model_build_long_series_targets.csv",
    "model_build_requirements_report.md",
    "model_build_topology_counts.csv",
    "output_cleanup_latest.csv",
}


def output_file(name: str) -> Path:
    """Return the canonical path for a generated output file."""
    if name in HYDRO_FILES:
        return HYDRO_CACHE / name
    if name in TOPOLOGY_FILES:
        return TOPOLOGY_CACHE / name
    if name in AUDIT_FILES:
        return AUDIT_CACHE / name
    if name.startswith("ocean_"):
        return OCEAN_CACHE / name
    if name.startswith("ne_10m_lakes"):
        return GEODATA_CACHE / name
    return OUTPUT / name


def resolve_output_file(name: str) -> Path:
    """Prefer the canonical cache path, with legacy root fallback."""
    path = output_file(name)
    if path.exists():
        return path
    legacy = OUTPUT / name
    if legacy.exists():
        return legacy
    return path


def ensure_output_parent(name: str) -> Path:
    path = output_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def ensure_cache_dirs() -> None:
    for path in [HYDRO_CACHE, TOPOLOGY_CACHE, AUDIT_CACHE, OCEAN_CACHE, GEODATA_CACHE]:
        path.mkdir(parents=True, exist_ok=True)
