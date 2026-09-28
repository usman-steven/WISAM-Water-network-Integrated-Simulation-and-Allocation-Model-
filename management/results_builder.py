"""Build model result tables.

The simulator stores detailed state on nodes and process modules. This builder
turns that state into stable DataFrame contracts used by scripts, exports,
calibration tools and future scenario analysis.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import LinkType, NodeType, Sector
from core.nodes import (
    DemandNode,
    EcoControlNode,
    GroundwaterNode,
    OceanNode,
    ReservoirNode,
    RiverChannelNode,
    SubbasinNode,
)

# Public result column names. Keep these stable for existing tools and outputs.
CITY = "城市"
BASIN = "流域"
PROVINCE = "省份"
SECTOR = "行业"
UNIT_ID = "需求单元ID"
WATER_ZONE_ID = "三级区ID"
WATER_ZONE = "三级区"
BASIN_L2 = "二级流域"
DEMAND = "年均需水万m³"
SUPPLY = "年均供水万m³"
SHORTAGE = "年均缺水万m³"
MAX_SHORT_RATE = "最大月缺水率"
GW_OVER = "累计超采万m³"
RETURN_FLOW = "年均退水万m³"
CONSUMPTION = "年均耗水万m³"
YEAR = "year"
ANNUAL_DEMAND = "需水万m³"
ANNUAL_SUPPLY = "供水万m³"
ANNUAL_SHORTAGE = "缺水万m³"
ANNUAL_RETURN_FLOW = "退水万m³"
ANNUAL_CONSUMPTION = "耗水万m³"

RES_NAME = "水库"
ANCHOR = "挂载"
TOTAL_CAPACITY = "总库容万m³"
FINAL_STORAGE = "末蓄量万m³"
MIN_STORAGE = "最低蓄量万m³"
MIN_STORAGE_RATIO = "最低蓄水率"
ANNUAL_INFLOW = "年均入库万m³"

ECO_UNIT = "单元"
ECO_TYPE = "类型"
ECO_PRIORITY = "优先级"
ANNUAL_RUNOFF = "年均径流万m³"
ANNUAL_TRANSIT_FLOW = "年均过境流量万m³"
ANNUAL_ECO_BASEFLOW = "年均生态基流万m³"
SECTION = "断面"
RIVER = "河流"
MISSING_MONTHS = "缺水月数"
MISSING_FREQ = "缺水频率"
MEAN_MISSING = "年均缺水万m³"

TRANSFER_NAME = "调水工程"
TRANSFER_TARGETS = "受水节点数"
PLAN_TAKE = "年均计划取水万m3"
ACTUAL_TAKE = "年均实际取水万m3"
ACTUAL_DELIVER = "年均实际到水万m3"
TRANSFER_GAP = "年均缺额万m3"
UTIL = "利用率"

SOURCE_TYPES = (
    "local_sw",
    "groundwater",
    "unconventional",
    "reservoir",
    "diversion",
    "transfer",
    "historical_closure",
)
EXTRA_SOURCE_TYPES = ("lake",)


def annualized_mean(series) -> float:
    """Return annualized mean from a monthly series."""
    if series is None or len(series) == 0:
        return 0.0
    return float(np.mean(series)) * 12.0


def shortage_rate(demand, shortage) -> float:
    """Return maximum monthly shortage rate for one demand series."""
    if demand is None or shortage is None or len(demand) == 0 or len(shortage) == 0:
        return 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        rates = np.where(demand > 0, shortage / demand, 0.0)
    return float(np.max(rates))


def annual_sum(series, mask) -> float:
    """Return annual total for a monthly series under a boolean year mask."""
    if series is None or len(series) == 0:
        return 0.0
    values = np.asarray(series, dtype=float)
    n = min(len(values), len(mask))
    if n <= 0:
        return 0.0
    return float(np.sum(values[:n][mask[:n]]))


def annual_shortage_rate(demand, shortage, mask) -> float:
    """Return maximum monthly shortage rate within one calendar year."""
    if demand is None or shortage is None or len(demand) == 0 or len(shortage) == 0:
        return 0.0
    demand_values = np.asarray(demand, dtype=float)
    shortage_values = np.asarray(shortage, dtype=float)
    n = min(len(demand_values), len(shortage_values), len(mask))
    if n <= 0:
        return 0.0
    demand_year = demand_values[:n][mask[:n]]
    shortage_year = shortage_values[:n][mask[:n]]
    with np.errstate(divide="ignore", invalid="ignore"):
        rates = np.where(demand_year > 0, shortage_year / demand_year, 0.0)
    return float(np.max(rates)) if len(rates) else 0.0


class ResultsBuilder:
    """Convert node state into stable result tables."""

    def __init__(self, config, network, transfer_mod):
        self.config = config
        self.network = network
        self.transfer_mod = transfer_mod
        self.mapping = getattr(network, "_demand_mapping", None)

    def _build_city_sector_summary(self) -> pd.DataFrame:
        records = []
        for dm in self.network.get_nodes(NodeType.DEMAND):
            if not isinstance(dm, DemandNode):
                continue
            for sector in [sector.value for sector in Sector]:
                demand = dm.demands.get(sector, np.zeros(1))
                allocation = dm.allocation.get(sector, np.zeros(1))
                shortage = dm.shortage.get(sector, np.zeros(1))
                return_flow = dm.return_flow_by_sector.get(sector, np.zeros(1))
                consumption = dm.consumption.get(sector, np.zeros(1))

                records.append(
                    {
                        "demand_node_id": dm.id,
                        UNIT_ID: getattr(dm, "unit_id", ""),
                        "city_code": dm.city_code,
                        CITY: dm.city_name,
                        BASIN: dm.basin,
                        BASIN_L2: getattr(dm, "basin_l2", ""),
                        WATER_ZONE_ID: getattr(dm, "water_zone_id", ""),
                        WATER_ZONE: getattr(dm, "water_zone_name", ""),
                        PROVINCE: dm.province,
                        SECTOR: sector,
                        "split_weight": round(
                            float(getattr(dm, "demand_sector_weights", {}).get(
                                sector, getattr(dm, "demand_area_weight", 1.0)
                            ) or 0.0),
                            6,
                        ),
                        "split_basis": getattr(dm, "demand_split_basis", {}).get(sector, ""),
                        DEMAND: round(annualized_mean(demand), 1),
                        SUPPLY: round(annualized_mean(allocation), 1),
                        SHORTAGE: round(annualized_mean(shortage), 1),
                        MAX_SHORT_RATE: round(shortage_rate(demand, shortage), 4),
                        GW_OVER: round(dm.gw_cumulative_over, 1),
                        RETURN_FLOW: round(annualized_mean(return_flow), 1),
                        CONSUMPTION: round(annualized_mean(consumption), 1),
                    }
                )
        return pd.DataFrame(records)

    def _time_year_masks(self) -> dict[int, np.ndarray]:
        time_index = getattr(getattr(self.config, "time", None), "time_index", None)
        if time_index is None or len(time_index) == 0:
            return {}
        years = np.array([int(ts.year) for ts in time_index], dtype=int)
        return {int(year): years == int(year) for year in sorted(set(years))}

    def _demand_node_metadata(self, dm: DemandNode) -> dict:
        return {
            "demand_node_id": dm.id,
            UNIT_ID: getattr(dm, "unit_id", ""),
            "city_code": dm.city_code,
            CITY: dm.city_name,
            BASIN: dm.basin,
            BASIN_L2: getattr(dm, "basin_l2", ""),
            WATER_ZONE_ID: getattr(dm, "water_zone_id", ""),
            WATER_ZONE: getattr(dm, "water_zone_name", ""),
            PROVINCE: dm.province,
        }

    def _build_city_sector_annual_summary(self) -> pd.DataFrame:
        year_masks = self._time_year_masks()
        if not year_masks:
            return pd.DataFrame()

        records = []
        for dm in self.network.get_nodes(NodeType.DEMAND):
            if not isinstance(dm, DemandNode):
                continue
            meta = self._demand_node_metadata(dm)
            for sector in [sector.value for sector in Sector]:
                demand = dm.demands.get(sector, np.zeros(1))
                allocation = dm.allocation.get(sector, np.zeros(1))
                shortage = dm.shortage.get(sector, np.zeros(1))
                return_flow = dm.return_flow_by_sector.get(sector, np.zeros(1))
                consumption = dm.consumption.get(sector, np.zeros(1))
                for year, mask in year_masks.items():
                    records.append(
                        {
                            YEAR: year,
                            **meta,
                            SECTOR: sector,
                            "split_weight": round(
                                float(getattr(dm, "demand_sector_weights", {}).get(
                                    sector, getattr(dm, "demand_area_weight", 1.0)
                                ) or 0.0),
                                6,
                            ),
                            "split_basis": getattr(dm, "demand_split_basis", {}).get(sector, ""),
                            ANNUAL_DEMAND: round(annual_sum(demand, mask), 1),
                            ANNUAL_SUPPLY: round(annual_sum(allocation, mask), 1),
                            ANNUAL_SHORTAGE: round(annual_sum(shortage, mask), 1),
                            MAX_SHORT_RATE: round(annual_shortage_rate(demand, shortage, mask), 4),
                            ANNUAL_RETURN_FLOW: round(annual_sum(return_flow, mask), 1),
                            ANNUAL_CONSUMPTION: round(annual_sum(consumption, mask), 1),
                        }
                    )
        return pd.DataFrame(records)

    def _disaggregate_to_units(self, city_sector_df: pd.DataFrame) -> pd.DataFrame:
        if city_sector_df is None or city_sector_df.empty or self.mapping is None:
            return pd.DataFrame()

        records = []
        for _, row in city_sector_df.iterrows():
            row_unit_id = str(row.get(UNIT_ID, "") or "")
            if row_unit_id:
                records.append(row.to_dict())
                continue
            for link in self.mapping.get_city_links(str(row["city_code"])):
                records.append(
                    {
                        UNIT_ID: link.unit_id,
                        "demand_node_id": row["demand_node_id"],
                        "city_code": row["city_code"],
                        CITY: row[CITY],
                        BASIN: link.basin or row[BASIN],
                        BASIN_L2: link.basin_l2,
                        WATER_ZONE_ID: link.zone_id,
                        WATER_ZONE: link.zone_name,
                        PROVINCE: link.province or row[PROVINCE],
                        SECTOR: row[SECTOR],
                        "split_weight": link.weight,
                        "split_basis": "area_fallback",
                        DEMAND: round(float(row[DEMAND]) * link.weight, 1),
                        SUPPLY: round(float(row[SUPPLY]) * link.weight, 1),
                        SHORTAGE: round(float(row[SHORTAGE]) * link.weight, 1),
                        MAX_SHORT_RATE: row[MAX_SHORT_RATE],
                        GW_OVER: round(float(row[GW_OVER]) * link.weight, 1),
                        RETURN_FLOW: round(float(row.get(RETURN_FLOW, 0.0)) * link.weight, 1),
                        CONSUMPTION: round(float(row.get(CONSUMPTION, 0.0)) * link.weight, 1),
                    }
                )
        return pd.DataFrame(records)

    def _build_source_mix(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        city_records = []
        for dm in self.network.get_nodes(NodeType.DEMAND):
            if not isinstance(dm, DemandNode):
                continue
            record = self._demand_node_metadata(dm)
            total_supply = 0.0
            for source_type in SOURCE_TYPES:
                annual = annualized_mean(dm.source_supply.get(source_type))
                record[source_type] = round(annual, 1)
                total_supply += annual
            extra_supply = {}
            for source_type in EXTRA_SOURCE_TYPES:
                annual = annualized_mean(dm.source_supply.get(source_type))
                extra_supply[source_type] = round(annual, 1)
                total_supply += annual
            record["total_supply"] = round(total_supply, 1)
            record.update(extra_supply)
            city_records.append(record)

        city_df = pd.DataFrame(city_records)
        if city_df.empty or self.mapping is None:
            return pd.DataFrame(), pd.DataFrame()

        unit_records = []
        for _, row in city_df.iterrows():
            row_unit_id = str(row.get(UNIT_ID, "") or "")
            if row_unit_id:
                unit_records.append(row.to_dict())
                continue
            for link in self.mapping.get_city_links(str(row["city_code"])):
                record = {
                    UNIT_ID: link.unit_id,
                    "demand_node_id": row["demand_node_id"],
                    "city_code": row["city_code"],
                    CITY: row[CITY],
                    BASIN: link.basin or row[BASIN],
                    BASIN_L2: link.basin_l2,
                    WATER_ZONE_ID: link.zone_id,
                    WATER_ZONE: link.zone_name,
                    PROVINCE: link.province or row[PROVINCE],
                }
                total_supply = 0.0
                for source_type in SOURCE_TYPES:
                    value = float(row[source_type]) * link.weight
                    record[source_type] = round(value, 1)
                    total_supply += value
                extra_supply = {}
                for source_type in EXTRA_SOURCE_TYPES:
                    value = float(row.get(source_type, 0.0)) * link.weight
                    extra_supply[source_type] = round(value, 1)
                    total_supply += value
                record["total_supply"] = round(total_supply, 1)
                record.update(extra_supply)
                unit_records.append(record)
        unit_df = pd.DataFrame(unit_records)
        source_columns = list(SOURCE_TYPES) + list(EXTRA_SOURCE_TYPES) + ["total_supply"]
        aggregate = {col: "sum" for col in source_columns if col in city_df.columns}
        city_groups = ["city_code", CITY, PROVINCE]
        city_total = city_df.groupby(city_groups, as_index=False).agg(aggregate)
        basin_labels = city_df.groupby(city_groups)[BASIN].apply(
            lambda values: ";".join(sorted({str(v) for v in values if str(v)}))
        ).reset_index()
        city_total = city_total.merge(basin_labels, on=city_groups, how="left")
        return unit_df, city_total

    def _build_source_mix_annual(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        year_masks = self._time_year_masks()
        if not year_masks:
            return pd.DataFrame(), pd.DataFrame()

        records = []
        source_columns = list(SOURCE_TYPES) + list(EXTRA_SOURCE_TYPES)
        for dm in self.network.get_nodes(NodeType.DEMAND):
            if not isinstance(dm, DemandNode):
                continue
            meta = self._demand_node_metadata(dm)
            for year, mask in year_masks.items():
                record = {YEAR: year, **meta}
                total_supply = 0.0
                for source_type in source_columns:
                    value = annual_sum(dm.source_supply.get(source_type), mask)
                    record[source_type] = round(value, 1)
                    total_supply += value
                record["total_supply"] = round(total_supply, 1)
                records.append(record)

        unit_df = pd.DataFrame(records)
        if unit_df.empty:
            return pd.DataFrame(), pd.DataFrame()

        aggregate = {
            col: "sum"
            for col in source_columns + ["total_supply"]
            if col in unit_df.columns
        }
        city_groups = [YEAR, "city_code", CITY, PROVINCE]
        city_total = unit_df.groupby(city_groups, as_index=False).agg(aggregate)
        basin_labels = unit_df.groupby(city_groups)[BASIN].apply(
            lambda values: ";".join(sorted({str(v) for v in values if str(v)}))
        ).reset_index()
        city_total = city_total.merge(basin_labels, on=city_groups, how="left")
        return unit_df, city_total

    def _build_demand_summaries(self, results: dict) -> None:
        city_sector = self._build_city_sector_summary()
        unit_sector = self._disaggregate_to_units(city_sector)
        if unit_sector.empty:
            unit_sector = city_sector.copy()

        results["summary_city"] = city_sector
        results["summary_unit"] = unit_sector
        results["summary"] = unit_sector

        if city_sector.empty:
            results["summary_city_total"] = pd.DataFrame()
            results["summary_basin"] = pd.DataFrame()
            results["summary_basin_total"] = pd.DataFrame()
            return

        aggregate = {
            DEMAND: "sum",
            SUPPLY: "sum",
            SHORTAGE: "sum",
            MAX_SHORT_RATE: "max",
            GW_OVER: "sum",
            RETURN_FLOW: "sum",
            CONSUMPTION: "sum",
        }
        city_groups = ["city_code", CITY, PROVINCE]
        city_total = city_sector.groupby(city_groups, as_index=False).agg(aggregate)
        basin_labels = city_sector.groupby(city_groups)[BASIN].apply(
            lambda values: ";".join(sorted({str(v) for v in values if str(v)}))
        ).reset_index()
        results["summary_city_total"] = city_total.merge(
            basin_labels, on=city_groups, how="left"
        )
        results["summary_basin"] = unit_sector.groupby(
            [BASIN, SECTOR],
            as_index=False,
        ).agg(aggregate)
        results["summary_basin_total"] = results["summary_basin"].groupby(
            [BASIN],
            as_index=False,
        ).agg(aggregate)

    def _build_demand_annual_summaries(self, results: dict) -> None:
        annual = self._build_city_sector_annual_summary()
        if annual.empty:
            results["summary_annual"] = pd.DataFrame()
            results["summary_city_annual"] = pd.DataFrame()
            results["summary_basin_annual"] = pd.DataFrame()
            return

        results["summary_annual"] = annual
        aggregate = {
            ANNUAL_DEMAND: "sum",
            ANNUAL_SUPPLY: "sum",
            ANNUAL_SHORTAGE: "sum",
            MAX_SHORT_RATE: "max",
            ANNUAL_RETURN_FLOW: "sum",
            ANNUAL_CONSUMPTION: "sum",
        }
        city_groups = [YEAR, "city_code", CITY, PROVINCE]
        city_total = annual.groupby(city_groups, as_index=False).agg(aggregate)
        basin_labels = annual.groupby(city_groups)[BASIN].apply(
            lambda values: ";".join(sorted({str(v) for v in values if str(v)}))
        ).reset_index()
        results["summary_city_annual"] = city_total.merge(
            basin_labels, on=city_groups, how="left"
        )
        results["summary_basin_annual"] = annual.groupby(
            [YEAR, BASIN],
            as_index=False,
        ).agg(aggregate)

    def _build_reservoir_summary(self) -> pd.DataFrame:
        records = []
        for res in self.network.get_nodes(NodeType.RESERVOIR):
            if not isinstance(res, ReservoirNode) or res.storage_ts is None:
                continue
            usable = max(1.0, res.normal_storage - res.dead_storage)
            min_storage = float(np.min(res.storage_ts))
            service_nodes = sum(
                1
                for link in self.network.downstream_links(res.id)
                if link.link_type == LinkType.SUPPLY_RESERVOIR
            )
            records.append(
                {
                    RES_NAME: res.name,
                    BASIN: res.basin,
                    ANCHOR: res.channel_node_id,
                    "service_nodes": service_nodes,
                    TOTAL_CAPACITY: res.total_capacity,
                    FINAL_STORAGE: round(res.current_storage, 1),
                    MIN_STORAGE: round(min_storage, 1),
                    MIN_STORAGE_RATIO: round((min_storage - res.dead_storage) / usable, 3),
                    ANNUAL_INFLOW: round(annualized_mean(res.inflow_ts), 1),
                }
            )
        return pd.DataFrame(records)

    def _build_transfer_summary(self) -> pd.DataFrame:
        records = []
        for project in self.transfer_mod.projects.values():
            if project.actual_ts is None:
                continue
            annual_plan = annualized_mean(project.planned_ts)
            annual_actual = annualized_mean(project.actual_ts)
            annual_delivered = annualized_mean(project.delivered_ts)
            annual_shortage = annualized_mean(project.shortage_ts)
            utilization = annual_actual / annual_plan if annual_plan > 0 else 0.0
            records.append(
                {
                    TRANSFER_NAME: project.name or project.id,
                    "source_node_id": project.source_node_id,
                    TRANSFER_TARGETS: len(project.receiving_nodes),
                    PLAN_TAKE: round(annual_plan, 1),
                    ACTUAL_TAKE: round(annual_actual, 1),
                    ACTUAL_DELIVER: round(annual_delivered, 1),
                    TRANSFER_GAP: round(annual_shortage, 1),
                    UTIL: round(utilization, 3),
                }
            )
        return pd.DataFrame(records)

    def _build_node_topology_summary(self) -> pd.DataFrame:
        records = []
        for node_type in NodeType:
            nodes = self.network.get_nodes(node_type)
            if nodes:
                records.append(
                    {
                        "node_type": node_type.value,
                        "count": len(nodes),
                    }
                )
        return pd.DataFrame(records)

    def _build_link_topology_summary(self) -> pd.DataFrame:
        records = []
        for link_type in LinkType:
            links = self.network.get_links(link_type)
            if links:
                records.append(
                    {
                        "link_type": link_type.value,
                        "count": len(links),
                        "active_count": sum(1 for link in links if getattr(link, "is_active", True)),
                    }
                )
        return pd.DataFrame(records)

    def _node_name(self, node_id: str) -> str:
        node = self.network.nodes.get(node_id)
        if node is None:
            return ""
        return getattr(node, "name", "") or getattr(node, "city_name", "") or node_id

    def _node_type(self, node_id: str) -> str:
        node = self.network.nodes.get(node_id)
        return getattr(getattr(node, "node_type", None), "value", "") if node is not None else ""

    def _build_supply_topology_detail(self) -> pd.DataFrame:
        supply_types = {
            LinkType.SUPPLY_LOCAL,
            LinkType.SUPPLY_RESERVOIR,
            LinkType.SUPPLY_LAKE,
            LinkType.SUPPLY_GROUNDWATER,
            LinkType.SUPPLY_DIVERSION,
            LinkType.SUPPLY_TRANSFER,
        }
        records = []
        for link in self.network.links.values():
            if link.link_type not in supply_types:
                continue
            target = self.network.nodes.get(link.to_node)
            records.append(
                {
                    "link_id": link.id,
                    "source_link_type": link.link_type.value,
                    "from_node": link.from_node,
                    "from_node_type": self._node_type(link.from_node),
                    "from_name": self._node_name(link.from_node),
                    "to_node": link.to_node,
                    "to_node_type": self._node_type(link.to_node),
                    "to_name": self._node_name(link.to_node),
                    "demand_node_id": getattr(target, "id", "") if isinstance(target, DemandNode) else "",
                    UNIT_ID: getattr(target, "unit_id", ""),
                    WATER_ZONE_ID: getattr(target, "water_zone_id", ""),
                    WATER_ZONE: getattr(target, "water_zone_name", ""),
                    BASIN_L2: getattr(target, "basin_l2", ""),
                    "city_code": getattr(target, "city_code", ""),
                    "city_name": getattr(target, "city_name", ""),
                    PROVINCE: getattr(target, "province", ""),
                    BASIN: getattr(target, "basin", ""),
                    "loss_rate": getattr(link, "loss_rate", 0.0),
                    "source_priority": getattr(link, "source_priority", ""),
                    "service_weight": getattr(link, "service_weight", ""),
                    "source_unit_ids": getattr(link, "source_unit_ids", ""),
                    "selection_method": getattr(link, "selection_method", ""),
                    "is_active": getattr(link, "is_active", True),
                }
            )
        return pd.DataFrame(records)

    def _build_return_flow_topology_detail(self) -> pd.DataFrame:
        records = []
        for link in self.network.get_links(LinkType.RETURN_FLOW):
            source = self.network.nodes.get(link.from_node)
            target = self.network.nodes.get(link.to_node)
            records.append(
                {
                    "link_id": link.id,
                    "from_node": link.from_node,
                    "from_name": self._node_name(link.from_node),
                    "demand_node_id": getattr(source, "id", "") if isinstance(source, DemandNode) else "",
                    UNIT_ID: getattr(source, "unit_id", ""),
                    WATER_ZONE_ID: getattr(source, "water_zone_id", ""),
                    WATER_ZONE: getattr(source, "water_zone_name", ""),
                    BASIN_L2: getattr(source, "basin_l2", ""),
                    "to_node": link.to_node,
                    "to_node_type": self._node_type(link.to_node),
                    "to_name": self._node_name(link.to_node),
                    "city_code": getattr(source, "city_code", ""),
                    "city_name": getattr(source, "city_name", ""),
                    "target_unit_id": getattr(target, "unit_id", ""),
                    "target_basin": getattr(target, "basin", ""),
                    "target_province": getattr(target, "province", ""),
                    "return_ratio": getattr(link, "return_ratio", 1.0),
                    "source_unit_ids": getattr(link, "source_unit_ids", ""),
                    "selection_method": getattr(link, "selection_method", ""),
                    "is_active": getattr(link, "is_active", True),
                }
            )
        return pd.DataFrame(records)

    def _build_groundwater_topology_summary(self) -> pd.DataFrame:
        records = []
        for node in self.network.get_nodes(NodeType.GROUNDWATER):
            if not isinstance(node, GroundwaterNode):
                continue
            dm = self.network.nodes.get(getattr(node, "demand_node_id", ""))
            records.append(
                {
                    "groundwater_node_id": node.id,
                    "city_code": node.city_code,
                    "city_name": node.city_name,
                    "demand_node_id": node.demand_node_id,
                    UNIT_ID: getattr(dm, "unit_id", ""),
                    WATER_ZONE_ID: getattr(dm, "water_zone_id", ""),
                    WATER_ZONE: getattr(dm, "water_zone_name", ""),
                    BASIN_L2: getattr(dm, "basin_l2", ""),
                    PROVINCE: node.province,
                    BASIN: node.basin,
                    "gw_exploitable_annual": round(float(node.exploitable_annual), 3),
                    "initial_over": round(float(node.initial_over), 3),
                    "gw_over_decay": round(float(node.over_decay), 4),
                }
            )
        return pd.DataFrame(records)

    def _build_eco_baseflow_summary(self) -> pd.DataFrame:
        records = []
        for sb in self.network.get_nodes(NodeType.SUBBASIN):
            if not isinstance(sb, SubbasinNode) or sb.eco_deficit_ts is None:
                continue
            if sb.mean_annual_runoff <= 0:
                continue
            deficit_months = int(np.sum(sb.eco_deficit_ts > 0))
            total_months = len(sb.eco_deficit_ts)
            records.append(
                {
                    ECO_UNIT: sb.name,
                    ECO_TYPE: "子流域",
                    BASIN: sb.basin,
                    PROVINCE: sb.province,
                    ECO_PRIORITY: sb.eco_priority,
                    ANNUAL_RUNOFF: round(sb.mean_annual_runoff, 0),
                    ANNUAL_ECO_BASEFLOW: round(float(np.sum(sb.eco_baseflow)), 0),
                    "年均生态基流缺水万m³": round(annualized_mean(sb.eco_deficit_ts), 1),
                    MISSING_MONTHS: deficit_months,
                    MISSING_FREQ: round(deficit_months / max(1, total_months), 3),
                }
            )

        for channel in self.network.get_nodes(NodeType.RIVER_CHANNEL):
            if not isinstance(channel, RiverChannelNode) or channel.eco_deficit_ts is None:
                continue
            deficit_months = int(np.sum(channel.eco_deficit_ts > 0))
            total_months = len(channel.eco_deficit_ts)
            records.append(
                {
                    ECO_UNIT: channel.name,
                    ECO_TYPE: "干流",
                    BASIN: channel.basin,
                    PROVINCE: channel.province,
                    ECO_PRIORITY: channel.eco_priority,
                    ANNUAL_TRANSIT_FLOW: round(channel.mean_annual_flow, 0),
                    ANNUAL_ECO_BASEFLOW: round(float(np.sum(channel.eco_baseflow)), 0),
                    "年均生态基流缺水万m³": round(annualized_mean(channel.eco_deficit_ts), 1),
                    MISSING_MONTHS: deficit_months,
                    MISSING_FREQ: round(deficit_months / max(1, total_months), 3),
                }
            )
        return pd.DataFrame(records)

    def _build_ecology_summary(self) -> pd.DataFrame:
        records = []
        for node in self.network.get_nodes(NodeType.OCEAN):
            if not isinstance(node, OceanNode) or node.eco_deficit_ts is None:
                continue
            deficit_months = int(np.sum(node.eco_deficit_ts > 0))
            total_months = len(node.eco_deficit_ts)
            records.append(
                {
                    SECTION: node.name,
                    RIVER: node.river_name,
                    ECO_TYPE: "入海口",
                    MISSING_MONTHS: deficit_months,
                    MISSING_FREQ: round(deficit_months / max(1, total_months), 3),
                    MEAN_MISSING: round(annualized_mean(node.eco_deficit_ts), 1),
                }
            )

        for node in self.network.get_nodes(NodeType.ECO_CONTROL):
            if not isinstance(node, EcoControlNode) or node.eco_deficit_ts is None:
                continue
            deficit_months = int(np.sum(node.eco_deficit_ts > 0))
            total_months = len(node.eco_deficit_ts)
            records.append(
                {
                    SECTION: node.name,
                    RIVER: node.river_name,
                    ECO_TYPE: "控制断面",
                    MISSING_MONTHS: deficit_months,
                    MISSING_FREQ: round(deficit_months / max(1, total_months), 3),
                    MEAN_MISSING: round(annualized_mean(node.eco_deficit_ts), 1),
                }
            )
        return pd.DataFrame(records)

    def build(self) -> dict:
        results = {"config": self.config.to_dict()}
        self._build_demand_summaries(results)
        self._build_demand_annual_summaries(results)

        unit_mix, city_mix = self._build_source_mix()
        results["source_mix_summary"] = unit_mix
        results["source_mix_unit_total"] = unit_mix
        results["source_mix_city_total"] = city_mix
        unit_mix_annual, city_mix_annual = self._build_source_mix_annual()
        results["source_mix_annual"] = unit_mix_annual
        results["source_mix_city_annual"] = city_mix_annual

        results["reservoir_summary"] = self._build_reservoir_summary()
        results["transfer_summary"] = self._build_transfer_summary()
        results["node_topology_summary"] = self._build_node_topology_summary()
        results["link_topology_summary"] = self._build_link_topology_summary()
        results["supply_topology_detail"] = self._build_supply_topology_detail()
        results["return_flow_topology_detail"] = self._build_return_flow_topology_detail()
        results["groundwater_topology_summary"] = self._build_groundwater_topology_summary()
        results["eco_baseflow_summary"] = self._build_eco_baseflow_summary()
        results["ecology_summary"] = self._build_ecology_summary()
        return results
