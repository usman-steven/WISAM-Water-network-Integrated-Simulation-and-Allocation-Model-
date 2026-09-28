# WISAM 1.0.0: data interface

This document describes the file formats used by `DataLoader`, `TopologyBuilder`, `ModelConfig` and the simulator. Input-table paths are relative to the `data_dir` passed to `ModelAPI`. Supply optional files for the processes enabled in the application; absent files use the defaults described below.

## Common conventions

- Simulation steps are calendar months, January through December inclusive. `TimeConfig` uses month-start timestamps (`YYYY-MM-01`). Specify the simulation years explicitly for a study.
- Model water quantities are generally **10^4 m³ (万m³)**: annual demand/plan fields are annual volumes; monthly arrays, intake capacities and minimum-flow settings are monthly volumes. They are not discharge in m³/s. Convert discharge to the appropriate monthly volume before supplying it. Storage fields are volumes. Convert 万m³ to 亿m³ by dividing by 10,000.
- Runoff-unit area is km²; climate precipitation/PET is mm/month; temperature is °C. Conceptual runoff functions convert mm × km² to 万m³ with a factor of 0.1. Supply/return/loss weights are dimensionless fractions unless explicitly named otherwise.
- Prefer UTF-8 CSV. The reader also attempts UTF-8 with BOM, GBK, GB18030 and Latin-1. Identifiers must match exactly across files; pandas type inference can discard leading zeros in numeric-looking IDs. Use consistent identifiers and check loaded mappings.
- Monthly wide tables use a date column first, followed by one column per **raw unit ID**, not `SB_...` node ID. If at least 80% of dates parse, rows are aligned to the configured month starts, duplicate dates keep the first row, and missing values are time-interpolated then forward/backward filled. Otherwise values are read in row order, truncated or edge-padded. Remaining nonnumeric values become zero. Supply complete month-start series to avoid unintended interpolation.

## Core inputs

| File | Required fields / supported fields | Requirement and interpretation |
|---|---|---|
| `units.csv` | `UID`, `CID`, `WID`, `AREA`, `干流`; optional `UNAME`, `CNAME`, `WNAME`, `basin`, `basin_l2`, `province`, `x`, `y` | The only file whose absence explicitly raises a required-input error. Lower-case equivalents `uid`, `cid`, `wid`, `area`, `is_mainstream`, `uname`, `cname`, `wname` are accepted. `干流`/`is_mainstream` is 0 for runoff units and 1 for channel units. Unique IDs and positive areas should be checked before loading. |
| `topology.csv` | `from_uid`, `to_uid`; optional `loss_rate`, `river_name` | Directed connections between raw unit IDs or resolvable node IDs. Falls back to `channel_topology.csv` if missing. Invalid/unresolved/self-connections are skipped. Missing topology emits a warning; the builder may still add inferred tributary/supply/return connections. |
| `climate/precip.csv` | First date column; unit-ID columns | Precipitation in mm/month for runoff calculation. Missing precipitation can fall back to zero. |
| `climate/pet.csv` | Same wide format | PET in mm/month. Missing series triggers the configured PET fallback. |
| `climate/temp_mean.csv` | Same wide format | °C, used by snow/PET processes. Missing temperature may fall back to 15 °C. |
| `climate/temp_max.csv`, `climate/temp_min.csv` | Same wide format | Optional °C series for Hargreaves PET; absent extremes are approximated from mean temperature. |
| `hydro_params.csv` | `unit_id`; optional `param_a`, `param_b`, `param_c`, `param_d`, `param_x1`, `param_x2`, `runoff_coeff`, `et_reduction`, `baseflow_index`, `is_high_altitude`, `eco_priority` | Missing fields retain node defaults. ABCD `param_b` and GR2M `param_x1` are conceptual storage depths in mm; other listed hydrological coefficients are dimensionless. `is_high_altitude` is an integer flag. |
| `demand/annual_demand.csv` | `city_code`, `year`; any of `domestic`, `industrial`, `agricultural`, `ecological` | Annual sector demand in 万m³/year. Needed for a meaningful allocation run with demand. Missing sectors remain zero. One input year is repeated; multiple years are linearly interpolated, with endpoint values outside the supplied range. |

If `basin` is absent, the builder infers Chinese basin names from the first letter of `WID` (`D`, `E`, `C`, `F`); other prefixes become unknown. Each non-mainstream unit produces `SB_<UID>` and `DM_<UID>` nodes; mainstream units produce `CH_<UID>`. Demand is split within a city by non-mainstream unit area unless explicit split weights are supplied. An optional `shp/unit.shp` with `UID` may provide centroids if useful `x,y` columns are absent; this requires geopandas and is not needed for the synthetic example. Coordinates used by PET should be longitude/latitude degrees, not projected metres.

## Hydrology caches: outside `data_dir`

`output_paths.resolve_output_file()` searches `WISAM_OUTPUT_DIR/cache/hydro/` first and then `WISAM_OUTPUT_DIR/`. Without the environment override, the root is the installation's `output/`. Set the variable before importing the model. The following files are automatically discovered:

| File | Contract and precedence |
|---|---|
| `calibrated_params.csv` | `unit_id`, optional `param_a`–`param_d`; overrides matching values loaded from `hydro_params.csv`. |
| `runoff_monthly.csv` | Monthly wide table; 万m³/month. A matched unit uses these values instead of simulated conceptual runoff. |
| `baseflow_monthly.csv` | Same format/unit. If absent for a unit using cached runoff, baseflow defaults to 30% of runoff. |
| `gw_recharge_monthly.csv` | Same format/unit. If absent for a unit using cached runoff, recharge defaults to 80% of the estimated/provided baseflow. |

Use a separate cache directory for each configuration and retain its inventory with the run inputs.

## Optional demand, supply and physical tables

| File | Columns consumed and meaning |
|---|---|
| `demand/unit_demand_split_factors.csv` | `city_code`, `unit_id`; optional `domestic_weight`, `industrial_weight`, `agricultural_weight`, `ecological_weight`, `unconventional_weight`, `groundwater_weight`, and corresponding `*_basis` descriptions. Nonnegative fractions replace the area split where present; check sums by city/sector because the loader does not automatically normalize this table. |
| `demand/demand_calibration_factors.csv` | `year` column plus first available of `demand_factor`, `factor`, `scale`; one of `demand_node_id`, `unit_id`, `city_code`, `province`, and optional `sector`. Blank year means a time-invariant factor. Factors multiply demand. |
| `return_ratios.csv` | `city_code` plus any of `domestic_return`, `industrial_return`, `agricultural_return`, `ecological_return`; dimensionless sector return fractions. Missing table retains node defaults. |
| `gw_params.csv` | `city_code`; `gw_exploitable_annual` (万m³/year), `initial_over` (万m³), `gw_over_decay` (dimensionless, default 0.98). Creates groundwater supply nodes for matched demand units, with city values split using groundwater weights. |
| `unconventional_supply.csv` | `city_code`, `year`, and first available of `annual_supply`, `annual_supply_wan`, `unconventional_supply`, `other_supply`; 万m³/year. Positive matched entries form supply targets. |
| `source_structure_factors.csv` | One of `demand_node_id`, `unit_id`, `city_code`, `province`; optional `year`; any of `surface_factor`, `groundwater_factor`, `unconventional_factor`, `local_sw_factor`, `reservoir_factor`, `diversion_factor`, `transfer_factor`, `lake_factor`. Dimensionless adjustments; record these alongside other effective parameters. |
| `physical/soil_params.csv` | `unit_id`; optional `soil_type`, `sand_pct`, `clay_pct`, `soil_depth_mm`, `field_capacity`, `wilting_point`, `permeability`. Percentages are 0–100; moisture fractions are dimensionless. Used for heuristic initial parameter estimates where the default ABCD storage parameter remains 200 mm. |
| `physical/terrain_params.csv` | `unit_id`, optional `mean_elev`, `mean_slope`, `drainage_density`; used by initial-parameter heuristics. Elevation is interpreted in metres, including the 3000 m high-altitude threshold. |
| `physical/landuse.csv` | `unit_id`, `year`, optional `cropland_pct`, `forest_pct`, `grassland_pct`, `urban_pct`, `water_pct`, `unused_pct`, `irrigated_area_km2`; percentages 0–100, area km². Supports annual parameter/demand adjustments. |

## Optional infrastructure and ecological inputs

The table below gives identifiers and operational fields. Optional descriptive fields include `name`, `basin`, `province`, `x`, `y` where consumed by the loader. A missing file omits its facilities. Supply capacities and valid network attachments for each facility.

| File | Operational fields |
|---|---|
| `infra/reservoirs.csv` | `id`, `channel_uid`; `total_capacity`, `normal_storage`, `flood_limit_storage`, `dead_storage` (万m³); `initial_ratio` (default 0.5 of usable storage); `flood_months` (quoted comma-separated month numbers), `evap_01`–`evap_12` (mm/month), `seepage_rate`, `area_coeff`, `order`; `status`, `online_year`, `retire_year`, `scenario_group`. |
| `infra/lakes.csv` | `id`, `channel_uid`; `max_storage`, `normal_storage`, `dead_storage`, `eco_min_storage` (万m³); `outflow_capacity` (万m³/month), `initial_ratio`, `area_coeff`. This loader does not read the reservoir-style online-year/status/scenario fields for lakes. |
| `infra/transfers.csv` | `id`, `source_node_id`, `receiving_nodes` (quoted JSON map of receiver reference to weight), `source_type`; `annual_plan` (万m³/year), `monthly_plan` (quoted 12-value monthly volumes), `intake_capacity` (万m³/month), `dynamic_plan`, `loss_rate`, `priority`, `min_source_ratio`, `min_source_flow` (万m³/month); `status`, `scenario_group`, `online_year`/`start_year`, `retire_year`/`end_year`. |
| `infra/diversions.csv` | `id`, `channel_uid`, `supply_to` (JSON target-weight map), `max_capacity`, `priority`, `diversion_type`, `eco_reserve_ratio`, `eco_reserve_min_flow`, `downstream_min_ratio`, `downstream_min_flow`. Capacity/minimum-flow quantities are monthly volumes. Read only when diversions are enabled. |
| `infra/facility_links.csv` | `anchor_node_id` or `unit_id`, `from_node`, `to_node`, `split_ratio`, `priority`. Defines connections inside a facility subgraph; `__ENTRY__` and `__OUTLET__` are supported boundary references. |
| `ecology/eco_control.csv` | `id`, `channel_uid`, `type` (`control` or `ocean`), `river_name`, `min_eco_flow` (quoted 12 monthly volumes in 万m³); optional `target_section`, `calibration_role`, `scope_note`. Short monthly vectors are zero-padded. |

Reservoir/lake/diversion `channel_uid` normally uses the raw unit ID; transfer `source_node_id` uses a network node ID. Receiver references can identify existing demand nodes or be expanded from recognized city/unit references. Check resolved connections in the supply-topology output.

A positive explicit monthly transfer vector overrides dynamic plan selection. Otherwise `dynamic_plan` supports automatic, uniform, demand-weighted, flow-weighted and hybrid behavior. With a positive annual plan and no positive intake capacity, the loader uses the maximum supplied monthly plan, or the entire annual plan as the monthly capacity fallback. Supply explicit intake capacities when engineering limits are available.

`status=planned` is filtered according to `include_planned_infrastructure`; `scenario_group` is compared with `active_infrastructure_scenario` for reservoirs and transfers. Their availability years are evaluated through the infrastructure input-year policy. Other facility types do not necessarily implement those same filters.

Additional optional files are the configured water-right policy CSV (default `policies/yellow_river_1987_allocation_plan.csv`) and `process/ocean_process_factors.csv`. Water rights require `province`, optional `quota_key`, and positive `annual_quota_wan_m3` or `annual_quota_yi_m3`; optional `m01`–`m12` weights are normalized. The process-factor file uses `scope_prefix`, optional `enabled`, sector `*_return_cap` fields, `local_surface_access_ratio`, and channel/link loss additions, multipliers and caps. These process factors are skipped in the natural preset. Missing water-right input can leave the control inactive despite an enabled switch; check load logs and exported policy/audit tables.

## Input-year policies

`ModelConfig.set_input_year_policy(domain, policy, fixed_year)` accepts `calendar` or `fixed`. Calendar uses the simulation year; fixed selects the declared reference year and raises an error when resolved without a fixed year. Supported application points include demand lookup, infrastructure availability, source-structure factors and unconventional supply. The grouped `allocation_inputs` policy supplies a fallback for demand/source-structure/unconventional domains.

To combine historical hydrology with a fixed reference-year network, configure demand, infrastructure, source structure and unconventional supply separately. Climate and precomputed hydrology are aligned directly to the simulation time axis; changing their reference period requires changing the input series. Land-use updates use the simulation calendar year.

## Result tables and export

`run()` returns a dictionary of pandas tables and additional model results. Important table families are:

| Result keys | Interpretation |
|---|---|
| `summary_unit`, `summary_city`, `summary_basin`, `summary_city_total`, `summary_basin_total` | Demand, supply, shortage, return flow and consumption; period tables contain annualized volumes. Chinese column labels retain units, e.g. `年均供水万m³`. |
| `summary_annual`, `summary_city_annual`, `summary_basin_annual` | Calendar-year volumes, with `year`; do not sum annualized period tables as though they were monthly records. |
| `source_mix_summary`, `source_mix_unit_total`, `source_mix_city_total`, `source_mix_annual`, `source_mix_city_annual` | Demand-side source accounting: `local_sw`, `groundwater`, `unconventional`, `reservoir`, `diversion`, `transfer`, `historical_closure`, plus `lake` where applicable. |
| `transfer_summary` | Plan withdrawal, actual withdrawal, post-loss delivery, gap and utilization. Project totals can differ from final demand-side transfer supply. |
| `reservoir_summary`, `ecology_summary`, `eco_baseflow_summary` | Storage summaries and separate ecological-control/baseflow diagnostics. |
| `node_topology_summary`, `link_topology_summary`, `supply_topology_detail`, `return_flow_topology_detail`, `groundwater_topology_summary` | Constructed network and supply/return relationships. |
| `audit_summary`, `audit_monthly`, `reservoir_balance_audit`, `lake_balance_audit`, `water_rights_*` | Water-accounting residuals, storage balances and water-right usage diagnostics. |

`ModelAPI` provides demand, reservoir, subbasin and channel time-series accessors. Standard export writes nonempty result tables to Excel if openpyxl/xlsxwriter is available, otherwise to `<filename_stem>_csv/`. It exports detailed demand time series for at most the first 30 demand nodes. Use the accessors or returned tables for complete extraction. Existing local sidecar tables may also enter an export; use an isolated output directory and inspect the resulting inventory.
