# Modeling and usage notes

WISAM 1.0.0 uses monthly conceptual hydrology and rule-based water allocation. The following assumptions and configuration details guide its application.

## Method assumptions

- **Time scale and routing.** River routing represents monthly residence storage and losses. Use the model for monthly water-balance and allocation analysis; daily operational constraints, hydraulic water levels and flood peaks require finer-resolution methods.
- **Groundwater.** Groundwater supply is represented through allowable pumping, conceptual recharge, accumulated overuse and optional annual recharge budgets. Aquifer heads and spatial drawdown are outside this representation. Interpret groundwater supply share as a source-accounting indicator. Recharge budgets using a complete annual series assume that annual recharge information is available.
- **Runoff and PET.** Cached runoff takes precedence over conceptual runoff calculations. The full-series ABCD+snow generator and monthly fallback have different options and initial conditions; use a consistent pathway across comparisons. The `penman` option implements a Hargreaves-based approximation multiplied by 1.1. Missing meteorological inputs invoke the documented defaults.
- **Storage and ecology.** Reservoir/lake evaporation uses empirical storage-area relationships. Keep their coefficients and units consistent with the application. Ecological allocation demand, Tennant-style node baseflow and control-section minimum flows are separate quantities. Node baseflow estimates are updated periodically from simulated information.
- **Allocation and source accounting.** Priority and proportional allocation evaluate specified management rules. Source priorities, service areas, return-flow routing and policy tables determine outcomes. Distinguish project withdrawal, post-loss project delivery and final demand-side supply, particularly for serial transfers.

## Configuration and input handling

- **Check input coverage.** `units.csv` is required. Other files may be skipped or replaced by defaults when absent. Check loaded counts, topology, climate coverage and process switches. Monthly data gaps can be interpolated or filled; missing demand sectors are zero. Unresolved topology references can be skipped.
- **Provide operating limits explicitly.** A missing transfer intake capacity falls back to the largest supplied monthly plan or the annual plan. Water-right controls require a valid quota file. Reservoir/transfer online-year and scenario filters are not applied uniformly to other facility types. Consult [Data interface](DATA_INTERFACE.md) for each field.
- **Manage caches per run.** Set `WISAM_OUTPUT_DIR` before importing the model and retain the effective cache files with the run configuration. Cached calibration parameters override matching input parameters. With cached runoff but no baseflow/recharge series, baseflow is estimated as 30% of runoff and recharge as 80% of baseflow.
- **Apply year policies by domain.** Fixed-year policies control demand lookup, infrastructure availability, source structure and unconventional supply. Climate and cached hydrology follow their input dates; land-use updates follow calendar years. Use a fresh model instance for each scenario and account for antecedent storage when comparing selected periods.
- **Select the required outputs.** Standard export includes detailed time series for at most 30 demand nodes. Use the API accessors for further node-level extraction. Export directories can contain additional sidecar tables, so use separate destinations for independent runs.
