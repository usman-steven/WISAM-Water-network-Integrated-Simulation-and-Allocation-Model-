"""Project-level display names and standard output locations."""

from pathlib import Path


MODEL_NAME = "WISAM"
MODEL_VERSION = "1.0.0"
MODEL_FULL_NAME = "Water network Integrated Simulation and Allocation Model"
MODEL_CN_NAME = "WISAM 水网综合模拟与配置模型"
MODEL_LEGACY_NAME = "WNSAM"
MODEL_LEGACY_NAMES = ("WNSAM", "HydroNet-Alloc")
DEFAULT_NETWORK_NAME = "WISAM-4B"

OUTPUT_DIR = Path("output")
STANDARD_RESULTS_DIR = OUTPUT_DIR / "results" / "hydroalloc"
STANDARD_TABLES_DIR = STANDARD_RESULTS_DIR / "tables"
STANDARD_FIGURES_DIR = STANDARD_RESULTS_DIR / "figures"
STANDARD_REPORTS_DIR = STANDARD_RESULTS_DIR / "reports"
