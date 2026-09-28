"""
Data loading utilities for the HydroNet-Alloc water-network model.

The loader reads CSV inputs, builds the network topology, and attaches
climate, hydrology, demand, infrastructure, groundwater, and ecology data.
"""
import os
import json
import numpy as np
import pandas as pd
from typing import Optional
from collections import defaultdict

from config import ModelConfig, NodeType, LinkType, Sector
from core.nodes import (SubbasinNode, DemandNode, GroundwaterNode, ReservoirNode,
                         LakeNode, RiverDiversionNode, RiverChannelNode,
                         OceanNode, EcoControlNode)
from core.links import SupplyLink
from core.network import WaterNetwork
from data_io.topology import TopologyBuilder
from model_metadata import MODEL_NAME
from output_paths import resolve_output_file
from water.water_rights import WaterRightsScheme

class DataLoader:

    def __init__(self, data_dir: str, config: ModelConfig):
        self.data_dir = data_dir
        self.config = config
        self.builder = TopologyBuilder(config)
        self.network: Optional[WaterNetwork] = None
        self.n_steps = config.time.n_steps
        self.landuse_mod = None

    def _read_file(self, filepath) -> Optional[pd.DataFrame]:
        """Read a CSV file with several common encodings."""
        if not os.path.exists(filepath):
            return None

        for encoding in ["utf-8-sig", "utf-8", "gbk", "gb18030", "latin1"]:
            try:
                return pd.read_csv(filepath, encoding=encoding)
            except Exception:
                continue

        print(f"    [WARN] unable to read CSV: {filepath}")
        return None

    def _output_path(self, filename: str) -> str:
        return str(resolve_output_file(filename))

    def _time_aligned_values(self, df: pd.DataFrame, column: str) -> np.ndarray:
        """Return a monthly series aligned to ``config.time.time_index``.

        Time-series input files usually carry a date column. If a table has no
        usable date column, values are consumed in row order and padded/sliced
        to the configured simulation length.
        """
        values = pd.to_numeric(df[column], errors='coerce')
        date_col = df.columns[0] if len(df.columns) > 0 else None
        dates = pd.to_datetime(df[date_col], errors='coerce') if date_col else None

        if dates is not None and dates.notna().sum() >= max(1, int(len(df) * 0.8)):
            series = pd.Series(values.values.astype(float), index=dates)
            series = series[series.index.notna()]
            series = series[~series.index.duplicated(keep='first')]
            aligned = series.reindex(self.config.time.time_index)
            if aligned.isna().any():
                aligned = aligned.interpolate(method='time', limit_direction='both')
                aligned = aligned.ffill().bfill()
            return np.nan_to_num(aligned.values.astype(float))

        arr = values.values.astype(float)
        arr = np.nan_to_num(arr)
        if len(arr) < self.n_steps:
            arr = np.pad(arr, (0, self.n_steps - len(arr)), mode='edge')
        elif len(arr) > self.n_steps:
            arr = arr[:self.n_steps]
        return arr

    def _clean_str(self, value, default: str = "") -> str:
        if pd.isna(value):
            return default
        text = str(value).strip()
        if text.lower() in {"nan", "none", "null"}:
            return default
        return text

    def _safe_float(self, value, default: float = 0.0) -> float:
        if pd.isna(value):
            return default
        try:
            text = self._clean_str(value, "")
            return float(text) if text else default
        except Exception:
            return default

    def _safe_int(self, value, default: int = 0) -> int:
        if pd.isna(value):
            return default
        try:
            text = self._clean_str(value, "")
            return int(float(text)) if text else default
        except Exception:
            return default

    def _infra_row_enabled(self, row, default_status: str = "built") -> bool:
        status = self._clean_str(row.get('status', default_status), default_status).lower()
        scenario_group = self._clean_str(row.get('scenario_group', 'all'), 'all').lower()
        active_group = self._clean_str(
            getattr(self.config, 'active_infrastructure_scenario', 'all'), 'all'
        ).lower()

        if hasattr(self.config, 'is_process_enabled'):
            include_planned = self.config.is_process_enabled(
                'include_planned_infrastructure')
        else:
            include_planned = bool(getattr(self.config, 'include_planned_infrastructure', True))

        if status == 'planned' and not include_planned:
            return False

        if active_group not in {'', 'all'} and scenario_group not in {'', 'all', active_group}:
            return False

        return True

    def _default_monthly_evap(self, basin: str):
        defaults = {
            '黄河': np.array([15, 20, 30, 45, 65, 85, 98, 90, 70, 45, 25, 16], dtype=float),
            '海河': np.array([12, 18, 32, 50, 75, 95, 108, 100, 78, 50, 28, 15], dtype=float),
            '淮河': np.array([15, 20, 35, 55, 80, 100, 112, 105, 82, 55, 30, 18], dtype=float),
            '长江': np.array([18, 25, 35, 50, 70, 90, 100, 95, 75, 50, 30, 20], dtype=float),
        }
        return defaults.get(self._clean_str(basin, ''), np.array(
            [15, 20, 32, 48, 68, 88, 98, 93, 73, 48, 28, 16], dtype=float))

    def _resolve_anchor_uid(self, raw_uid: str,
                            asset_id: str = "",
                            asset_name: str = "",
                            river_name: str = "") -> str:
        """Resolve configured facility aliases onto current network UIDs."""
        if not raw_uid:
            return raw_uid

        for candidate in (raw_uid, f"CH_{raw_uid}", f"SB_{raw_uid}"):
            if self.network is not None and candidate in self.network.nodes:
                return raw_uid

        alias_map = {
            'RES_longyangxia': 'D02040563250099',
            'RES_liujiaxia': 'D02040862010099',
            'RES_sanmenxia': 'D06012214080099',
            'RES_xiaolangdi': 'D06042441900099',
            'RES_miyun': 'C01020113030099',
            'LK_hongze': 'E03010732080099',
            'OC_黄河': 'D07033037050099',
            'OC_淮河': 'E04040032070099',
            'OC_海河': 'C04010337140099',
            'ECO_huayuankou': 'D07032641020099',
            'GF_D010100_63270000': 'D01010163260099',
            'GF_D020100_63270000': 'D02040563250099',
            'GF_D020200_63270000': 'D02040862010099',
            'GF_D050700_63270000': 'D06042441900099',
            'GF_D060100_63270000': 'D06012214080099',
            'GF_D060200_63270000': 'D07033037050099',
            'GF_C020200_13080000': 'C01020113030099',
            'GF_E030100_34120000': 'E03010732080099',
            'GF_E050100_34120000': 'E04040032070099',
            'GF_C030200_13080000': 'C04010337140099',
        }

        for key in (asset_id, asset_name, raw_uid):
            mapped = alias_map.get(key, "")
            if not mapped:
                continue
            for candidate in (f"CH_{mapped}", f"SB_{mapped}", mapped):
                if self.network is not None and candidate in self.network.nodes:
                    return mapped

        return raw_uid

    def _load_hydro_params_with_calibration(self):
        """Load hydrology parameters and apply calibrated values from output/."""
        self._load_hydro_params()

        calib_df = self._read_file(self._output_path("calibrated_params.csv"))
        if calib_df is None:
            return

        calib_count = 0
        for _, row in calib_df.iterrows():
            uid = str(row.get('unit_id', '')).strip()
            sb_id = f"SB_{uid}"
            sb = self.network.nodes.get(sb_id)
            if not isinstance(sb, SubbasinNode):
                continue
            for attr in ['param_a', 'param_b', 'param_c', 'param_d']:
                if attr in row.index and pd.notna(row[attr]):
                    setattr(sb, attr, float(row[attr]))
            calib_count += 1

        print(f"    calibrated hydro params: {calib_count}")

    def _load_precomputed_runoff(self):
        """Load precomputed runoff, groundwater recharge and baseflow from output/."""
        file_map = {
            'runoff': self._output_path("runoff_monthly.csv"),
            'gw_recharge': self._output_path("gw_recharge_monthly.csv"),
            'baseflow': self._output_path("baseflow_monthly.csv"),
        }

        for var_name, filepath in file_map.items():
            df = self._read_file(filepath)
            if df is None:
                if var_name == 'runoff':
                    print(f"    missing precomputed runoff: {filepath}")
                continue

            loaded = 0
            for sb in self.network.get_nodes(NodeType.SUBBASIN):
                if not isinstance(sb, SubbasinNode):
                    continue
                uid = sb.unit_id
                if uid not in df.columns:
                    continue

                values = self._time_aligned_values(df, uid)
                values = np.maximum(values, 0)

                if var_name == 'runoff':
                    sb.precomputed_runoff = values
                    sb.use_precomputed = True
                elif var_name == 'gw_recharge':
                    sb.precomputed_gw_recharge = values
                elif var_name == 'baseflow':
                    sb.precomputed_baseflow = values

                loaded += 1

            print(f"    precomputed {var_name}: {loaded}")

    def _load_demand(self):
        """Load city demand and split it to tertiary-basin city demand units."""
        df = self._read_csv("demand", "annual_demand.csv")
        if df is None:
            print("    missing annual_demand.csv")
            return

        df['city_code'] = df['city_code'].astype(str)
        demand_factors = self._load_demand_calibration_factors()
        split_factors = self._load_unit_demand_split_factors()
        self.network._unit_demand_split_factors = split_factors
        all_years = list(range(self.config.time.start_year,
                               self.config.time.end_year + 1))
        demand_input_years = [
            self._resolve_input_year("demand", year)
            for year in all_years
        ]
        sector_cn = {
            'domestic': Sector.DOMESTIC.value,
            'industrial': Sector.INDUSTRIAL.value,
            'agricultural': Sector.AGRICULTURAL.value,
            'ecological': Sector.ECOLOGICAL.value,
        }

        demand_nodes = [
            dm for dm in self.network.get_nodes(NodeType.DEMAND)
            if isinstance(dm, DemandNode)
        ]
        loaded = 0
        for dm in demand_nodes:
            cid = dm.city_code
            city_data = df[df['city_code'] == cid]
            if len(city_data) == 0:
                continue

            city_data = city_data.sort_values('year')
            data_years = city_data['year'].values

            for sec_en, sec_cn in sector_cn.items():
                if sec_en not in city_data.columns:
                    continue
                sector_weight = self._demand_sector_weight(split_factors, dm, sec_en)
                dm.demand_sector_weights[sec_cn] = sector_weight
                data_vals = city_data[sec_en].values.astype(float)
                if len(data_years) == 1:
                    annual_interp = np.full(len(all_years), data_vals[0])
                else:
                    annual_interp = np.interp(
                        demand_input_years, data_years, data_vals)

                city_monthly = np.zeros(self.n_steps)
                for i, year in enumerate(all_years):
                    factor_year = demand_input_years[i]
                    factor = self._demand_calibration_factor(
                        demand_factors, dm, factor_year, sector_key=sec_en)
                    t_start = i * 12
                    city_monthly[t_start:t_start + 12] = (
                        annual_interp[i] * factor * sector_weight / 12.0
                    )

                dm.demands[sec_cn] = city_monthly
            loaded += 1

        print(f"    demand nodes loaded: {loaded}")

    def _resolve_input_year(self, domain: str, calendar_year: int) -> int:
        resolver = getattr(self.config, "resolve_input_year", None)
        if callable(resolver):
            return int(resolver(domain, int(calendar_year)))
        return int(calendar_year)

    def _load_demand_calibration_factors(self):
        """Load optional city/province-year demand scaling factors."""
        df = self._read_csv("demand", "demand_calibration_factors.csv")
        if df is None:
            return None

        value_col = None
        for col in ["demand_factor", "factor", "scale"]:
            if col in df.columns:
                value_col = col
                break
        if value_col is None or "year" not in df.columns:
            print("    [WARN] demand_calibration_factors.csv missing year/factor")
            return None

        factors = {
            "by_demand_sector": {},
            "by_demand_sector_year": {},
            "by_unit_sector": {},
            "by_unit_sector_year": {},
            "by_city_sector": {},
            "by_city_sector_year": {},
            "by_province_sector": {},
            "by_province_sector_year": {},
            "by_city": {},
            "by_city_year": {},
            "by_province": {},
            "by_province_year": {},
        }
        counts = {key: 0 for key in factors}
        sector_aliases = {
            "domestic": "domestic",
            "industrial": "industrial",
            "agricultural": "agricultural",
            "ecological": "ecological",
            "生活": "domestic",
            "工业": "industrial",
            "农业": "agricultural",
            "生态": "ecological",
            Sector.DOMESTIC.value: "domestic",
            Sector.INDUSTRIAL.value: "industrial",
            Sector.AGRICULTURAL.value: "agricultural",
            Sector.ECOLOGICAL.value: "ecological",
        }
        for _, row in df.iterrows():
            factor = self._safe_float(row.get(value_col, 1.0), 1.0)
            if factor < 0:
                continue
            demand_node_id = self._clean_str(row.get("demand_node_id", ""))
            unit_id = self._clean_str(row.get("unit_id", ""))
            city_code = self._clean_str(row.get("city_code", ""))
            province = self._clean_str(row.get("province", ""))
            sector_raw = self._clean_str(row.get("sector", ""))
            sector = sector_aliases.get(sector_raw, sector_raw.lower() if sector_raw else "")
            year_raw = row.get("year", "")
            year = None
            if pd.notna(year_raw) and self._clean_str(year_raw, ""):
                try:
                    year = int(float(year_raw))
                except Exception:
                    year = None
            if sector:
                if demand_node_id:
                    if year is None:
                        factors["by_demand_sector"][(demand_node_id, sector)] = factor
                        counts["by_demand_sector"] += 1
                    else:
                        factors["by_demand_sector_year"][(demand_node_id, year, sector)] = factor
                        counts["by_demand_sector_year"] += 1
                elif unit_id:
                    if year is None:
                        factors["by_unit_sector"][(unit_id, sector)] = factor
                        counts["by_unit_sector"] += 1
                    else:
                        factors["by_unit_sector_year"][(unit_id, year, sector)] = factor
                        counts["by_unit_sector_year"] += 1
                elif city_code:
                    if year is None:
                        factors["by_city_sector"][(city_code, sector)] = factor
                        counts["by_city_sector"] += 1
                    else:
                        factors["by_city_sector_year"][(city_code, year, sector)] = factor
                        counts["by_city_sector_year"] += 1
                elif province:
                    if year is None:
                        factors["by_province_sector"][(province, sector)] = factor
                        counts["by_province_sector"] += 1
                    else:
                        factors["by_province_sector_year"][(province, year, sector)] = factor
                        counts["by_province_sector_year"] += 1
            elif city_code:
                if year is None:
                    factors["by_city"][city_code] = factor
                    counts["by_city"] += 1
                else:
                    factors["by_city_year"][(city_code, year)] = factor
                    counts["by_city_year"] += 1
            elif province:
                if year is None:
                    factors["by_province"][province] = factor
                    counts["by_province"] += 1
                else:
                    factors["by_province_year"][(province, year)] = factor
                    counts["by_province_year"] += 1

        print(
            "    demand calibration factors loaded: "
            f"unit_sector_year={counts['by_unit_sector_year']}, "
            f"city_sector_year={counts['by_city_sector_year']}, "
            f"city={counts['by_city']}, city_year={counts['by_city_year']}, "
            f"province={counts['by_province']}, province_year={counts['by_province_year']}"
        )
        return factors

    def _demand_calibration_factor(
        self,
        factors,
        dm: DemandNode,
        year: int,
        sector_key: Optional[str] = None,
    ) -> float:
        if not factors:
            return 1.0
        demand_node_id = self._clean_str(getattr(dm, "id", ""))
        unit_id = self._clean_str(getattr(dm, "unit_id", ""))
        cid = self._clean_str(getattr(dm, "city_code", ""))
        province = self._clean_str(getattr(dm, "province", ""))
        year = int(year)
        sector_key = self._clean_str(sector_key or "")
        value = None
        if sector_key:
            value = factors["by_demand_sector_year"].get((demand_node_id, year, sector_key))
            if value is None:
                value = factors["by_unit_sector_year"].get((unit_id, year, sector_key))
            if value is None:
                value = factors["by_city_sector_year"].get((cid, year, sector_key))
            if value is None:
                value = factors["by_province_sector_year"].get((province, year, sector_key))
            if value is None:
                value = factors["by_demand_sector"].get((demand_node_id, sector_key))
            if value is None:
                value = factors["by_unit_sector"].get((unit_id, sector_key))
            if value is None:
                value = factors["by_city_sector"].get((cid, sector_key))
            if value is None:
                value = factors["by_province_sector"].get((province, sector_key))
        if value is None:
            value = factors["by_city_year"].get((cid, year))
        if value is None:
            value = factors["by_province_year"].get((province, year))
        if value is None:
            value = factors["by_city"].get(cid)
        if value is None:
            value = factors["by_province"].get(province)
        if value is None:
            return 1.0
        return max(0.0, float(value))

    def _load_unit_demand_split_factors(self):
        """Load optional sector-specific city-to-unit demand split factors."""
        df = self._read_csv("demand", "unit_demand_split_factors.csv")
        if df is None:
            print("    unit demand split factors: area fallback")
            return None

        required = {"city_code", "unit_id"}
        if not required.issubset(set(df.columns)):
            print("    [WARN] unit_demand_split_factors.csv missing city_code/unit_id")
            return None

        factor_columns = [
            "domestic_weight",
            "industrial_weight",
            "agricultural_weight",
            "ecological_weight",
            "unconventional_weight",
            "groundwater_weight",
        ]
        by_city_unit = {}
        by_unit = {}
        count = 0
        for _, row in df.iterrows():
            city_code = self._clean_str(row.get("city_code", ""))
            unit_id = self._clean_str(row.get("unit_id", ""))
            if not city_code or not unit_id:
                continue
            rec = {}
            for col in factor_columns:
                if col in row.index and pd.notna(row[col]):
                    rec[col] = max(0.0, self._safe_float(row.get(col, 0.0), 0.0))
            for col in [
                "domestic_basis",
                "industrial_basis",
                "agricultural_basis",
                "ecological_basis",
                "unconventional_basis",
                "groundwater_basis",
            ]:
                if col in row.index:
                    rec[col] = self._clean_str(row.get(col, ""))
            if not rec:
                continue
            by_city_unit[(city_code, unit_id)] = rec
            by_unit[unit_id] = rec
            count += 1

        print(f"    unit demand split factors loaded: {count}")
        return {"by_city_unit": by_city_unit, "by_unit": by_unit}

    def _demand_split_record(self, factors, dm: DemandNode):
        if not factors:
            return None
        city_code = self._clean_str(getattr(dm, "city_code", ""))
        unit_id = self._clean_str(getattr(dm, "unit_id", ""))
        rec = factors.get("by_city_unit", {}).get((city_code, unit_id))
        if rec is None and unit_id:
            rec = factors.get("by_unit", {}).get(unit_id)
        return rec

    def _demand_sector_weight(self, factors, dm: DemandNode, sector_key: str) -> float:
        rec = self._demand_split_record(factors, dm)
        weight_col = f"{sector_key}_weight"
        if rec and weight_col in rec:
            weight = max(0.0, float(rec[weight_col]))
            basis = rec.get(f"{sector_key}_basis", "")
            if basis:
                dm.demand_split_basis[sector_key] = basis
            return weight
        return self._demand_node_weight(dm)

    def _demand_node_weight(self, dm: DemandNode) -> float:
        weight = getattr(dm, "demand_area_weight", 1.0)
        try:
            weight = float(weight)
        except Exception:
            weight = 1.0
        return max(0.0, weight)

    def _load_gw_params(self):
        """Load city groundwater parameters and split them to demand nodes."""
        df = self._read_csv("gw_params.csv")
        if df is None:
            return

        df['city_code'] = df['city_code'].astype(str)
        split_factors = getattr(self.network, "_unit_demand_split_factors", None)
        demand_nodes = [
            dm for dm in self.network.get_nodes(NodeType.DEMAND)
            if isinstance(dm, DemandNode)
        ]
        count = 0
        for dm in demand_nodes:
            cid = dm.city_code
            row = df[df['city_code'] == cid]
            if len(row) == 0:
                continue
            row = row.iloc[0]
            gw_weight = self._demand_sector_weight(split_factors, dm, "groundwater")
            dm.demand_sector_weights["groundwater"] = gw_weight
            dm.gw_exploitable_annual = float(row.get('gw_exploitable_annual', 0)) * gw_weight
            dm.gw_cumulative_over = float(row.get('initial_over', 0)) * gw_weight
            dm.gw_over_decay = float(row.get('gw_over_decay', 0.98))
            gw_id = f"GW_{dm.id[3:] if dm.id.startswith('DM_') else dm.id}"
            gw_node = GroundwaterNode(
                id=gw_id,
                name=f"{dm.name or dm.city_name}地下水",
                basin=dm.basin,
                province=dm.province,
                city_code=cid,
                city_name=dm.city_name,
                exploitable_annual=dm.gw_exploitable_annual,
                initial_over=dm.gw_cumulative_over,
                over_decay=dm.gw_over_decay,
                demand_node_id=dm.id,
            )
            self.network.add_node(gw_node)
            self.network.add_link(SupplyLink(
                id=f"SPG_{gw_id}_to_{dm.id}",
                name=f"{dm.city_name}地下水供水",
                link_type=LinkType.SUPPLY_GROUNDWATER,
                from_node=gw_id,
                to_node=dm.id,
                loss_rate=0.0,
                source_priority=2,
            ))
            count += 1

        print(f"    demand-node groundwater params loaded: {count}")

    def _load_unconventional_supply(self):
        """Load optional city-year unconventional supply and split to demand nodes."""
        self.network._unconventional_supply_by_city_year = {}
        self.network._unconventional_supply_by_demand_year = {}
        df = self._read_csv("unconventional_supply.csv")
        if df is None:
            print("    unconventional supply targets loaded: 0 (optional)")
            return

        required = {"city_code", "year"}
        if not required.issubset(set(df.columns)):
            print("    [WARN] unconventional_supply.csv missing city_code/year")
            return

        value_col = None
        for col in ["annual_supply", "annual_supply_wan", "unconventional_supply", "other_supply"]:
            if col in df.columns:
                value_col = col
                break
        if value_col is None:
            print("    [WARN] unconventional_supply.csv missing annual supply column")
            return

        split_factors = getattr(self.network, "_unit_demand_split_factors", None)
        demand_nodes_by_city = defaultdict(list)
        for dm in self.network.get_nodes(NodeType.DEMAND):
            if isinstance(dm, DemandNode):
                demand_nodes_by_city[self._clean_str(dm.city_code)].append(dm)

        count = 0
        node_count = 0
        for _, row in df.iterrows():
            city_code = self._clean_str(row.get("city_code", ""))
            year = self._safe_int(row.get("year", 0), 0)
            annual_supply = self._safe_float(row.get(value_col, 0.0), 0.0)
            if not city_code or year <= 0 or annual_supply <= 0:
                continue
            self.network._unconventional_supply_by_city_year[(city_code, year)] = annual_supply
            for dm in demand_nodes_by_city.get(city_code, []):
                weight = self._demand_sector_weight(split_factors, dm, "unconventional")
                dm.demand_sector_weights["unconventional"] = weight
                self.network._unconventional_supply_by_demand_year[(dm.id, year)] = (
                    annual_supply * weight
                )
                node_count += 1
            count += 1

        print(
            "    unconventional supply targets loaded: "
            f"city_year={count}, demand_year={node_count}"
        )

    def _load_source_structure_factors(self):
        df = self._read_csv("source_structure_factors.csv")
        if df is None or self.network is None:
            return

        by_city = {}
        by_city_year = {}
        by_demand = {}
        by_demand_year = {}
        by_unit = {}
        by_unit_year = {}
        by_province = {}
        by_province_year = {}
        count_city = 0
        count_city_year = 0
        count_demand = 0
        count_demand_year = 0
        count_unit = 0
        count_unit_year = 0
        count_province = 0
        count_province_year = 0

        for _, row in df.iterrows():
            rec = {}
            for key in [
                "surface_factor",
                "groundwater_factor",
                "unconventional_factor",
                "local_sw_factor",
                "reservoir_factor",
                "diversion_factor",
                "transfer_factor",
                "lake_factor",
            ]:
                if key in row.index and pd.notna(row[key]):
                    rec[key] = float(row[key])
            if not rec:
                continue

            city_code = self._clean_str(row.get("city_code", ""))
            demand_node_id = self._clean_str(row.get("demand_node_id", ""))
            unit_id = self._clean_str(row.get("unit_id", ""))
            province = self._clean_str(row.get("province", ""))
            year_raw = row.get("year", "")
            year = None
            if pd.notna(year_raw) and self._clean_str(year_raw, ""):
                try:
                    year = int(float(year_raw))
                except Exception:
                    year = None
            if demand_node_id:
                if year is None:
                    by_demand[demand_node_id] = rec
                    count_demand += 1
                else:
                    by_demand_year[(demand_node_id, year)] = rec
                    count_demand_year += 1
            elif unit_id:
                if year is None:
                    by_unit[unit_id] = rec
                    count_unit += 1
                else:
                    by_unit_year[(unit_id, year)] = rec
                    count_unit_year += 1
            elif city_code:
                if year is None:
                    by_city[city_code] = rec
                    count_city += 1
                else:
                    by_city_year[(city_code, year)] = rec
                    count_city_year += 1
            elif province:
                if year is None:
                    by_province[province] = rec
                    count_province += 1
                else:
                    by_province_year[(province, year)] = rec
                    count_province_year += 1

        self.network._source_structure_factors = {
            "by_demand": by_demand,
            "by_demand_year": by_demand_year,
            "by_unit": by_unit,
            "by_unit_year": by_unit_year,
            "by_city": by_city,
            "by_city_year": by_city_year,
            "by_province": by_province,
            "by_province_year": by_province_year,
        }
        print(
            "    source structure factors loaded: "
            f"demand={count_demand}, demand_year={count_demand_year}, "
            f"unit={count_unit}, unit_year={count_unit_year}, "
            f"city={count_city}, city_year={count_city_year}, "
            f"province={count_province}, province_year={count_province_year}"
        )

    def _load_water_rights(self):
        """Load optional water-right controls such as the Yellow River 1987 plan."""
        if self.network is None:
            return
        self.network._water_rights_scheme = None
        self.network._water_rights_policy_table = pd.DataFrame()

        if hasattr(self.config, "is_process_enabled"):
            enabled = self.config.is_process_enabled("enable_water_rights")
        else:
            enabled = bool(getattr(self.config, "enable_water_rights", 0))
        if not enabled:
            print("    skip water-right controls: enable_water_rights=0")
            return

        policy_file = self._clean_str(
            getattr(
                self.config,
                "water_rights_policy_file",
                "policies/yellow_river_1987_allocation_plan.csv",
            )
        )
        if not policy_file:
            print("    [WARN] water-right policy file is not configured")
            return
        filepath = policy_file
        if not os.path.isabs(filepath):
            filepath = os.path.join(
                self.data_dir,
                *policy_file.replace("\\", "/").split("/"),
            )

        df = self._read_file(filepath)
        if df is None or df.empty:
            print(f"    [WARN] missing water-right policy file: {filepath}")
            return

        scheme_id = self._clean_str(
            getattr(self.config, "water_rights_scheme_id", ""),
            "water_rights",
        )
        if "scheme_id" in df.columns:
            values = [self._clean_str(v) for v in df["scheme_id"].dropna().tolist()]
            if values:
                scheme_id = values[0]
        scheme_name = scheme_id
        if "scheme_name" in df.columns:
            values = [self._clean_str(v) for v in df["scheme_name"].dropna().tolist()]
            if values:
                scheme_name = values[0]
        river = self._clean_str(
            getattr(self.config, "water_rights_target_basin", ""),
            "黄河",
        )
        if "river" in df.columns:
            values = [self._clean_str(v) for v in df["river"].dropna().tolist()]
            if values:
                river = values[0]

        scheme = WaterRightsScheme(id=scheme_id, name=scheme_name, river=river)

        month_cols = [f"m{i:02d}" for i in range(1, 13)]
        if set(month_cols).issubset(set(df.columns)):
            weights = np.array(
                [self._safe_float(df.iloc[0].get(col, 0.0), 0.0) for col in month_cols],
                dtype=float,
            )
            if np.isfinite(weights).all() and weights.sum() > 0:
                scheme.monthly_pattern = weights / weights.sum()

        quota_rows = 0
        for _, row in df.iterrows():
            province = self._clean_str(row.get("province", ""))
            quota_key = self._clean_str(row.get("quota_key", province), province)
            if not province or not quota_key:
                continue
            annual = self._safe_float(row.get("annual_quota_wan_m3", np.nan), np.nan)
            if not np.isfinite(annual) or annual <= 0:
                annual_yi = self._safe_float(row.get("annual_quota_yi_m3", np.nan), np.nan)
                annual = annual_yi * 10000.0 if np.isfinite(annual_yi) else 0.0
            if annual <= 0:
                continue
            scheme.province_quota_key[province] = quota_key
            if quota_key not in scheme.annual_quota:
                scheme.annual_quota[quota_key] = annual
            quota_rows += 1

        if not scheme.annual_quota:
            print(f"    [WARN] water-right policy has no valid quotas: {filepath}")
            return

        self.network._water_rights_scheme = scheme
        self.network._water_rights_policy_table = df.copy()
        print(
            "    water-right controls loaded: "
            f"scheme={scheme.id}, quota_keys={len(scheme.annual_quota)}, rows={quota_rows}"
        )

    def _read_csv(self, *path_parts) -> Optional[pd.DataFrame]:
        """Read a CSV input under ``data_dir``."""
        filepath = os.path.join(self.data_dir, *path_parts)
        return self._read_file(filepath)

    def load_all(self) -> WaterNetwork:
        """Load all model inputs and return a fully initialized network."""
        print("=" * 60)
        print(f"  {MODEL_NAME} data loading")
        print("=" * 60)

        print("\n[1/10] Loading computational units...")
        self._load_units()

        print("[2/10] Loading river topology...")
        self._load_topology()

        print("[3/10] Building network...")
        self.network = self.builder.build()

        print("\n[4/10] Loading climate series...")
        self._load_climate()

        print("[5/10] Loading hydrology parameters...")
        self._load_hydro_params_with_calibration()

        print("[5.5] Loading precomputed runoff series...")
        self._load_precomputed_runoff()

        print("[6/10] Loading physical data...")
        self._load_physical_data()

        print("[7/10] Loading demand data...")
        self._load_demand()

        print("[8/10] Loading return ratios and groundwater parameters...")
        self._load_return_ratios()
        self._load_gw_params()
        self._load_unconventional_supply()
        self._load_source_structure_factors()
        self._load_water_rights()

        print("[9/10] Loading infrastructure...")
        self._load_reservoirs()
        self._load_lakes()
        self._load_transfers()
        self._load_diversions()
        self._load_facility_links()

        print("[10/10] Loading ecology controls...")
        self._load_ecology()

        print("[10.5/10] Loading managed ocean process factors...")
        self._load_managed_ocean_process_factors()

        print("\n[OK] data loading complete")
        print(self.network.summary())
        return self.network


    # ------------------------------------------------------------------
    # 1. Computational units

    def _load_units(self):
        df = self._read_csv("units.csv")
        if df is None:
            raise FileNotFoundError("missing required input: data/units.csv")
        df = self._attach_unit_centroids(df)
        self.builder.load_units(df)

    def _attach_unit_centroids(self, df: pd.DataFrame) -> pd.DataFrame:
        """Attach unit centroids so spatial return-flow routing can use distance."""
        out = df.copy()
        has_xy = {"x", "y"}.issubset(out.columns)
        if has_xy:
            x = pd.to_numeric(out["x"], errors="coerce").fillna(0.0)
            y = pd.to_numeric(out["y"], errors="coerce").fillna(0.0)
            if (x.abs().sum() + y.abs().sum()) > 0:
                return out

        shp_path = os.path.join(self.data_dir, "shp", "unit.shp")
        if not os.path.exists(shp_path):
            return out

        try:
            import geopandas as gpd

            gdf = gpd.read_file(shp_path)
            if "UID" not in gdf.columns:
                return out
            if gdf.crs is not None and getattr(gdf.crs, "is_geographic", False):
                projected = gdf.to_crs(3857)
                centroids = gpd.GeoSeries(projected.geometry.centroid, crs=projected.crs).to_crs(gdf.crs)
            else:
                centroids = gdf.geometry.centroid
                if gdf.crs is not None:
                    centroids = gpd.GeoSeries(centroids, crs=gdf.crs).to_crs(4326)
            centroid_df = pd.DataFrame(
                {
                    "UID": gdf["UID"].astype(str),
                    "x": [float(pt.x) if pt is not None and not pt.is_empty else np.nan for pt in centroids],
                    "y": [float(pt.y) if pt is not None and not pt.is_empty else np.nan for pt in centroids],
                }
            ).dropna(subset=["x", "y"])
        except Exception as exc:
            print(f"    [WARN] unable to attach unit centroids: {exc}")
            return out

        if "x" in out.columns:
            out = out.drop(columns=["x"])
        if "y" in out.columns:
            out = out.drop(columns=["y"])
        return out.merge(centroid_df, on="UID", how="left")

    # ════════════════ 2. 拓扑 ════════════════

    def _load_topology(self):
        df = self._read_csv("topology.csv")
        if df is None:
            df = self._read_csv("channel_topology.csv")
        if df is not None and len(df) > 0:
            self.builder.load_channel_topology(df)
        else:
            print("    ⚠ 未提供拓扑文件")

    # ════════════════ 4. 气象 ════════════════

    def _load_climate(self):
        var_map = {
            'precip': 'precip',
            'temp_mean': 'temp_mean',
            'temp_max': 'temp_max',
            'temp_min': 'temp_min',
            'pet': 'pet',
        }
        for filename, attr_name in var_map.items():
            df = self._read_csv("climate", f"{filename}.csv")
            if df is None:
                if filename in ('precip', 'temp_mean', 'pet'):
                    print(f"    ⚠ 缺少 {filename}.csv")
                continue

            # 第一列是日期，跳过
            date_col = df.columns[0]
            loaded, missing = 0, 0

            for sb in self.network.get_nodes(NodeType.SUBBASIN):
                if not isinstance(sb, SubbasinNode):
                    continue
                uid = sb.unit_id
                if uid in df.columns:
                    values = self._time_aligned_values(df, uid)
                    # 插值NaN
                    nans = np.isnan(values)
                    if nans.any():
                        valid = ~nans
                        if valid.any():
                            idx = np.arange(len(values))
                            values[nans] = np.interp(idx[nans], idx[valid], values[valid])
                        else:
                            values[:] = 0.0
                    # 非负约束
                    if attr_name in ('precip', 'pet'):
                        values = np.maximum(values, 0)
                    setattr(sb, attr_name, values)
                    loaded += 1
                else:
                    missing += 1
            print(f"    {filename}: 加载 {loaded}, 缺失 {missing}")

    # ════════════════ 5. 水文参数 ════════════════

    def _load_hydro_params(self):
        df = self._read_csv("hydro_params.csv")
        if df is None:
            print("    使用默认参数")
            return
        count = 0
        for _, row in df.iterrows():
            uid = str(row.get('unit_id', '')).strip()
            sb_id = f"SB_{uid}"
            sb = self.network.nodes.get(sb_id)
            if not isinstance(sb, SubbasinNode):
                continue
            for attr in ['param_a', 'param_b', 'param_c', 'param_d',
                          'param_x1', 'param_x2', 'runoff_coeff',
                          'et_reduction', 'baseflow_index']:
                if attr in row.index and pd.notna(row[attr]):
                    setattr(sb, attr, float(row[attr]))
            if 'is_high_altitude' in row.index and pd.notna(row['is_high_altitude']):
                sb.is_high_altitude = bool(int(row['is_high_altitude']))
            if 'eco_priority' in row.index and pd.notna(row['eco_priority']):
                sb.eco_priority = int(row['eco_priority'])
            count += 1
        print(f"    加载 {count} 个单元的水文参数")


    # ════════════════ 6. 物理数据 ════════════════

    def _load_physical_data(self):
        try:
            from hydro.landuse import LandUseModule
            self.landuse_mod = LandUseModule()
        except ImportError:
            return

        for name, method in [
            ("soil_params.csv", self.landuse_mod.load_soil),
            ("terrain_params.csv", self.landuse_mod.load_terrain),
            ("landuse.csv", self.landuse_mod.load),
        ]:
            df = self._read_csv("physical", name)
            if df is not None:
                method(df)

        # 用物理数据估算缺失参数
        estimated = 0
        for sb in self.network.get_nodes(NodeType.SUBBASIN):
            if not isinstance(sb, SubbasinNode):
                continue
            # 只对默认参数的单元估算
            if sb.param_b == 200.0 and sb.unit_id in self.landuse_mod._soil:
                params = self.landuse_mod.estimate_initial_params(sb.unit_id)
                for k, v in params.items():
                    if hasattr(sb, k):
                        setattr(sb, k, v)
                estimated += 1
        if estimated > 0:
            print(f"    {estimated} 个单元用物理数据估算参数")

    # ════════════════ 7. 需水 ════════════════

    # ------------------------------------------------------------------
    # 8. Return flows and groundwater

    def _load_return_ratios(self):
        """Load sectoral return-flow ratios onto demand-unit nodes."""
        df = self._read_csv("return_ratios.csv")
        if df is None:
            print("    using default return ratios")
            return

        df["city_code"] = df["city_code"].astype(str)
        sector_columns = [
            (Sector.DOMESTIC.value, "domestic_return"),
            (Sector.INDUSTRIAL.value, "industrial_return"),
            (Sector.AGRICULTURAL.value, "agricultural_return"),
            (Sector.ECOLOGICAL.value, "ecological_return"),
        ]

        count = 0
        for dm in self.network.get_nodes(NodeType.DEMAND):
            if not isinstance(dm, DemandNode):
                continue
            row = df[df["city_code"] == dm.city_code]
            if len(row) == 0:
                continue
            row = row.iloc[0]
            for sector, column in sector_columns:
                if column in row.index and pd.notna(row[column]):
                    dm.return_ratios[sector] = float(row[column])
            count += 1

        print(f"    city return ratios loaded: {count}")

    def _load_managed_ocean_process_factors(self):
        """Apply traceable full-mode process factors for ocean-outflow calibration.

        The factors adjust physical/process parameters before simulation:
        sectoral return-flow caps on demand nodes and river-link loss rates.
        They are deliberately skipped in natural runs so station and natural
        water-resource calibration are not altered by managed allocation tuning.
        """
        if self.network is None:
            return
        run_preset = self._clean_str(getattr(self.config, "run_preset", "full"), "full").lower()
        if run_preset == "natural":
            print("    skip ocean process factors: run_preset=natural")
            return

        df = self._read_csv("process", "ocean_process_factors.csv")
        if df is None or df.empty:
            print("    no ocean process factor file")
            return

        rows = []
        for _, row in df.iterrows():
            enabled = self._safe_float(row.get("enabled", 1), 1.0)
            prefix = self._clean_str(row.get("scope_prefix", ""))
            if not prefix or enabled <= 0:
                continue
            rows.append(
                {
                    "scope_prefix": prefix,
                    "scope_name": self._clean_str(row.get("scope_name", "")),
                    "domestic_return_cap": self._safe_optional_float(row.get("domestic_return_cap", np.nan)),
                    "industrial_return_cap": self._safe_optional_float(row.get("industrial_return_cap", np.nan)),
                    "agricultural_return_cap": self._safe_optional_float(row.get("agricultural_return_cap", np.nan)),
                    "ecological_return_cap": self._safe_optional_float(row.get("ecological_return_cap", np.nan)),
                    "local_surface_access_ratio": self._safe_optional_float(row.get("local_surface_access_ratio", np.nan)),
                    "channel_loss_add": self._safe_optional_float(row.get("channel_loss_add", np.nan)),
                    "channel_loss_multiplier": self._safe_optional_float(row.get("channel_loss_multiplier", np.nan)),
                    "channel_loss_cap": self._safe_optional_float(row.get("channel_loss_cap", np.nan)),
                    "channel_node_loss_add": self._safe_optional_float(row.get("channel_node_loss_add", np.nan)),
                    "channel_node_loss_multiplier": self._safe_optional_float(row.get("channel_node_loss_multiplier", np.nan)),
                    "channel_node_loss_cap": self._safe_optional_float(row.get("channel_node_loss_cap", np.nan)),
                    "process_basis": self._clean_str(row.get("process_basis", "")),
                }
            )
        if not rows:
            print("    no enabled ocean process factors")
            return

        rows = sorted(rows, key=lambda item: len(item["scope_prefix"]), reverse=True)
        sector_caps = [
            (Sector.DOMESTIC.value, "domestic_return_cap"),
            (Sector.INDUSTRIAL.value, "industrial_return_cap"),
            (Sector.AGRICULTURAL.value, "agricultural_return_cap"),
            (Sector.ECOLOGICAL.value, "ecological_return_cap"),
        ]

        demand_adjustments = []
        for dm in self.network.get_nodes(NodeType.DEMAND):
            if not isinstance(dm, DemandNode):
                continue
            factor = self._best_ocean_process_factor(rows, self._demand_scope_keys(dm))
            if factor is None:
                continue
            changed = {}
            access_ratio = factor.get("local_surface_access_ratio")
            if access_ratio is not None and np.isfinite(access_ratio):
                old_access = getattr(dm, "local_surface_access_ratio", None)
                new_access = max(0.0, min(1.0, float(access_ratio)))
                setattr(dm, "local_surface_access_ratio", new_access)
                if old_access is None or abs(float(old_access) - new_access) > 1e-12:
                    changed["local_surface_access_ratio"] = {
                        "old": old_access,
                        "new": new_access,
                    }
            for sector, column in sector_caps:
                cap = factor.get(column)
                if cap is None or not np.isfinite(cap):
                    continue
                cap = max(0.0, min(1.0, float(cap)))
                old = float(dm.return_ratios.get(sector, 0.0))
                new = min(old, cap)
                if abs(new - old) > 1e-12:
                    dm.return_ratios[sector] = new
                    changed[sector] = {"old": old, "new": new}
            if changed:
                demand_adjustments.append(
                    {
                        "city_code": getattr(dm, "city_code", ""),
                        "city_name": getattr(dm, "city_name", ""),
                        "scope_prefix": factor["scope_prefix"],
                        "scope_name": factor["scope_name"],
                        "changed": changed,
                    }
                )

        link_adjustments = []
        flow_link_types = {LinkType.RIVER, LinkType.TRIBUTARY}
        for link in list(self.network.links.values()):
            if getattr(link, "link_type", None) not in flow_link_types:
                continue
            keys = [
                self._node_scope_key(getattr(link, "from_node", "")),
                self._node_scope_key(getattr(link, "to_node", "")),
            ]
            factor = self._best_ocean_process_factor(rows, keys)
            if factor is None:
                continue
            loss_add = factor.get("channel_loss_add")
            loss_multiplier = factor.get("channel_loss_multiplier")
            loss_cap = factor.get("channel_loss_cap")
            if (
                (loss_add is None or not np.isfinite(loss_add))
                and (loss_multiplier is None or not np.isfinite(loss_multiplier))
                and (loss_cap is None or not np.isfinite(loss_cap))
            ):
                continue
            old = max(0.0, float(getattr(link, "loss_rate", 0.0) or 0.0))
            new = old
            if loss_multiplier is not None and np.isfinite(loss_multiplier):
                new *= max(0.0, float(loss_multiplier))
            if loss_add is not None and np.isfinite(loss_add):
                new += max(0.0, float(loss_add))
            if loss_cap is not None and np.isfinite(loss_cap):
                new = min(new, max(0.0, float(loss_cap)))
            new = max(0.0, min(0.90, new))
            if abs(new - old) > 1e-12:
                link.loss_rate = new
                link_adjustments.append(
                    {
                        "link_id": getattr(link, "id", ""),
                        "from_node": getattr(link, "from_node", ""),
                        "to_node": getattr(link, "to_node", ""),
                        "scope_prefix": factor["scope_prefix"],
                        "scope_name": factor["scope_name"],
                        "old_loss_rate": old,
                        "new_loss_rate": new,
                    }
                )

        channel_node_adjustments = []
        for node in self.network.get_nodes(NodeType.RIVER_CHANNEL):
            if not isinstance(node, RiverChannelNode):
                continue
            factor = self._best_ocean_process_factor(rows, [self._node_scope_key(node.id)])
            if factor is None:
                continue
            loss_add = factor.get("channel_node_loss_add")
            loss_multiplier = factor.get("channel_node_loss_multiplier")
            loss_cap = factor.get("channel_node_loss_cap")
            if (
                (loss_add is None or not np.isfinite(loss_add))
                and (loss_multiplier is None or not np.isfinite(loss_multiplier))
                and (loss_cap is None or not np.isfinite(loss_cap))
            ):
                continue
            old = max(0.0, float(getattr(node, "loss_rate", 0.0) or 0.0))
            new = old
            if loss_multiplier is not None and np.isfinite(loss_multiplier):
                new *= max(0.0, float(loss_multiplier))
            if loss_add is not None and np.isfinite(loss_add):
                new += max(0.0, float(loss_add))
            if loss_cap is not None and np.isfinite(loss_cap):
                new = min(new, max(0.0, float(loss_cap)))
            new = max(0.0, min(0.90, new))
            if abs(new - old) > 1e-12:
                node.loss_rate = new
                channel_node_adjustments.append(
                    {
                        "node_id": getattr(node, "id", ""),
                        "unit_id": getattr(node, "unit_id", ""),
                        "scope_prefix": factor["scope_prefix"],
                        "scope_name": factor["scope_name"],
                        "old_loss_rate": old,
                        "new_loss_rate": new,
                    }
                )

        self.network._managed_ocean_process_factors = rows
        self.network._managed_ocean_process_adjustments = {
            "demand_return_ratio_caps": demand_adjustments,
            "river_loss_rates": link_adjustments,
            "channel_node_loss_rates": channel_node_adjustments,
        }
        print(
            "    ocean process factors applied: "
            f"demand_nodes={len(demand_adjustments)}, river_links={len(link_adjustments)}, "
            f"channel_nodes={len(channel_node_adjustments)}"
        )

    def _safe_optional_float(self, value):
        if pd.isna(value):
            return np.nan
        text = self._clean_str(value, "")
        if not text:
            return np.nan
        try:
            return float(text)
        except Exception:
            return np.nan

    def _best_ocean_process_factor(self, rows, keys):
        clean_keys = [str(key) for key in keys if str(key)]
        for row in rows:
            prefix = row["scope_prefix"]
            if any(key.startswith(prefix) for key in clean_keys):
                return row
        return None

    def _demand_scope_keys(self, dm: DemandNode) -> list[str]:
        keys = []
        for unit_id in getattr(dm, "coupled_unit_ids", []) or []:
            keys.append(str(unit_id))
        unit_id = getattr(dm, "unit_id", "")
        if unit_id:
            keys.append(str(unit_id))
        return keys

    def _node_scope_key(self, node_id: str) -> str:
        node = self.network.nodes.get(node_id) if self.network is not None else None
        for attr in ("unit_id", "channel_node_id"):
            value = self._clean_str(getattr(node, attr, "") if node is not None else "")
            if value:
                return value
        text = self._clean_str(node_id)
        for prefix in ("CH_", "SB_"):
            if text.startswith(prefix):
                return text[len(prefix):]
        return text

    # ------------------------------------------------------------------
    # 9. Infrastructure

    def _load_reservoirs(self):
        df = self._read_csv("infra", "reservoirs.csv")
        if df is None:
            return
        count = 0
        for _, row in df.iterrows():
            res_id = self._clean_str(row.get('id', ''))
            if not res_id:
                continue
            if not self._infra_row_enabled(row, default_status="built"):
                continue

            flood_str = self._clean_str(row.get('flood_months', '7,8,9')) or '7,8,9'
            try:
                flood_months = [int(x.strip()) for x in flood_str.split(',') if x.strip()]
            except ValueError:
                flood_months = [7, 8, 9]

            basin_name = self._clean_str(row.get('basin', ''))
            evap = self._default_monthly_evap(basin_name).copy()
            for m in range(12):
                col = f'evap_{m+1:02d}'
                if col in row.index and pd.notna(row[col]):
                    evap[m] = self._safe_float(row[col], 50.0)

            normal_storage = self._safe_float(row.get('normal_storage', 0))
            dead_storage = self._safe_float(row.get('dead_storage', 0))
            usable = normal_storage - dead_storage
            init_ratio = self._safe_float(row.get('initial_ratio', 0.5), 0.5)
            init_storage = dead_storage + usable * init_ratio

            res = ReservoirNode(
                id=res_id, name=self._clean_str(row.get('name', '')),
                status=self._clean_str(row.get('status', 'built'), 'built').lower(),
                online_year=self._safe_int(row.get('online_year', 1900), 1900),
                retire_year=self._safe_int(row.get('retire_year', 0), 0) or None,
                scenario_group=self._clean_str(row.get('scenario_group', 'all'), 'all').lower(),
                basin=basin_name,
                province=self._clean_str(row.get('province', '')),
                x=self._safe_float(row.get('x', 0)), y=self._safe_float(row.get('y', 0)),
                total_capacity=self._safe_float(row.get('total_capacity', 0)),
                normal_storage=normal_storage,
                flood_limit_storage=self._safe_float(
                    row.get('flood_limit_storage', normal_storage), normal_storage),
                dead_storage=dead_storage,
                initial_storage=init_storage,
                current_storage=init_storage,
                monthly_evap=evap,
                seepage_rate=self._safe_float(row.get('seepage_rate', 0)),
                area_coeff=self._safe_float(row.get('area_coeff', 0.001), 0.001),
                flood_season_months=flood_months,
                order_in_channel=self._safe_int(row.get('order', 1), 1),
            )
            channel_uid = self._resolve_anchor_uid(
                self._clean_str(row.get('channel_uid', '')),
                asset_id=res_id,
                asset_name=self._clean_str(row.get('name', '')),
            )
            self.builder.add_reservoir(res, channel_uid)
            count += 1
        print(f"    加载 {count} 座水库")

    def _load_lakes(self):
        df = self._read_csv("infra", "lakes.csv")
        if df is None:
            return
        count = 0
        for _, row in df.iterrows():
            lake_id = str(row.get('id', '')).strip()
            if not lake_id:
                continue
            lake = LakeNode(
                id=lake_id, name=str(row.get('name', '')),
                basin=str(row.get('basin', '')),
                x=float(row.get('x', 0)), y=float(row.get('y', 0)),
                max_storage=float(row.get('max_storage', 0)),
                normal_storage=float(row.get('normal_storage', 0)),
                dead_storage=float(row.get('dead_storage', 0)),
                eco_min_storage=float(row.get('eco_min_storage', 0)),
                outflow_capacity=float(row.get('outflow_capacity', 0)),
                area_coeff=float(row.get('area_coeff', 0.001)),
            )
            usable = lake.normal_storage - lake.dead_storage
            ratio = float(row.get('initial_ratio', 0.5))
            lake.current_storage = lake.dead_storage + usable * ratio

            channel_uid = self._resolve_anchor_uid(
                str(row.get('channel_uid', '')).strip(),
                asset_id=lake_id,
                asset_name=str(row.get('name', '')).strip(),
            )
            self.builder.add_lake(lake, channel_uid)
            count += 1
        print(f"    加载 {count} 座湖泊")

    def _load_transfers(self):
        df = self._read_csv("infra", "transfers.csv")
        if df is None:
            return
        from water.transfer import TransferProject
        from config import SourceType

        count = 0
        for _, row in df.iterrows():
            proj_id = self._clean_str(row.get('id', ''))
            if not proj_id:
                continue
            if not self._infra_row_enabled(row, default_status="built"):
                continue

            source_node_id = self._clean_str(row.get('source_node_id', ''))
            transfer_source_alias = {
                'RES_danjiangkou': 'CH_F08030142060099',
            }
            source_node_id = transfer_source_alias.get(
                source_node_id, source_node_id)

            # 解析受水节点JSON
            recv_str = self._clean_str(row.get('receiving_nodes', '{}'), '{}')
            try:
                receiving = json.loads(recv_str)
            except json.JSONDecodeError:
                receiving = {}

            expanded_receiving = {}
            for target_ref, ratio in receiving.items():
                expanded = self.builder._expand_transfer_targets(target_ref, ratio)
                if expanded:
                    for target_id, target_ratio in expanded.items():
                        expanded_receiving[target_id] = (
                            expanded_receiving.get(target_id, 0.0) + target_ratio)
                else:
                    expanded_receiving[target_ref] = (
                        expanded_receiving.get(target_ref, 0.0) + float(ratio))

            annual_plan = self._safe_float(row.get('annual_plan', 0), 0.0)

            # Parse an optional 12-month transfer plan.
            plan_str = str(row.get('monthly_plan', ''))
            monthly_specified = False
            try:
                monthly = np.array([float(x.strip()) for x in plan_str.split(',') if x.strip()])
                monthly = np.where(np.isfinite(monthly), monthly, 0.0)
                if len(monthly) == 12:
                    monthly_specified = float(monthly.sum()) > 0
                elif 0 < len(monthly) < 12:
                    monthly = np.pad(monthly, (0, 12 - len(monthly)), constant_values=0)
                    monthly = monthly[:12]
                    monthly_specified = float(monthly.sum()) > 0
                else:
                    monthly = np.zeros(12)
            except (ValueError, AttributeError):
                monthly = np.zeros(12)

            intake_capacity = self._safe_float(row.get('intake_capacity', 0), 0.0)
            if annual_plan > 0 and intake_capacity <= 0:
                if monthly_specified and float(np.nanmax(monthly)) > 0:
                    intake_capacity = float(np.nanmax(monthly))
                else:
                    # When only the annual design volume is known, do not infer
                    # an artificial monthly mean capacity. Auto monthly plans
                    # are already bounded by their month-specific plan, source
                    # availability, losses, and water-right controls. Using
                    # annual_plan / 12 here clips seasonal irrigation and water
                    # network deliveries that have no explicit design-flow data.
                    intake_capacity = annual_plan

            plan_mode_raw = self._clean_str(row.get('dynamic_plan', '1'), '1').lower()
            if monthly_specified:
                plan_mode = 'specified'
            elif annual_plan <= 0:
                plan_mode = 'specified'
            elif plan_mode_raw in ('', '1', '1.0', 'true', 'yes', 'auto'):
                plan_mode = 'auto'
            elif plan_mode_raw in ('0', '0.0', 'false', 'no'):
                plan_mode = 'uniform'
            elif plan_mode_raw in ('uniform', 'demand_weighted', 'flow_weighted', 'hybrid'):
                plan_mode = plan_mode_raw
            else:
                plan_mode = 'auto'
            # 水源类型
            st_str = str(row.get('source_type', 'RIVER')).upper()
            st_map = {
                'RESERVOIR': SourceType.RESERVOIR,
                'RIVER': SourceType.RIVER,
                'LAKE': SourceType.LAKE,
                'TRANSFER': SourceType.TRANSFER,
                'REGULATED': SourceType.TRANSFER,
                'REGULATED_TRANSFER': SourceType.TRANSFER,
            }
            source_type = st_map.get(st_str, SourceType.RIVER)

            proj = TransferProject(
                id=proj_id,
                name=self._clean_str(row.get('name', '')),
                status=self._clean_str(row.get('status', 'built'), 'built').lower(),
                scenario_group=self._clean_str(row.get('scenario_group', 'all'), 'all').lower(),
                source_node_id=source_node_id,
                source_type=source_type,
                intake_capacity=intake_capacity,
                receiving_nodes=expanded_receiving,
                annual_plan=annual_plan,
                monthly_plan=monthly,
                loss_rate=self._safe_float(row.get('loss_rate', 0.05), 0.05),
                priority=self._safe_int(row.get('priority', 1), 1),
                start_year=self._safe_int(row.get('online_year', row.get('start_year', 1900)), 1900),
                end_year=self._safe_int(row.get('retire_year', row.get('end_year', 0)), 0) or None,
                min_source_storage_ratio=self._safe_float(row.get('min_source_ratio', 0.3), 0.3),
                min_source_flow=self._safe_float(row.get('min_source_flow', 0), 0.0),
                dynamic_plan=plan_mode != 'specified',
                plan_mode=plan_mode,
            )
            # 注册到TransferModule（在simulator中完成）
            # 这里先存到network属性中
            if not hasattr(self.network, '_transfer_projects'):
                self.network._transfer_projects = []
            self.network._transfer_projects.append(proj)
            if source_node_id in self.network.nodes:
                for recv_id in expanded_receiving:
                    if recv_id in self.network.nodes:
                        self.network.add_link(SupplyLink(
                            id=f"SPT_{proj_id}_to_{recv_id}",
                            name=f"transfer_{proj_id}_to_{recv_id}",
                            link_type=LinkType.SUPPLY_TRANSFER,
                            from_node=source_node_id,
                            to_node=recv_id,
                            loss_rate=float(row.get('loss_rate', 0.05)),
                            source_priority=4,
                        ))
            count += 1
        print(f"    加载 {count} 个调水工程")

    def _load_facility_links(self):
        df = self._read_csv("infra", "facility_links.csv")
        if df is not None and len(df) > 0:
            self.builder.load_facility_links(df)
            print(f"    loaded facility links: {len(df)}")
        self.builder.finalize_facility_graphs()
        registry = getattr(self.network, '_facility_graph', None)
        if registry is not None and hasattr(registry, '_graphs'):
            graph_count = sum(1 for graph in registry._graphs.values() if getattr(graph, 'edges', None))
            print(f"    finalized facility subgraphs: {graph_count}")

    def _load_diversions(self):
        if hasattr(self.config, "is_process_enabled") and not self.config.is_process_enabled("enable_diversions"):
            print("    skip diversions: enable_diversions=0")
            return
        df = self._read_csv("infra", "diversions.csv")
        if df is None:
            return
        count = 0
        for _, row in df.iterrows():
            div_id = str(row.get('id', '')).strip()
            if not div_id:
                continue
            supply_str = str(row.get('supply_to', '{}'))
            try:
                supply_to = json.loads(supply_str)
            except json.JSONDecodeError:
                supply_to = {}

            expanded_supply = {}
            for demand_ref, ratio in supply_to.items():
                expanded_supply.update(
                    self.builder._expand_demand_targets(demand_ref, ratio))

            div = RiverDiversionNode(
                id=div_id, name=str(row.get('name', '')),
                x=float(row.get('x', 0)), y=float(row.get('y', 0)),
                max_capacity=float(row.get('max_capacity', 0)),
                priority=int(row.get('priority', 1)),
                diversion_type=str(row.get('diversion_type', '')),
                supply_to=expanded_supply,
                eco_reserve_ratio=float(row.get('eco_reserve_ratio', 0) or 0),
                eco_reserve_min_flow=float(row.get('eco_reserve_min_flow', 0) or 0),
                downstream_min_ratio=float(row.get('downstream_min_ratio', 0) or 0),
                downstream_min_flow=float(row.get('downstream_min_flow', 0) or 0),
            )
            channel_uid = self._resolve_anchor_uid(
                str(row.get('channel_uid', '')).strip(),
                asset_id=div_id,
                asset_name=str(row.get('name', '')).strip(),
            )
            self.builder.add_diversion(div, channel_uid)
            count += 1
        print(f"    加载 {count} 个引水口")

    # ════════════════ 10. 生态 ════════════════

    def _load_ecology(self):
        df = self._read_csv("ecology", "eco_control.csv")
        if df is None:
            return
        count = 0
        for _, row in df.iterrows():
            eco_id = str(row.get('id', '')).strip()
            if not eco_id:
                continue

            flow_str = str(row.get('min_eco_flow', ''))
            try:
                flows = np.array([float(x.strip()) for x in flow_str.split(',') if x.strip()])
                if len(flows) < 12:
                    flows = np.pad(flows, (0, 12 - len(flows)), constant_values=0)
                flows = flows[:12]
            except (ValueError, AttributeError):
                flows = np.zeros(12)

            eco_type = str(row.get('type', 'control')).lower()
            channel_uid = self._resolve_anchor_uid(
                str(row.get('channel_uid', '')).strip(),
                asset_id=eco_id,
                asset_name=str(row.get('name', '')).strip(),
                river_name=str(row.get('river_name', '')).strip(),
            )

            if eco_type == 'ocean':
                node = OceanNode(
                    id=eco_id, name=str(row.get('name', '')),
                    river_name=str(row.get('river_name', '')),
                    x=float(row.get('x', 0)), y=float(row.get('y', 0)),
                    min_eco_flow=flows,
                )
                # 入海水量率定需要把多个控制断面汇总到同一个公报分区。
                # 这些扩展字段不参与水量计算，只用于结果对比和审计。
                node.target_section = self._clean_str(row.get('target_section', ''))
                node.calibration_role = self._clean_str(row.get('calibration_role', ''))
                node.scope_note = self._clean_str(row.get('scope_note', ''))
                self.builder.add_ocean(node, channel_uid)
            else:
                node = EcoControlNode(
                    id=eco_id, name=str(row.get('name', '')),
                    river_name=str(row.get('river_name', '')),
                    x=float(row.get('x', 0)), y=float(row.get('y', 0)),
                    min_eco_flow=flows,
                )
                # 入海水量率定断面采用 control 类型，避免额外出海支路改变拓扑。
                node.target_section = self._clean_str(row.get('target_section', ''))
                node.calibration_role = self._clean_str(row.get('calibration_role', ''))
                node.scope_note = self._clean_str(row.get('scope_note', ''))
                self.builder.add_eco_control(node, channel_uid)
            count += 1
        print(f"    加载 {count} 个生态断面")
