from __future__ import annotations

from collections import defaultdict

import numpy as np

from config import LinkType
from core.nodes import (
    DemandNode,
    GroundwaterNode,
    LakeNode,
    ReservoirNode,
    RiverDiversionNode,
    RiverChannelNode,
    SubbasinNode,
)
from water.allocation import WaterSource


class SourceBuilder:
    def __init__(self, network, gw_mod, config=None):
        self.network = network
        self.gw_mod = gw_mod
        self.config = config
        self.mapping = getattr(network, "_demand_mapping", None)
        self.structure_factors = getattr(network, "_source_structure_factors", None)

        self._enabled_cache = {}
        self._source_factor_cache = {}
        self._source_factor_year_fallback_cache = {}
        self._unit_ids_cache = {}
        self._subbasin_nodes_cache = {}
        self._linked_local_cache = {}
        self._unit_weight_cache = {}
        self._weighted_local_cache = {}
        self._supply_link_cache = {}
        self._supply_node_cache = {}
        self._local_surface_base_cache = {}
        self._local_surface_share_cache = {}
        self._local_supply_links_by_source = defaultdict(list)
        self._groundwater_balance_budget = {}
        self._groundwater_balance_remaining = {}
        self._named_float_map_cache = {}
        self._upstream_links_cache = {
            node_id: network.upstream_links(node_id)
            for node_id in getattr(network, "nodes", {})
        }
        for link in network.get_links(LinkType.SUPPLY_LOCAL):
            if getattr(link, "is_active", True):
                self._local_supply_links_by_source[link.from_node].append(link)

        self._years = None
        self._months = None
        if config is not None and hasattr(config, "time"):
            time_index = getattr(config.time, "time_index", None)
            if time_index is not None:
                self._years = np.array([int(ts.year) for ts in time_index], dtype=int)
                self._months = np.array([int(ts.month) for ts in time_index], dtype=int)

    def _source_factor(self, dm: DemandNode, source_type: str, year: int | None = None) -> float:
        factor_year = self._resolve_input_year("source_structure", year)
        cache_key = (dm.id, source_type, factor_year)
        cached = self._source_factor_cache.get(cache_key)
        if cached is not None:
            return cached

        factors = self.structure_factors or {}
        by_demand = factors.get("by_demand", {})
        by_demand_year = factors.get("by_demand_year", {})
        by_unit = factors.get("by_unit", {})
        by_unit_year = factors.get("by_unit_year", {})
        by_city = factors.get("by_city", {})
        by_city_year = factors.get("by_city_year", {})
        by_province = factors.get("by_province", {})
        by_province_year = factors.get("by_province_year", {})

        rec = None
        unit_id = str(getattr(dm, "unit_id", "") or "")
        if factor_year is not None:
            rec = by_demand_year.get((str(dm.id), factor_year), None)
            if rec is None and unit_id:
                rec = by_unit_year.get((unit_id, factor_year), None)
            if rec is None:
                rec = by_city_year.get((str(dm.city_code), factor_year), None)
            if rec is None:
                rec = by_province_year.get((str(dm.province), factor_year), None)
            if rec is None:
                rec = self._source_factor_year_fallback(
                    by_demand_year,
                    str(dm.id),
                    factor_year,
                )
            if rec is None and unit_id:
                rec = self._source_factor_year_fallback(
                    by_unit_year,
                    unit_id,
                    factor_year,
                )
            if rec is None:
                rec = self._source_factor_year_fallback(
                    by_city_year,
                    str(dm.city_code),
                    factor_year,
                )
            if rec is None:
                rec = self._source_factor_year_fallback(
                    by_province_year,
                    str(dm.province),
                    factor_year,
                )
        if rec is None:
            rec = by_demand.get(str(dm.id), None)
        if rec is None and unit_id:
            rec = by_unit.get(unit_id, None)
        if rec is None:
            rec = by_city.get(str(dm.city_code), None)
        if rec is None:
            rec = by_province.get(str(dm.province), None)
        if not rec:
            self._source_factor_cache[cache_key] = 1.0
            return 1.0

        specific_map = {
            "local_sw": "local_sw_factor",
            "groundwater": "groundwater_factor",
            "unconventional": "unconventional_factor",
            "reservoir": "reservoir_factor",
            "lake": "lake_factor",
            "diversion": "diversion_factor",
            "transfer": "transfer_factor",
        }
        value = 1.0
        key = specific_map.get(source_type)
        if key and key in rec:
            value = max(0.0, float(rec[key]))
        elif (
            source_type in {"local_sw", "reservoir", "lake", "diversion", "transfer"}
            and "surface_factor" in rec
        ):
            value = max(0.0, float(rec["surface_factor"]))
        elif source_type == "groundwater" and "groundwater_factor" in rec:
            value = max(0.0, float(rec["groundwater_factor"]))
        elif source_type == "unconventional" and "unconventional_factor" in rec:
            value = max(0.0, float(rec["unconventional_factor"]))

        self._source_factor_cache[cache_key] = value
        return value

    def _source_structure_fill_missing_years_enabled(self) -> bool:
        return bool(int(getattr(self.config, "source_structure_fill_missing_years", 1) or 0))

    def _source_factor_year_fallback(self, year_map: dict, key: str, year: int | None):
        """Return the nearest calibrated source-structure year for missing years.

        S2 covers 1965-2024, while province source-structure calibration
        targets are mostly available from 2000 onward.  For earlier historical
        years, falling back to a coarse non-year factor understates actual
        groundwater and local supply capacity in heavily developed regions.
        Using the nearest available calibrated year keeps the source mix tied
        to observed public-bulletin structure instead of leaving a large
        residual historical closure term.
        """
        if not self._source_structure_fill_missing_years_enabled():
            return None
        if year is None or not year_map:
            return None

        lookup_key = (id(year_map), str(key), int(year))
        cached = self._source_factor_year_fallback_cache.get(lookup_key)
        if lookup_key in self._source_factor_year_fallback_cache:
            return cached

        candidates = []
        for raw_key, rec in year_map.items():
            if not isinstance(raw_key, tuple) or len(raw_key) != 2:
                continue
            scope_key, rec_year = raw_key
            if str(scope_key) != str(key):
                continue
            try:
                candidates.append((int(rec_year), rec))
            except Exception:
                continue

        if not candidates:
            self._source_factor_year_fallback_cache[lookup_key] = None
            return None

        target_year = int(year)
        previous = [(rec_year, rec) for rec_year, rec in candidates if rec_year <= target_year]
        if previous:
            selected = max(previous, key=lambda item: item[0])[1]
        else:
            selected = min(candidates, key=lambda item: item[0])[1]
        self._source_factor_year_fallback_cache[lookup_key] = selected
        return selected

    def _resolve_input_year(self, domain: str, year: int | None) -> int | None:
        if year is None:
            return None
        resolver = getattr(self.config, "resolve_input_year", None)
        if callable(resolver):
            return int(resolver(domain, int(year)))
        return int(year)

    def _enabled(self, key: str, default: bool = True) -> bool:
        cached = self._enabled_cache.get(key)
        if cached is not None:
            return cached

        if hasattr(self.config, "is_process_enabled"):
            try:
                value = bool(self.config.is_process_enabled(key))
                self._enabled_cache[key] = value
                return value
            except AttributeError:
                pass
        value = getattr(self.config, key, default)
        if hasattr(self.config, "_switch_value"):
            value = bool(self.config._switch_value(value))
        else:
            value = bool(value)
        self._enabled_cache[key] = value
        return value

    def _get_unit_ids(self, dm: DemandNode):
        cached = self._unit_ids_cache.get(dm.id)
        if cached is not None:
            return cached
        if getattr(dm, "coupled_unit_ids", None):
            unit_ids = list(dm.coupled_unit_ids)
        elif self.mapping is not None:
            unit_ids = self.mapping.get_city_units(dm.city_code) or []
        else:
            unit_ids = []
        self._unit_ids_cache[dm.id] = unit_ids
        return unit_ids

    def _get_subbasin_nodes(self, dm: DemandNode):
        cached = self._subbasin_nodes_cache.get(dm.id)
        if cached is not None:
            return cached
        nodes = []
        for unit_id in self._get_unit_ids(dm):
            sb = self.network.nodes.get(f"SB_{unit_id}")
            if isinstance(sb, SubbasinNode):
                nodes.append(sb)
        self._subbasin_nodes_cache[dm.id] = nodes
        return nodes

    def _links_for(self, dm: DemandNode, link_type: LinkType):
        cache_key = (dm.id, link_type)
        cached = self._supply_link_cache.get(cache_key)
        if cached is not None:
            return cached
        links = [
            link
            for link in self._upstream_links_cache.get(dm.id, [])
            if link.link_type == link_type
        ]
        self._supply_link_cache[cache_key] = links
        return links

    def _nodes_for_links(self, dm: DemandNode, link_type: LinkType, node_cls):
        cache_key = (dm.id, link_type, node_cls)
        cached = self._supply_node_cache.get(cache_key)
        if cached is not None:
            return cached
        pairs = []
        for link in self._links_for(dm, link_type):
            node = self.network.nodes.get(link.from_node)
            if isinstance(node, node_cls):
                pairs.append((link, node))
        self._supply_node_cache[cache_key] = pairs
        return pairs

    def _get_linked_local_subbasins(self, dm: DemandNode):
        cached = self._linked_local_cache.get(dm.id)
        if cached is not None:
            return cached
        nodes = []
        for link in self._links_for(dm, LinkType.SUPPLY_LOCAL):
            sb = self.network.nodes.get(link.from_node)
            if isinstance(sb, SubbasinNode):
                nodes.append(sb)
        nodes = nodes or self._get_subbasin_nodes(dm)
        self._linked_local_cache[dm.id] = nodes
        return nodes

    def _unit_weight(self, dm: DemandNode, unit_id: str) -> float:
        cache_key = (dm.id, unit_id)
        cached = self._unit_weight_cache.get(cache_key)
        if cached is not None:
            return cached

        node_unit_id = str(getattr(dm, "unit_id", "") or "")
        if node_unit_id and str(unit_id) == node_unit_id:
            self._unit_weight_cache[cache_key] = 1.0
            return 1.0

        unit_ids = [str(uid) for uid in self._get_unit_ids(dm)]
        if len(unit_ids) == 1 and str(unit_id) == unit_ids[0]:
            self._unit_weight_cache[cache_key] = 1.0
            return 1.0

        if self.mapping is not None:
            weight = float(self.mapping.get_city_weight(dm.city_code, unit_id))
            if weight > 0:
                self._unit_weight_cache[cache_key] = weight
                return weight

        weight = 0.0 if not unit_ids else 1.0 / len(unit_ids)
        self._unit_weight_cache[cache_key] = weight
        return weight

    def _weighted_local_units(self, dm: DemandNode):
        cached = self._weighted_local_cache.get(dm.id)
        if cached is not None:
            return cached
        weighted = []
        for link in self._links_for(dm, LinkType.SUPPLY_LOCAL):
            node = self.network.nodes.get(link.from_node)
            if not isinstance(node, (SubbasinNode, RiverChannelNode)):
                continue
            weight = max(0.0, float(getattr(link, "service_weight", 0.0) or 0.0))
            if weight <= 0 and isinstance(node, SubbasinNode):
                weight = self._unit_weight(dm, node.unit_id)
            if weight > 0:
                weighted.append((weight, node))
        if not weighted:
            for sb in self._get_linked_local_subbasins(dm):
                weight = self._unit_weight(dm, sb.unit_id)
                if weight > 0:
                    weighted.append((weight, sb))
        weighted.sort(key=lambda item: item[0], reverse=True)
        self._weighted_local_cache[dm.id] = weighted
        return weighted

    def _local_surface_sharing_enabled_for_source(self, source_id: str) -> bool:
        if not bool(int(getattr(self.config, "share_local_surface_by_demand", 1) or 0)):
            return False
        target_basin = str(getattr(self.config, "water_rights_target_basin", "") or "").strip()
        if not target_basin:
            return True
        node = self.network.nodes.get(source_id)
        basin = str(getattr(node, "basin", "") or "").strip()
        return basin == target_basin

    def _local_surface_demand_share(self, source_id: str, dm: DemandNode, t: int) -> float:
        """Share one routed local source among linked demand units.

        Several demand units may take from the same river reach. Without an
        explicit sharing rule, traversal order lets earlier units see the full
        reach flow and concentrates shortage downstream in the model ordering.
        The share is based on same-month demand and the topology service weight,
        which keeps the constraint on the physical source but removes that
        ordering artifact.
        """
        if not self._local_surface_sharing_enabled_for_source(source_id):
            return 1.0

        links = self._local_supply_links_by_source.get(source_id, [])
        if len(links) <= 1:
            return 1.0

        cache_key = (source_id, int(t))
        share_map = self._local_surface_share_cache.get(cache_key)
        if share_map is None:
            demand_weights = {}
            service_weights = {}
            for link in links:
                target = self.network.nodes.get(link.to_node)
                if not isinstance(target, DemandNode):
                    continue
                service_weight = max(0.0, float(getattr(link, "service_weight", 1.0) or 0.0))
                service_weights[target.id] = service_weights.get(target.id, 0.0) + service_weight
                demand = max(0.0, float(target.total_demand(t)))
                if demand > 0:
                    demand_weights[target.id] = (
                        demand_weights.get(target.id, 0.0) + demand * max(service_weight, 1e-9)
                    )

            total = sum(demand_weights.values())
            if total <= 0:
                demand_weights = service_weights
                total = sum(demand_weights.values())

            if total > 0:
                share_map = {
                    target_id: max(0.0, value) / total
                    for target_id, value in demand_weights.items()
                }
            else:
                share_map = {}
            self._local_surface_share_cache[cache_key] = share_map

        return max(0.0, min(1.0, float(share_map.get(dm.id, 0.0))))

    def _groundwater_balance_enabled(self) -> bool:
        mode = str(getattr(self.config, "groundwater_balance_mode", "managed_overdraft") or "").strip().lower()
        return mode in {"recharge_balance", "annual_recharge_balance", "sustainable_recharge"}

    def _csv_tokens(self, value) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, (list, tuple, set)):
            raw_items = value
        else:
            raw_items = str(value).replace(";", ",").split(",")
        return {str(item).strip() for item in raw_items if str(item).strip()}

    def _historical_irrigation_canal_enabled(self, dm: DemandNode, t: int) -> bool:
        if not self._enabled("enable_historical_irrigation_canal_buffer", default=False):
            return False
        if self._months is not None and t < len(self._months):
            month = int(self._months[t])
        else:
            month = None
        months = {
            int(token)
            for token in self._csv_tokens(
                getattr(self.config, "historical_irrigation_canal_months", "")
            )
            if token.isdigit()
        }
        if months and month not in months:
            return False

        prefixes = self._csv_tokens(
            getattr(self.config, "historical_irrigation_canal_scope_prefixes", "")
        )
        if not prefixes:
            return False
        scope_keys = list(getattr(dm, "coupled_unit_ids", []) or [])
        unit_id = str(getattr(dm, "unit_id", "") or "")
        if unit_id:
            scope_keys.append(unit_id)
        return any(
            str(scope).startswith(prefix)
            for scope in scope_keys
            for prefix in prefixes
        )

    def _historical_irrigation_canal_target_access(self) -> float:
        value = float(
            getattr(self.config, "historical_irrigation_canal_target_access_ratio", 0.0)
            or 0.0
        )
        return max(0.0, min(1.0, value))

    def _groundwater_recharge_series(self, sb):
        data = getattr(sb, "precomputed_gw_recharge", None)
        if data is not None:
            return data
        return getattr(sb, "gw_recharge", None)

    def _groundwater_balance_key(self, dm: DemandNode, year: int | None):
        if year is None:
            return None
        return (dm.id, int(year))

    def _named_float_map(self, attr_name: str) -> dict[str, float]:
        cached = self._named_float_map_cache.get(attr_name)
        if cached is not None:
            return cached
        raw = getattr(self.config, attr_name, "") if self.config is not None else ""
        out: dict[str, float] = {}
        if isinstance(raw, dict):
            items = raw.items()
        else:
            text = str(raw or "").replace("；", ";").replace(",", ";")
            pairs = []
            for token in text.split(";"):
                token = token.strip()
                if not token:
                    continue
                if "=" in token:
                    k, v = token.split("=", 1)
                elif ":" in token:
                    k, v = token.split(":", 1)
                else:
                    continue
                pairs.append((k, v))
            items = pairs
        for key, value in items:
            name = str(key).strip()
            if not name:
                continue
            try:
                out[name] = float(value)
            except (TypeError, ValueError):
                continue
        self._named_float_map_cache[attr_name] = out
        return out

    def _basin_float_override(self, dm: DemandNode, attr_name: str, default: float) -> float:
        mapping = self._named_float_map(attr_name)
        if not mapping:
            return float(default)
        basin = str(getattr(dm, "basin", "") or "").strip()
        if basin in mapping:
            return float(mapping[basin])
        for key, value in mapping.items():
            if key and key in basin:
                return float(value)
        return float(default)

    def _groundwater_annual_recharge_budget(self, dm: DemandNode, year: int) -> float:
        key = (dm.id, int(year))
        cached = self._groundwater_balance_budget.get(key)
        if cached is not None:
            return cached

        if self._years is None:
            indices = []
        else:
            indices = np.where(self._years == int(year))[0]

        budget = 0.0
        for sb in self._get_subbasin_nodes(dm):
            weight = self._unit_weight(dm, sb.unit_id)
            series = self._groundwater_recharge_series(sb)
            if series is None or len(indices) == 0:
                continue
            valid = indices[indices < len(series)]
            if len(valid) > 0:
                budget += float(np.sum(np.asarray(series)[valid])) * weight

        factor = max(0.0, self._basin_float_override(
            dm,
            "groundwater_recharge_balance_factor_by_basin",
            float(getattr(self.config, "groundwater_recharge_balance_factor", 1.0) or 0.0),
        ))
        buffer_ratio = max(
            0.0,
            self._basin_float_override(
                dm,
                "groundwater_balance_storage_buffer_by_basin",
                float(getattr(self.config, "groundwater_balance_storage_buffer_ratio", 0.0) or 0.0),
            ),
        )
        budget = budget * factor + max(0.0, float(getattr(dm, "gw_exploitable_annual", 0.0) or 0.0)) * buffer_ratio
        budget = max(0.0, budget)
        self._groundwater_balance_budget[key] = budget
        self._groundwater_balance_remaining[key] = budget
        return budget

    def _groundwater_remaining_budget(self, dm: DemandNode, year: int | None) -> float:
        key = self._groundwater_balance_key(dm, year)
        if key is None:
            return float("inf")
        if key not in self._groundwater_balance_remaining:
            self._groundwater_annual_recharge_budget(dm, int(year))
        return max(0.0, float(self._groundwater_balance_remaining.get(key, 0.0)))

    def charge_groundwater_balance(self, dm: DemandNode, t: int, amount: float) -> None:
        if not self._groundwater_balance_enabled():
            return
        if self._years is None or t >= len(self._years):
            return
        key = self._groundwater_balance_key(dm, int(self._years[t]))
        if key is None:
            return
        if key not in self._groundwater_balance_remaining:
            self._groundwater_annual_recharge_budget(dm, int(self._years[t]))
        used = max(0.0, float(amount or 0.0))
        self._groundwater_balance_remaining[key] = max(
            0.0,
            float(self._groundwater_balance_remaining.get(key, 0.0)) - used,
        )

    def build_sources(self, dm: DemandNode, t, node_flow, trans_delivery):
        sources = []
        year = None
        if self._years is not None and t < len(self._years):
            year = int(self._years[t])
        unconventional_year = self._resolve_input_year("unconventional", year)

        subbasins = self._get_subbasin_nodes(dm)
        local_access_base = getattr(dm, "local_surface_access_ratio", None)
        if local_access_base is None:
            local_access_base = getattr(self.config, "local_surface_access_ratio", 0.35) or 0.35
        local_access_ratio = max(0.0, min(1.0, float(local_access_base)))
        max_units = max(1, int(getattr(self.config, "local_surface_max_units", 3) or 3))

        weighted_units = []
        for weight, sb in self._weighted_local_units(dm):
            if sb.id in node_flow:
                raw_available = max(0.0, node_flow.get(sb.id, 0.0))
            elif isinstance(sb, SubbasinNode) and sb.total_runoff is not None and t < len(sb.total_runoff):
                raw_available = max(0.0, sb.total_runoff[t])
            elif isinstance(sb, RiverChannelNode) and sb.outflow_series is not None and t < len(sb.outflow_series):
                raw_available = max(0.0, sb.outflow_series[t])
            else:
                raw_available = 0.0
            if raw_available > 0:
                weighted_units.append((weight, sb, raw_available))
                if len(weighted_units) >= max_units:
                    break

        selected_weight_sum = sum(item[0] for item in weighted_units)
        if selected_weight_sum > 0:
            local_factor = self._source_factor(dm, "local_sw", year)
            canal_buffer_enabled = self._historical_irrigation_canal_enabled(dm, int(t))
            canal_target_access = self._historical_irrigation_canal_target_access()
            canal_priority = float(
                getattr(self.config, "historical_irrigation_canal_priority", 1.5) or 1.5
            )
            for weight, sb, raw_available in weighted_units:
                base_key = (sb.id, int(t))
                if base_key not in self._local_surface_base_cache:
                    self._local_surface_base_cache[base_key] = raw_available
                source_base = max(0.0, float(self._local_surface_base_cache.get(base_key, raw_available)))
                demand_share = self._local_surface_demand_share(sb.id, dm, int(t))
                shared_available = (
                    min(raw_available, source_base * demand_share)
                    if demand_share < 1.0 else raw_available
                )
                local_available = (
                    shared_available
                    * (weight / selected_weight_sum)
                    * local_access_ratio
                    * local_factor
                )
                weighted_shared_available = shared_available * (weight / selected_weight_sum)
                if local_available > 0:
                    sources.append(
                        WaterSource(
                            source_id=sb.id,
                            source_name=f"local_surface_{getattr(sb, 'unit_id', sb.id)}",
                            source_type="local_sw",
                            available=local_available,
                            priority=1,
                            loss_rate=0.05,
                        )
                    )
                if canal_buffer_enabled and canal_target_access > 0:
                    current_access = max(0.0, local_access_ratio * local_factor)
                    canal_available = weighted_shared_available * max(
                        0.0, canal_target_access - current_access
                    )
                    if canal_available > 0:
                        sources.append(
                            WaterSource(
                                source_id=(
                                    f"{sb.id}::historical_irrigation_canal::{dm.id}"
                                ),
                                source_name=(
                                    f"historical_irrigation_canal_{getattr(sb, 'unit_id', sb.id)}"
                                ),
                                source_type="local_sw",
                                available=canal_available,
                                priority=canal_priority,
                                loss_rate=0.05,
                            )
                        )

        if self._enabled("enable_groundwater_allocation"):
            gw_recharge = 0.0
            for sb in subbasins:
                weight = self._unit_weight(dm, sb.unit_id)
                if sb.gw_recharge is not None and t < len(sb.gw_recharge):
                    gw_recharge += sb.gw_recharge[t] * weight
            gw_avail = self.gw_mod.calc_monthly_exploitable(
                dm.gw_exploitable_annual,
                gw_recharge,
                dm.gw_cumulative_over,
                dm.gw_over_decay,
            )
            gw_avail *= self._source_factor(dm, "groundwater", year)
            if self._groundwater_balance_enabled():
                gw_avail = min(gw_avail, self._groundwater_remaining_budget(dm, year))
            if gw_avail > 0:
                source_id = f"{dm.id}_gw"
                source_name = "groundwater"
                for _, gw_node in self._nodes_for_links(dm, LinkType.SUPPLY_GROUNDWATER, GroundwaterNode):
                    source_id = gw_node.id
                    source_name = gw_node.name or source_name
                    break
                sources.append(
                    WaterSource(
                        source_id=source_id,
                        source_name=source_name,
                        source_type="groundwater",
                        available=gw_avail,
                        priority=2,
                    )
                )

        unconventional_by_demand = getattr(self.network, "_unconventional_supply_by_demand_year", {})
        unconventional_by_city = getattr(self.network, "_unconventional_supply_by_city_year", {})
        if unconventional_year is not None and (unconventional_by_demand or unconventional_by_city):
            annual_supply = float(
                unconventional_by_demand.get((dm.id, unconventional_year), 0.0) or 0.0
            )
            if annual_supply <= 0:
                annual_supply = float(
                    unconventional_by_city.get((str(dm.city_code), unconventional_year), 0.0) or 0.0
                )
                annual_supply *= float(
                    getattr(dm, "demand_sector_weights", {}).get(
                        "unconventional",
                        getattr(dm, "demand_area_weight", 1.0),
                    )
                    or 0.0
                )
            annual_supply *= self._source_factor(dm, "unconventional", year)
            monthly_supply = max(0.0, annual_supply / 12.0)
            if monthly_supply > 0:
                priority = float(
                    getattr(self.config, "unconventional_supply_priority", 0.8) or 0.8
                )
                sources.append(
                    WaterSource(
                        source_id=f"{dm.id}_unconventional",
                        source_name="unconventional",
                        source_type="unconventional",
                        available=monthly_supply,
                        priority=priority,
                    )
                )

        if self._enabled("enable_reservoirs"):
            for link, fn in self._nodes_for_links(dm, LinkType.SUPPLY_RESERVOIR, ReservoirNode):
                avail = max(0.0, node_flow.get(fn.id, 0.0))
                if avail <= 0 and fn.release_ts is not None and t < len(fn.release_ts):
                    avail = max(0.0, fn.release_ts[t])
                avail *= self._source_factor(dm, "reservoir", year)
                if avail > 0:
                    sources.append(
                        WaterSource(
                            source_id=fn.id,
                            source_name=f"reservoir_{fn.name}",
                            source_type="reservoir",
                            available=avail,
                            priority=3,
                            loss_rate=link.loss_rate,
                        )
                    )

        if self._enabled("enable_lakes"):
            for link, fn in self._nodes_for_links(dm, LinkType.SUPPLY_LAKE, LakeNode):
                if fn.id in node_flow:
                    avail = max(0.0, node_flow.get(fn.id, 0.0))
                elif fn.outflow_ts is not None and t < len(fn.outflow_ts):
                    avail = max(0.0, fn.outflow_ts[t])
                else:
                    avail = 0.0
                avail *= self._source_factor(dm, "lake", year)
                if avail > 0:
                    sources.append(
                        WaterSource(
                            source_id=fn.id,
                            source_name=f"lake_{fn.name}",
                            source_type="lake",
                            available=avail,
                            priority=3,
                            loss_rate=link.loss_rate,
                        )
                    )

        if self._enabled("enable_diversions"):
            for link, fn in self._nodes_for_links(dm, LinkType.SUPPLY_DIVERSION, RiverDiversionNode):
                if fn.actual_ts is not None and t < len(fn.actual_ts):
                    ratio = fn.supply_to.get(dm.id, 1.0)
                    avail = fn.actual_ts[t] * ratio
                    avail *= self._source_factor(dm, "diversion", year)
                    if avail > 0:
                        sources.append(
                            WaterSource(
                                source_id=fn.id,
                                source_name=f"diversion_{fn.name}",
                                source_type="diversion",
                                available=avail,
                                priority=3,
                                loss_rate=link.loss_rate,
                            )
                        )

        if self._enabled("enable_transfers"):
            td = trans_delivery.get(dm.id, 0)
            if td > 0:
                td *= self._source_factor(dm, "transfer", year)
            if td > 0:
                sources.append(
                    WaterSource(
                        source_id=f"{dm.id}_transfer",
                        source_name="transfer",
                        source_type="transfer",
                        available=td,
                        priority=0,
                    )
                )

        return sources
