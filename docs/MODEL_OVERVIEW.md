# WISAM 1.0.0: model overview

WISAM (Water network Integrated Simulation and Allocation Model; 水网综合模拟与配置模型) simulates monthly hydrology, infrastructure operation and water allocation across connected river basins.

## Scope and computational structure

WISAM is a deterministic monthly simulator. It represents runoff-producing units, river channels, demand units, groundwater supply, reservoirs, lakes, diversions, transfer projects and ecological control sections. Demand nodes normally represent non-mainstream basin–city intersection units (三级流域—地市需求单元); city and basin results aggregate those units.

The model evaluates configured operating rules using sector-priority or proportional allocation. Scenario settings control infrastructure availability, source management, demand and ecological requirements.

| Layer | Main source modules | Role |
|---|---|---|
| Configuration and interface | `config.py`, `api/interface.py`, `scenario/` | Time range, process switches, input-year policies and scenario configuration |
| Input and topology | `data_io/loader.py`, `data_io/topology.py`, `coupling/` | Read tables, map city demand to units, construct supply/return links and infrastructure attachments |
| Hydrology | `hydro/` | Precomputed monthly runoff input or conceptual runoff calculation; PET, snow and land-use options |
| Storage and management | `water/`, `management/source_builder.py`, `engineering/` | Reservoir/lake operation, project plans, ecological requirements, groundwater allowances and water-right accounting |
| Simulation and reporting | `engine/simulator.py`, `management/results_builder.py` | Monthly updates, source allocation, state histories and diagnostic result tables |

Within each month the simulator updates runoff, routes water and allocates sources, evaluates ecological controls, then updates groundwater management accounting. January updates may alter land-use-related parameters; ecological baseflow estimates are updated every five simulated years. Reservoir, lake, channel and groundwater-management states carry memory from preceding months.

## Processes represented

- **Hydrology:** cached runoff is used where available. Otherwise the simulator calls ABCD, GR2M or coefficient-based monthly functions. The full-series runoff generator and monthly fallback have different options and initial conditions.
- **Network and storage:** river/tributary links move water through the supplied topology, with configured losses and monthly channel residence storage. Reservoir and lake rules use storage, release and ecological settings. Facility subgraphs connect and order infrastructure at a shared river anchor.
- **Demand and allocation:** annual city demand is split to demand units, initially distributed over twelve months, and optionally redistributed by the dynamic-demand module. Priority allocation orders domestic, ecological, industrial and agricultural demand; source availability and source priorities also constrain delivery.
- **Groundwater:** an exploitable-volume allowance, recharge information and an accumulated-overuse penalty support groundwater allocation. Optional recharge-balance modes impose an annual accounting budget.
- **Transfers and diversions:** annual/monthly plans, receiving weights, source conditions, losses and capacities determine realized delivery. Project withdrawal, post-loss project delivery and final demand-side supply are distinct quantities.
- **Ecology:** ecological allocation demand, node baseflow requirements estimated by a Tennant-style rule, and explicit control-section minimum flows are reported as separate components.
- **Institutional settings:** optional water-right quotas and infrastructure-year/scenario filters apply the supplied policy and infrastructure assumptions.

## Running the model

`ModelAPI.setup_with_config(config, data_dir=...)` retains the complete `ModelConfig`. `run()` returns result tables; `export(filepath)` writes an Excel workbook or a CSV fallback, depending on installed optional Excel libraries. Use a fresh model instance for each independent comparison and retain the effective configuration with its input inventory.

Run the synthetic example from the package root:

```text
python examples/synthetic_demo.py
```

The example generates synthetic inputs, runs the model and checks water-accounting results.

Set the `WISAM_OUTPUT_DIR` environment variable **before importing model modules** to select the hydrology-cache and generated-file root. The default is the installation's `output` directory. The synthetic example uses a separate output location.

See [Data interface](DATA_INTERFACE.md) for input and output formats and [Modeling and usage notes](KNOWN_LIMITATIONS.md) for method assumptions and configuration guidance.
