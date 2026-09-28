# WISAM

**Water network Integrated Simulation and Allocation Model · Version 1.0.0**

WISAM integrates monthly hydrological processes, water-network infrastructure and multi-source water allocation. It represents runoff generation, reservoir and lake operations, inter-basin transfers, groundwater supply, sectoral demand and ecological water requirements.

**Author:** Wang Lichuan  
**Affiliation:** China Institute of Water Resources and Hydropower Research  
**License:** [MIT](LICENSE)

[中文](README.md)

## Installation

Python ≥3.11; Python 3.12 is recommended. From the project directory:

```text
python -m venv .venv
# Windows
.venv\Scripts\python.exe -m pip install -r requirements.txt
# macOS/Linux
.venv/bin/python -m pip install -r requirements.txt
```

## Quick start

Run the synthetic example (Windows PowerShell):

```powershell
.\.venv\Scripts\python.exe examples/synthetic_demo.py
.\.venv\Scripts\python.exe run_demo.py --list-scenarios
```

On macOS/Linux, use `.venv/bin/python` for these commands.

The example simulates 24 months for two runoff units and two demand nodes, with groundwater supply and a transfer project. Generated input and output files are stored in `examples/data/` and `examples/output/`.

Run a case with your input data:

```powershell
$env:WISAM_OUTPUT_DIR = './output/case01'
.\.venv\Scripts\python.exe run_demo.py --data-dir data --start-year 2024 --end-year 2024 --output output/case01/results.xlsx
```

Set `WISAM_OUTPUT_DIR` before starting Python to select a separate output and hydrology-cache directory (macOS/Linux: `export WISAM_OUTPUT_DIR=./output/case01`). Use matching input data and caches for each case.

## Documentation

- [Model overview](docs/MODEL_OVERVIEW.md)
- [Data interface](docs/DATA_INTERFACE.md)
- [Dependencies](docs/DEPENDENCIES.md)
- [Modeling and usage notes](docs/KNOWN_LIMITATIONS.md)
- [Synthetic example](examples/README.md)
- [Data and examples](DATA_POLICY.md)
- [Changelog](CHANGELOG.md)

The Python API is `api.interface.ModelAPI`; the command-line entry point is `run_demo.py`.

## Verification and citation

Run `python tools/verify_package.py` to verify release-file SHA-256 checksums. Software execution checks are recorded in `verification/`.

Please cite WISAM using [CITATION.cff](CITATION.cff) and specify the version used.

