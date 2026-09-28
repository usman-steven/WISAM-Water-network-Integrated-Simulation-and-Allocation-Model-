"""
engine/simulator.py - 仿真引擎核心

月循环流程：
  ① 土地利用参数更新（每年1月）
  ② 产流计算（非干流产流单元）
  ③ 河网汇流 + 水资源配置（拓扑排序遍历）
     SubbasinNode:      产流 → 水库 → 湖泊 → 引水 → 调水 → 出流
     RiverChannelNode:  汇集 → 水库 → 湖泊 → 引水 → 调水 → 损失 → 出流
     DemandNode:        收集水源 → 配置 → 退水
  ④ 生态流量检查
  ⑤ 地下水超采更新
"""
import importlib
import os
from pathlib import Path
import numpy as np
import pandas as pd
import time as _time
from typing import Dict, List, Optional
from collections import defaultdict

from config import (ModelConfig, NodeType, LinkType, Sector, SourceType,
                     RunoffMethod, ETMethod)
from core.network import WaterNetwork
from core.nodes import (SubbasinNode, RiverChannelNode,
                         ReservoirNode, LakeNode, DemandNode,
                         RiverDiversionNode, OceanNode,
                         EcoControlNode, JunctionNode)
from core.links import Link
from hydro.pet import calc_pet
from hydro.rainfall_runoff import abcd_step, gr2m_step, coefficient_step
from hydro.snowmelt import SnowModule
from hydro.groundwater import GroundwaterModule
from water.demand import DemandModule
from water.allocation import AllocationEngine, WaterSource, AllocationResult
from water.ecology import EcologyModule
from water.eco_baseflow import EcoBaseflowModule
from management import ResultsBuilder, SourceBuilder
from engineering import (
    ReservoirModule, LakeModule, DiversionModule, TransferModule,
    FACILITY_ENTRY, FACILITY_OUTLET,
)

class Simulator:

    def __init__(self, config: ModelConfig):
        self.config = config
        self.network = None

        # 子模块
        self.demand_mod = DemandModule(config)
        self.reservoir_mod = ReservoirModule(config)
        self.lake_mod = LakeModule()
        self.diversion_mod = DiversionModule()
        self.transfer_mod = TransferModule()
        self.alloc_engine = AllocationEngine(config.allocation_method)
        self.ecology_mod = EcologyModule()
        self.gw_mod = GroundwaterModule(
            config.is_process_enabled('enable_gw_overexploit')
            if hasattr(config, 'is_process_enabled')
            else config.enable_gw_overexploit
        )

        self.eco_baseflow_mod = EcoBaseflowModule(config)

        # 融雪
        self.snow_modules: Dict[str, SnowModule] = {}

        # 土地利用
        self.landuse_mod = None

        # 状态
        self._initialized = False
        self.results = {}
        self.log: List[str] = []
        self.results_builder = None
        self.source_builder = None
        self.audit = {}
        self._water_rights_scheme = None
        self._water_rights_initialized_steps = set()
        self._water_rights_remaining = {}
        self._yellow_annual_flow_ratio = {}
        self._control_nodes_by_channel = defaultdict(list)
        self._pending_return_flow = defaultdict(float)
        self._routing_topology = []
        self._process_switch_cache = {}
        self._nodes_by_type_cache = {}
        self._upstream_links_cache = {}
        self._downstream_links_cache = {}
        self._facility_graph_cache = {}
        self._facility_graph_nodes_cache = {}
        self._demand_total_series_by_node = {}
        self._downstream_demand_series_by_node = {}
        self._incoming_flow_links_cache = {}
        self._incoming_return_links_cache = {}
        self._sector_values = [s.value for s in Sector]
        self._years = None
        self._months = None

    def _resolve_input_year(self, domain: str, calendar_year: int) -> int:
        resolver = getattr(self.config, "resolve_input_year", None)
        if callable(resolver):
            return int(resolver(domain, int(calendar_year)))
        return int(calendar_year)

    def set_network(self, net: WaterNetwork):
        """设置网络并注册调水/引水"""
        self.network = net
        self._process_switch_cache = {}
        self._nodes_by_type_cache = {
            node_type: net.get_nodes(node_type)
            for node_type in NodeType
        }
        self._upstream_links_cache = {
            node_id: net.upstream_links(node_id)
            for node_id in net.nodes
        }
        self._downstream_links_cache = {
            node_id: net.downstream_links(node_id)
            for node_id in net.nodes
        }
        self._incoming_flow_links_cache = {}
        self._incoming_return_links_cache = {}
        for node_id, links in self._upstream_links_cache.items():
            flow_links = []
            return_links = []
            for link in links:
                if link.link_type in (LinkType.RIVER, LinkType.TRIBUTARY):
                    if link.is_active:
                        flow_links.append((
                            link.from_node,
                            max(0.0, 1.0 - float(getattr(link, 'loss_rate', 0.0) or 0.0)),
                        ))
                elif link.link_type == LinkType.RETURN_FLOW:
                    ratio = max(0.0, float(getattr(link, 'return_ratio', 1.0) or 0.0))
                    return_links.append((
                        f"_return_{link.id}",
                        f"_return_{link.from_node}",
                        ratio,
                    ))
            self._incoming_flow_links_cache[node_id] = tuple(flow_links)
            self._incoming_return_links_cache[node_id] = tuple(return_links)
        time_index = getattr(self.config.time, 'time_index', None)
        if time_index is not None:
            self._years = np.array([int(ts.year) for ts in time_index], dtype=int)
            self._months = np.array([int(ts.month) for ts in time_index], dtype=int)
        else:
            self._years = None
            self._months = None
        self.results_builder = ResultsBuilder(self.config, net, self.transfer_mod)
        self.source_builder = SourceBuilder(net, self.gw_mod, self.config)

        # 从网络中取出调水工程并注册
        if hasattr(net, '_transfer_projects'):
            for proj in net._transfer_projects:
                self.transfer_mod.add(proj)

        # 从网络中取出引水口并注册到diversion模块
        for node in self._nodes(NodeType.RIVER_DIVERSION):
            if isinstance(node, RiverDiversionNode):
                self.diversion_mod.register(node)

        self._control_nodes_by_channel = defaultdict(list)
        for node in (self._nodes(NodeType.OCEAN) + self._nodes(NodeType.ECO_CONTROL)):
            channel_id = getattr(node, 'channel_node_id', '')
            if channel_id:
                self._control_nodes_by_channel[channel_id].append(node)

        self._routing_topology = net.topological_sort(include_management=True)
        self._cache_facility_graphs(net)
        self._cache_demand_totals()
        self._setup_water_rights()

        # 取出土地利用模块
        self.landuse_mod = getattr(net, '_landuse_mod', None)

    def _nodes(self, node_type: NodeType) -> list:
        if self._nodes_by_type_cache:
            return self._nodes_by_type_cache.get(node_type, [])
        return self.network.get_nodes(node_type)

    def _cache_facility_graphs(self, net: WaterNetwork):
        self._facility_graph_cache = {}
        self._facility_graph_nodes_cache = {}
        registry = getattr(net, '_facility_graph', None)
        graphs = getattr(registry, '_graphs', {}) if registry is not None else {}
        for anchor_id, graph in graphs.items():
            if graph is None or not getattr(graph, 'edges', None):
                continue
            outgoing = defaultdict(list)
            for edge in graph.edges:
                outgoing[edge.from_node].append(edge)
            for edges in outgoing.values():
                edges.sort(key=lambda edge: (edge.priority, edge.to_node))
            graph_nodes = set(graph.graph_nodes())
            self._facility_graph_nodes_cache[anchor_id] = graph_nodes
            self._facility_graph_cache[anchor_id] = {
                'outgoing': {key: tuple(value) for key, value in outgoing.items()},
                'topological_order': tuple(graph.topological_order()),
                'graph_nodes': graph_nodes,
            }

    def _cache_demand_totals(self):
        self._demand_total_series_by_node = {}
        self._downstream_demand_series_by_node = {}
        n = self.config.time.n_steps
        for dm in self._nodes(NodeType.DEMAND):
            if not isinstance(dm, DemandNode):
                continue
            total = np.zeros(n)
            for arr in dm.demands.values():
                if arr is None:
                    continue
                n_use = min(n, len(arr))
                total[:n_use] += np.asarray(arr[:n_use], dtype=float)
            self._demand_total_series_by_node[dm.id] = total

        for node_id in self.network.nodes:
            downstream = []
            for link in self._downstream_links_cache.get(node_id, []):
                node = self.network.nodes.get(link.to_node)
                if isinstance(node, DemandNode):
                    series = self._demand_total_series_by_node.get(node.id)
                    if series is not None:
                        downstream.append(series)
            if downstream:
                self._downstream_demand_series_by_node[node_id] = np.sum(downstream, axis=0)

    def _setup_water_rights(self):
        """Prepare annual flow ratios and runtime quota pools for water-right controls."""
        self._water_rights_scheme = getattr(self.network, "_water_rights_scheme", None)
        self._water_rights_initialized_steps = set()
        self._water_rights_remaining = {}
        self._yellow_annual_flow_ratio = {}

        if not self._water_rights_scheme:
            return

        target_basin = str(
            getattr(self.config, "water_rights_target_basin", "黄河") or "黄河"
        )
        time_index = getattr(self.config.time, "time_index", None)
        if time_index is None or len(time_index) == 0:
            return
        years = np.array([int(ts.year) for ts in time_index], dtype=int)
        basin_monthly = np.zeros(len(time_index), dtype=float)
        for sb in self._nodes(NodeType.SUBBASIN):
            if not isinstance(sb, SubbasinNode):
                continue
            if str(getattr(sb, "basin", "")) != target_basin:
                continue
            series = getattr(sb, "precomputed_runoff", None)
            if series is None or len(series) == 0:
                series = getattr(sb, "total_runoff", None)
            if series is None or len(series) == 0:
                continue
            arr = np.asarray(series, dtype=float)
            n_use = min(len(arr), len(basin_monthly))
            basin_monthly[:n_use] += np.maximum(arr[:n_use], 0.0)

        annual = {
            int(year): float(basin_monthly[years == int(year)].sum())
            for year in sorted(set(years))
        }
        positive = [value for value in annual.values() if value > 0]
        mean_annual = float(np.mean(positive)) if positive else 0.0
        for year, value in annual.items():
            self._yellow_annual_flow_ratio[year] = (
                value / mean_annual if mean_annual > 0 else 1.0
            )

    def _water_rights_enabled_for(self, year: int) -> bool:
        if not self._water_rights_scheme:
            return False
        if not self._process_enabled("enable_water_rights", default=False):
            return False
        apply_from = int(getattr(self.config, "water_rights_apply_from_year", 1987) or 1987)
        return int(year) >= apply_from

    def _water_right_budget_mode(self) -> str:
        mode = str(getattr(self.config, "water_rights_budget_mode", "monthly") or "monthly")
        mode = mode.strip().lower().replace("-", "_")
        if mode in {"annual", "year", "yearly", "annual_carryover"}:
            return "annual"
        return "monthly"

    def _water_right_budget_key(self, t: int, quota_key: str) -> tuple:
        if self._water_right_budget_mode() == "annual" and self._years is not None and t < len(self._years):
            return (int(self._years[t]), quota_key)
        return (int(t), quota_key)

    def _water_right_demand_accounting_mode(self) -> str:
        mode = str(
            getattr(self.config, "water_rights_demand_accounting", "gross_withdrawal")
            or "gross_withdrawal"
        )
        mode = mode.strip().lower().replace("-", "_")
        if mode in {"net", "net_consumption", "consumption", "depletion"}:
            return "net_consumption"
        return "gross_withdrawal"

    def _demand_consumptive_fraction(self, dm: DemandNode, t: int) -> float:
        demands = []
        for sector in self._sector_values:
            arr = dm.demands.get(sector)
            value = float(arr[t]) if arr is not None and t < len(arr) else 0.0
            if value <= 0:
                continue
            return_ratio = max(0.0, min(1.0, float(dm.return_ratios.get(sector, 0.0))))
            demands.append((value, 1.0 - return_ratio))
        total = sum(value for value, _ in demands)
        if total <= 0:
            return 1.0
        return max(0.0, min(1.0, sum(value * frac for value, frac in demands) / total))

    def _water_right_available_charge_factor(
        self,
        dm: DemandNode,
        src: WaterSource,
        t: int,
    ) -> float:
        if self._water_right_demand_accounting_mode() != "net_consumption":
            return 1.0
        delivered_fraction = max(0.0, 1.0 - float(src.loss_rate or 0.0))
        return max(1e-6, delivered_fraction * self._demand_consumptive_fraction(dm, t))

    def _water_right_annual_factor(self, ratio: float) -> float:
        scheme = self._water_rights_scheme
        ratio = float(ratio or 1.0)
        if scheme is None:
            return 1.0
        if ratio > 1.2:
            return float(getattr(scheme, "wet_year_factor", 1.15) or 1.15)
        if ratio < 0.5:
            return float(getattr(scheme, "extreme_dry_factor", 0.70) or 0.70)
        if ratio < 0.8:
            return float(getattr(scheme, "dry_year_factor", 0.85) or 0.85)
        return 1.0

    def _ensure_water_right_budget(self, t: int, year: int, month: int) -> None:
        if not self._water_rights_enabled_for(year):
            return
        budget_mode = self._water_right_budget_mode()
        audit_key = ("audit", int(t))
        if audit_key in self._water_rights_initialized_steps:
            return
        scheme = self._water_rights_scheme
        ratio = float(self._yellow_annual_flow_ratio.get(int(year), 1.0) or 1.0)
        for quota_key in sorted(getattr(scheme, "annual_quota", {}) or {}):
            quota = scheme.get_monthly_quota_by_key(quota_key, month, ratio)
            if not np.isfinite(quota):
                continue
            quota = max(0.0, float(quota))
            if budget_mode == "annual":
                remaining_key = (int(year), quota_key)
                if remaining_key not in self._water_rights_remaining:
                    annual_quota = max(
                        0.0,
                        float(scheme.annual_quota.get(quota_key, 0.0) or 0.0)
                        * self._water_right_annual_factor(ratio),
                    )
                    self._water_rights_remaining[remaining_key] = annual_quota
            else:
                self._water_rights_remaining[(int(t), quota_key)] = quota
            self.audit["water_rights_quota_total"][t] += quota
            self.audit["water_rights_quota_by_key"][quota_key][t] += quota
        self._water_rights_initialized_steps.add(audit_key)

    def _water_right_quota_key(self, province: str) -> str:
        scheme = self._water_rights_scheme
        if scheme is None:
            return ""
        return scheme.quota_key_for(str(province or "").strip())

    def _remaining_water_right(self, t: int, quota_key: str) -> float:
        if not quota_key:
            return float("inf")
        return float(
            self._water_rights_remaining.get(
                self._water_right_budget_key(t, quota_key),
                float("inf"),
            )
        )

    def _charge_water_right(self, t: int, quota_key: str, amount: float) -> float:
        amount = max(0.0, float(amount or 0.0))
        if amount <= 0 or not quota_key:
            return 0.0
        remaining = self._remaining_water_right(t, quota_key)
        if not np.isfinite(remaining):
            return amount
        used = min(amount, max(0.0, remaining))
        self._water_rights_remaining[self._water_right_budget_key(t, quota_key)] = max(
            0.0, remaining - used
        )
        self.audit["water_rights_use_total"][t] += used
        self.audit["water_rights_use_by_key"][quota_key][t] += used
        if amount > used:
            capped = amount - used
            self.audit["water_rights_capped_total"][t] += capped
            self.audit["water_rights_capped_by_key"][quota_key][t] += capped
        return used

    def _physical_source_id(self, source_id: str) -> str:
        text = str(source_id or "")
        if "::" in text:
            return text.split("::", 1)[0]
        return text

    def _node_effective_basin(self, node_id: str) -> str:
        node = self.network.nodes.get(self._physical_source_id(node_id))
        if node is None:
            return ""
        basin = str(getattr(node, "basin", "") or "")
        if basin:
            return basin
        anchor = str(getattr(node, "channel_node_id", "") or "")
        if anchor and anchor in self.network.nodes:
            return str(getattr(self.network.nodes[anchor], "basin", "") or "")
        return ""

    def _is_water_right_surface_source(self, source_id: str) -> bool:
        target_basin = str(
            getattr(self.config, "water_rights_target_basin", "黄河") or "黄河"
        )
        return self._node_effective_basin(source_id) == target_basin

    def _source_counts_for_water_rights(self, src: WaterSource) -> bool:
        if src.source_type not in {"local_sw", "reservoir", "lake", "diversion"}:
            return False
        return self._is_water_right_surface_source(src.source_id)

    def _cap_sources_by_water_rights(
        self,
        dm: DemandNode,
        sources: List[WaterSource],
        t: int,
        year: int,
        month: int,
    ) -> None:
        if not self._water_rights_enabled_for(year):
            return
        quota_key = self._water_right_quota_key(getattr(dm, "province", ""))
        if not quota_key:
            return
        self._ensure_water_right_budget(t, year, month)
        remaining = self._remaining_water_right(t, quota_key)
        if not np.isfinite(remaining):
            return

        eligible = [src for src in sources if self._source_counts_for_water_rights(src)]
        eligible.sort(key=lambda src: (float(src.priority), src.source_id))
        budget = max(0.0, remaining)
        for src in eligible:
            original = max(0.0, float(src.available or 0.0))
            charge_factor = self._water_right_available_charge_factor(dm, src, t)
            allowed = original
            if original * charge_factor > budget:
                allowed = budget / charge_factor if charge_factor > 0 else 0.0
            src.available = allowed
            budget = max(0.0, budget - allowed * charge_factor)

    def _charge_demand_water_rights(
        self,
        dm: DemandNode,
        t: int,
        year: int,
        month: int,
        result: AllocationResult,
        source_map: Dict[str, WaterSource],
    ) -> None:
        if not self._water_rights_enabled_for(year):
            return
        quota_key = self._water_right_quota_key(getattr(dm, "province", ""))
        if not quota_key:
            return
        self._ensure_water_right_budget(t, year, month)
        net_accounting = self._water_right_demand_accounting_mode() == "net_consumption"
        for sector, alloc_detail in result.alloc_detail.items():
            return_ratio = max(0.0, min(1.0, float(dm.return_ratios.get(sector, 0.0))))
            for source_id, delivered in alloc_detail.items():
                src = source_map.get(source_id)
                if src is None or not self._source_counts_for_water_rights(src):
                    continue
                delivered = max(0.0, float(delivered or 0.0))
                if net_accounting:
                    charge = delivered * max(0.0, 1.0 - return_ratio)
                else:
                    charge = delivered / max(1e-6, 1.0 - float(src.loss_rate or 0.0))
                self._charge_water_right(t, quota_key, charge)

    def _water_right_transfer_limit(
        self,
        t: int,
        year: int,
        month: int,
        receivers: Dict[str, float],
        loss_rate: float,
    ) -> float:
        if not self._water_rights_enabled_for(year):
            return float("inf")
        self._ensure_water_right_budget(t, year, month)
        count_loss = bool(int(getattr(self.config, "water_rights_count_transfer_loss", 1) or 0))
        gross_factor = 1.0 if count_loss else max(1e-6, 1.0 - float(loss_rate or 0.0))
        ratio_by_key = defaultdict(float)
        for recv_id, ratio in receivers.items():
            node = self.network.nodes.get(recv_id)
            province = str(getattr(node, "province", "") or "")
            quota_key = self._water_right_quota_key(province)
            if quota_key:
                ratio_by_key[quota_key] += max(0.0, float(ratio or 0.0)) * gross_factor
        if not ratio_by_key:
            return float("inf")
        limits = []
        for quota_key, ratio in ratio_by_key.items():
            if ratio <= 0:
                continue
            remaining = self._remaining_water_right(t, quota_key)
            if np.isfinite(remaining):
                limits.append(max(0.0, remaining) / ratio)
        return min(limits) if limits else float("inf")

    def _charge_transfer_water_rights(
        self,
        t: int,
        year: int,
        month: int,
        receivers: Dict[str, float],
        actual_withdrawal: float,
        loss_rate: float,
    ) -> None:
        if not self._water_rights_enabled_for(year):
            return
        self._ensure_water_right_budget(t, year, month)
        count_loss = bool(int(getattr(self.config, "water_rights_count_transfer_loss", 1) or 0))
        base = max(0.0, float(actual_withdrawal or 0.0))
        if not count_loss:
            base *= max(0.0, 1.0 - float(loss_rate or 0.0))
        for recv_id, ratio in receivers.items():
            node = self.network.nodes.get(recv_id)
            province = str(getattr(node, "province", "") or "")
            quota_key = self._water_right_quota_key(province)
            if quota_key:
                self._charge_water_right(t, quota_key, base * max(0.0, float(ratio or 0.0)))

    def _process_enabled(self, key: str, default: bool = True) -> bool:
        cached = self._process_switch_cache.get(key)
        if cached is not None:
            return cached

        # 所有模块统一从 ModelConfig.model_switches 读取开关。
        if hasattr(self.config, 'is_process_enabled'):
            try:
                value = bool(self.config.is_process_enabled(key))
                self._process_switch_cache[key] = value
                return value
            except AttributeError:
                pass
        value = getattr(self.config, key, default)
        if hasattr(self.config, '_switch_value'):
            value = bool(self.config._switch_value(value))
        else:
            value = bool(value)
        self._process_switch_cache[key] = value
        return value

    # ══════════════════════════════════════
    #  初始化
    # ══════════════════════════════════════

    def initialize(self):
        n = self.config.time.n_steps
        self._init_audit(n)
        self._pending_return_flow = defaultdict(float)
        self._log(f"初始化 {n} 步 "
                  f"({self.config.time.start_year}~{self.config.time.end_year})")

        # ── PET计算（仅在PET未加载时） ──
        self._log("检查蒸散发...")
        et_count = 0
        for sb in self._nodes(NodeType.SUBBASIN):
            if isinstance(sb, SubbasinNode) and sb.pet is None:
                sb.pet = np.zeros(n)
                for t in range(n):
                    month = self.config.time.time_index[t].month
                    sb.pet[t] = calc_pet(sb, month, t, self.config.et_method)
                et_count += 1
        if et_count > 0:
            self._log(f"  {et_count} 个单元计算了PET")
        else:
            self._log(f"  所有单元已有PET数据")

        # ── 融雪模块 ──
        if self._process_enabled('enable_snowmelt'):
            snow_count = 0
            for sb in self._nodes(NodeType.SUBBASIN):
                if isinstance(sb, SubbasinNode) and sb.is_high_altitude:
                    self.snow_modules[sb.id] = SnowModule()
                    snow_count += 1
            if snow_count > 0:
                self._log(f"  {snow_count} 个高海拔单元启用融雪")

        # ── 产流结果序列 ──
        for sb in self._nodes(NodeType.SUBBASIN):
            if isinstance(sb, SubbasinNode):
                sb.total_runoff = np.zeros(n)
                sb.surface_runoff = np.zeros(n)
                sb.baseflow = np.zeros(n)
                sb.gw_recharge = np.zeros(n)

        # ── 河道节点序列 ──
        for ch in self._nodes(NodeType.RIVER_CHANNEL):
            if isinstance(ch, RiverChannelNode):
                ch.inflow_series = np.zeros(n)
                ch.outflow_series = np.zeros(n)
                ch.routing_storage = 0.0
                ch.routing_storage_ts = np.zeros(n)

        # ── 水库初始化 ──
        res_count = 0
        if self._process_enabled('enable_reservoirs'):
            for res in self._nodes(NodeType.RESERVOIR):
                if isinstance(res, ReservoirNode):
                    self.reservoir_mod.init_reservoir(res, n_steps=n)
                    self.audit['reservoir_initial_storage_by_id'][res.id] = float(res.current_storage)
                    res_count += 1
        if res_count > 0:
            self._log(f"  {res_count} 座水库初始化")

        # ── 湖泊初始化 ──
        lake_count = 0
        if self._process_enabled('enable_lakes'):
            for lake in self._nodes(NodeType.LAKE):
                if isinstance(lake, LakeNode):
                    self.lake_mod.init_lake(lake, n_steps=n)
                    self.audit['lake_initial_storage_by_id'][lake.id] = float(lake.current_storage)
                    lake_count += 1
        if lake_count > 0:
            self._log(f"  {lake_count} 座湖泊初始化")

        # ── 调水/引水序列 ──
        self.transfer_mod.init_series(n)
        self.diversion_mod.init_series(n)

        # ── 需水节点序列 ──
        dm_count = 0
        for dm in self._nodes(NodeType.DEMAND):
            if isinstance(dm, DemandNode):
                for s in [s.value for s in Sector]:
                    if s not in dm.allocation:
                        dm.allocation[s] = np.zeros(n)
                    if s not in dm.shortage:
                        dm.shortage[s] = np.zeros(n)
                    if s not in dm.demands:
                        dm.demands[s] = np.zeros(n)
                    if s not in dm.return_flow_by_sector:
                        dm.return_flow_by_sector[s] = np.zeros(n)
                    if s not in dm.consumption:
                        dm.consumption[s] = np.zeros(n)
                for src_type in ['local_sw', 'groundwater', 'unconventional',
                                 'reservoir', 'lake', 'diversion', 'transfer',
                                 'historical_closure']:
                    if src_type not in dm.source_supply:
                        dm.source_supply[src_type] = np.zeros(n)
                dm_count += 1
        self._log(f"  {dm_count} 个需水节点初始化")

        # Dynamic demand monthly redistribution
        if (self._process_enabled('enable_dynamic_demand') and
                self._process_enabled('enable_demands')):
            adj = self.demand_mod.precompute_monthly_weights(
                self.network, n)
            self._log(f"  dynamic demand monthly redistribution: {adj} nodes")
        self._cache_demand_totals()

        if self._process_enabled('enable_transfers'):
            self.transfer_mod.prepare_monthly_plans(self.network, self.config)

        # Ecology control node series
        all_eco = (self._nodes(NodeType.OCEAN) +
                   self._nodes(NodeType.ECO_CONTROL))
        self.ecology_mod.init_nodes(all_eco, n)

        self._initialized = True
        self._log("初始化完成")
         # ── 生态基流计算（蒙大拿法）──
        sb_eco, ch_eco = self.eco_baseflow_mod.compute_eco_baseflow(
            self.network, n)
        self._log(f"  生态基流: {sb_eco} 个子流域 + {ch_eco} 个干流节点")

    # ══════════════════════════════════════
    #  主运行
    # ══════════════════════════════════════

    def run(self, progress_cb=None):
        if not self._initialized:
            self.initialize()

        ti = self.config.time.time_index
        n = len(ti)
        self._log(f"开始模拟 ({n}步)...")
        t0 = _time.time()

        for t in range(n):
            year = int(self._years[t]) if self._years is not None else ti[t].year
            month = int(self._months[t]) if self._months is not None else ti[t].month

            # ① 土地利用参数更新
            if month == 1 and self.landuse_mod:
                self._update_landuse_params(year)

            # 每5年用已模拟序列更新一次生态基流。
            if month == 1 and t > 0 and t % 60 == 0:
                self.eco_baseflow_mod.update_eco_baseflow(
                    self.network, n, t)

            # ② 产流
            self._step_runoff(t, month)

            # ③ 汇流+配置
            node_flow = self._step_routing(t, year, month)

            # ④ 生态
            self._step_ecology(t, month, node_flow)

            # ⑤ 地下水
            self._step_groundwater(t)

            if progress_cb and t % 12 == 0:
                progress_cb(t, n, f"{year}年")

        elapsed = _time.time() - t0
        self._log(f"完成，耗时 {elapsed:.1f}s")
        return self._build_results()

    # ══════════════════════════════════════
    #  ① 土地利用更新
    # ══════════════════════════════════════


    def _init_audit(self, n: int):
        self.audit = {
            'natural_runoff_total': np.zeros(n),
            'gw_recharge_total': np.zeros(n),
            'channel_loss_total': np.zeros(n),
            'diversion_withdraw_total': np.zeros(n),
            'transfer_withdraw_total': np.zeros(n),
            'transfer_delivery_total': np.zeros(n),
            'transfer_delivery_to_demand': np.zeros(n),
            'transfer_delivery_to_network': np.zeros(n),
            'transfer_delivery_to_unresolved': np.zeros(n),
            'demand_total': np.zeros(n),
            'allocation_total': np.zeros(n),
            'shortage_total': np.zeros(n),
            'return_flow_total': np.zeros(n),
            'return_flow_unrouted_total': np.zeros(n),
            'consumption_total': np.zeros(n),
            'source_supply_local_sw': np.zeros(n),
            'source_supply_groundwater': np.zeros(n),
            'source_supply_unconventional': np.zeros(n),
            'source_supply_reservoir': np.zeros(n),
            'source_supply_lake': np.zeros(n),
            'source_supply_diversion': np.zeros(n),
            'source_supply_transfer': np.zeros(n),
            'source_supply_historical_closure': np.zeros(n),
            'groundwater_supply_total': np.zeros(n),
            'ocean_outflow_total': np.zeros(n),
            'water_rights_quota_total': np.zeros(n),
            'water_rights_use_total': np.zeros(n),
            'water_rights_capped_total': np.zeros(n),
            'water_rights_quota_by_key': defaultdict(lambda: np.zeros(n)),
            'water_rights_use_by_key': defaultdict(lambda: np.zeros(n)),
            'water_rights_capped_by_key': defaultdict(lambda: np.zeros(n)),
            'reservoir_transfer_withdraw_by_id': defaultdict(lambda: np.zeros(n)),
            'lake_transfer_withdraw_by_id': defaultdict(lambda: np.zeros(n)),
            'reservoir_initial_storage_by_id': {},
            'lake_initial_storage_by_id': {},
        }

    def _update_landuse_params(self, year: int):
        """每年1月更新产流参数（如有土地利用数据）"""
        for sb in self._nodes(NodeType.SUBBASIN):
            if not isinstance(sb, SubbasinNode):
                continue
            base = {
                'runoff_coeff': sb.runoff_coeff,
                'baseflow_index': sb.baseflow_index,
                'et_reduction': sb.et_reduction,
                'param_c': sb.param_c,
            }
            adj = self.landuse_mod.adjust_params_for_year(
                sb.unit_id, year, base)
            sb.runoff_coeff = adj['runoff_coeff']
            sb.baseflow_index = adj['baseflow_index']
            sb.et_reduction = adj['et_reduction']
            sb.param_c = adj['param_c']

    # ══════════════════════════════════════
    #  ② 产流
    # ══════════════════════════════════════

    def _step_runoff(self, t: int, month: int):
        method = self.config.runoff_method

        for sb in self._nodes(NodeType.SUBBASIN):
            if not isinstance(sb, SubbasinNode):
                continue

            # 优先使用预计算产流；缺失时回退到配置的产流方法。
            if sb.use_precomputed and sb.precomputed_runoff is not None:
                if t < len(sb.precomputed_runoff):
                    sb.total_runoff[t] = max(0, sb.precomputed_runoff[t])

                    if sb.precomputed_baseflow is not None and t < len(sb.precomputed_baseflow):
                        sb.baseflow[t] = max(0, sb.precomputed_baseflow[t])
                    else:
                        sb.baseflow[t] = sb.total_runoff[t] * 0.3  # 估算

                    sb.surface_runoff[t] = max(0, sb.total_runoff[t] - sb.baseflow[t])

                    if sb.precomputed_gw_recharge is not None and t < len(sb.precomputed_gw_recharge):
                        sb.gw_recharge[t] = max(0, sb.precomputed_gw_recharge[t])
                    else:
                        sb.gw_recharge[t] = sb.baseflow[t] * 0.8  # 估算

                    self.audit['natural_runoff_total'][t] += sb.total_runoff[t]
                    self.audit['gw_recharge_total'][t] += sb.gw_recharge[t]

                    continue

            # 预计算不可用时，使用当前配置的产流方法。
            P = sb.precip[t] if sb.precip is not None else 0.0
            PET = sb.pet[t] if sb.pet is not None else 0.0
            T = sb.temp_mean[t] if sb.temp_mean is not None else 15.0

            # 融雪
            eff_P = P
            if sb.id in self.snow_modules:
                sr = self.snow_modules[sb.id].step(P, T, month)
                eff_P = sr['effective_precip']

            # 产流
            if method == RunoffMethod.ABCD:
                r = abcd_step(eff_P, PET,
                              sb.param_a, sb.param_b,
                              sb.param_c, sb.param_d,
                              sb.soil_storage, sb.gw_storage,
                              sb.area)
                sb.soil_storage = r['S_soil']
                sb.gw_storage = r['S_gw']

            elif method == RunoffMethod.GR2M:
                r = gr2m_step(eff_P, PET,
                              sb.param_x1, sb.param_x2,
                              sb.soil_storage, sb.gw_storage,
                              sb.area)
                sb.soil_storage = r['S']
                sb.gw_storage = r['R']

            else:
                r = coefficient_step(eff_P, PET,
                                      sb.et_reduction,
                                      sb.runoff_coeff,
                                      sb.baseflow_index,
                                      sb.area)

            sb.total_runoff[t] = max(0, r['total_runoff'])
            sb.surface_runoff[t] = max(0, r['surface'])
            sb.baseflow[t] = max(0, r['baseflow'])
            sb.gw_recharge[t] = max(0, r.get('gw_recharge', 0))
            self.audit['natural_runoff_total'][t] += sb.total_runoff[t]
            self.audit['gw_recharge_total'][t] += sb.gw_recharge[t]


    # ══════════════════════════════════════
    #  ③ 河网汇流 + 配置
    # ══════════════════════════════════════

    def _step_routing(self, t, year, month):
        topo = self._routing_topology or self.network.topological_sort(include_management=True)
        node_flow = {}
        enable_return_flows = self._process_enabled('enable_return_flows')
        enable_reservoirs = self._process_enabled('enable_reservoirs')
        enable_lakes = self._process_enabled('enable_lakes')
        enable_diversions = self._process_enabled('enable_diversions')
        enable_transfers = self._process_enabled('enable_transfers')
        enable_demands = self._process_enabled('enable_demands')
        enable_facilities = enable_reservoirs or enable_lakes or enable_diversions
        pending_returns = (
            self._pending_return_flow
            if enable_return_flows else {}
        )
        self._pending_return_flow = defaultdict(float)

        # 调水 ...（不变）
        trans_delivery = defaultdict(float)
        trans_withdraw = defaultdict(float)

        for nid in topo:
            node = self.network.nodes[nid]
            incoming = self._calc_incoming(
                nid, node_flow, trans_delivery, pending_returns)

            # ── SubbasinNode ──
            if isinstance(node, SubbasinNode):
                local = 0.0
                if node.total_runoff is not None and t < len(node.total_runoff):
                    local = node.total_runoff[t]
                flow = incoming + local

                if (
                    enable_facilities and
                    (
                        nid in self._facility_graph_cache or
                        (enable_reservoirs and node.reservoir_ids) or
                        (enable_lakes and node.lake_ids)
                    )
                ):
                    flow, facility_flow = self._process_anchor_facilities(
                        nid, node.reservoir_ids, node.lake_ids, t, month, flow,
                        trans_withdraw, trans_delivery)
                    node_flow.update(facility_flow)

                eco_reserve, available = self.eco_baseflow_mod.split_flow(
                    node, flow, month, t)

                graph_nodes = self._graph_nodes_for_anchor(nid)
                remaining_diversions = [
                    div_id for div_id in node.diversion_ids
                    if div_id not in graph_nodes
                ]
                if remaining_diversions and enable_diversions:
                    available = self._process_diversions(
                        nid, t, available, diversion_ids=remaining_diversions)

                if enable_transfers:
                    available, sb_tw = self._process_transfers_at_source(
                        nid, node, t, year, month, available, trans_delivery)
                    trans_withdraw[nid] += sb_tw

                if node.eco_priority >= 3:
                    actual_use = flow - available - eco_reserve
                    self.eco_baseflow_mod.post_allocation_eco(
                        node, flow, actual_use, month, t)

                node_flow[nid] = eco_reserve + available

            # ── RiverChannelNode ──
            elif isinstance(node, RiverChannelNode):
                flow = incoming

                if (
                    enable_facilities and
                    (
                        nid in self._facility_graph_cache or
                        (enable_reservoirs and node.reservoir_ids) or
                        (enable_lakes and node.lake_ids)
                    )
                ):
                    flow, facility_flow = self._process_anchor_facilities(
                        nid, node.reservoir_ids, node.lake_ids, t, month, flow,
                        trans_withdraw, trans_delivery)
                    node_flow.update(facility_flow)

                eco_reserve, available = self.eco_baseflow_mod.split_flow(
                    node, flow, month, t)

                graph_nodes = self._graph_nodes_for_anchor(nid)
                remaining_diversions = [
                    div_id for div_id in node.diversion_ids
                    if div_id not in graph_nodes
                ]
                if remaining_diversions and enable_diversions:
                    available = self._process_diversions(
                        nid, t, available, diversion_ids=remaining_diversions)

                if enable_transfers:
                    available, ch_tw = self._process_transfers_at_source(
                        nid, node, t, year, month, available, trans_delivery)
                    trans_withdraw[nid] += ch_tw

                if node.eco_priority >= 3:
                    actual_use = flow - available - eco_reserve
                    self.eco_baseflow_mod.post_allocation_eco(
                        node, flow, actual_use, month, t)

                total_before_loss = eco_reserve + available
                total_out = self._route_channel(node, t, total_before_loss)

                if node.inflow_series is not None and t < len(node.inflow_series):
                    node.inflow_series[t] = incoming
                if node.outflow_series is not None and t < len(node.outflow_series):
                    node.outflow_series[t] = total_out

                node_flow[nid] = total_out

            # ── DemandNode ──
            elif isinstance(node, DemandNode):
                if enable_demands:
                    self._process_demand(node, t, month,
                                          node_flow, trans_delivery)
                node_flow[nid] = incoming

            # ── 其他 ──
            elif isinstance(node, (ReservoirNode, LakeNode, RiverDiversionNode)):
                node_flow[nid] = node_flow.get(nid, incoming)
            elif isinstance(node, (OceanNode, EcoControlNode, JunctionNode)):
                node_flow[nid] = incoming
            else:
                node_flow[nid] = incoming

        return node_flow


    def _calc_incoming(self, node_id: str,
                        node_flow: Dict[str, float],
                        trans_delivery: Optional[Dict[str, float]] = None,
        pending_returns: Optional[Dict[str, float]] = None) -> float:
        incoming = 0.0
        for from_node, loss_factor in self._incoming_flow_links_cache.get(node_id, ()):
            incoming += max(0.0, node_flow.get(from_node, 0.0) * loss_factor)
        for return_key, from_return_key, ratio in self._incoming_return_links_cache.get(node_id, ()):
            if return_key in node_flow:
                incoming += node_flow.get(return_key, 0.0)
            else:
                incoming += node_flow.get(from_return_key, 0.0) * ratio
        if pending_returns is not None:
            incoming += max(0.0, pending_returns.get(node_id, 0.0))

        if trans_delivery is not None:
            recv_node = self.network.nodes.get(node_id)
            if isinstance(recv_node, (SubbasinNode, RiverChannelNode, JunctionNode)):
                incoming += max(0.0, trans_delivery.get(node_id, 0.0))
        return incoming

    # ── 水库处理（通用） ──

    def _channel_residence_months(self, node: RiverChannelNode) -> float:
        if not self._process_enabled('enable_channel_routing'):
            return 0.0

        explicit = float(getattr(node, 'routing_residence_months', 0.0) or 0.0)
        if explicit > 0:
            return explicit

        base = float(getattr(self.config, 'channel_base_residence_months', 0.0) or 0.0)
        factor = float(getattr(self.config, 'channel_area_residence_factor', 0.0) or 0.0)
        area_scale = max(float(getattr(self.config, 'channel_area_scale', 1.0) or 1.0), 1.0)
        max_residence = max(float(getattr(self.config, 'channel_max_residence_months', 0.0) or 0.0), 0.0)
        area_term = np.sqrt(max(float(getattr(node, 'area', 0.0) or 0.0), 0.0) / area_scale)
        residence = base + factor * area_term
        return float(np.clip(residence, 0.0, max_residence))

    def _route_channel(self, node: RiverChannelNode, t: int, inflow: float) -> float:
        inflow = max(0.0, inflow)
        transmissive = max(0.0, inflow * (1.0 - node.loss_rate))
        self.audit['channel_loss_total'][t] += max(0.0, inflow - transmissive)

        residence = self._channel_residence_months(node)
        prev_storage = max(0.0, float(getattr(node, 'routing_storage', 0.0) or 0.0))
        if residence <= 1e-9:
            outflow = transmissive
            new_storage = 0.0
        else:
            pool = prev_storage + transmissive
            release_coeff = 1.0 / (1.0 + residence)
            outflow = max(0.0, pool * release_coeff)
            new_storage = max(0.0, pool - outflow)

        node.routing_storage = new_storage
        if node.routing_storage_ts is not None and t < len(node.routing_storage_ts):
            node.routing_storage_ts[t] = new_storage
        return outflow

    def _process_reservoirs(self, reservoir_ids, t, month,
                             inflow, trans_withdraw, trans_delivery=None):
        if not self._process_enabled('enable_reservoirs'):
            return inflow, {}
        sorted_ids = self._sort_reservoirs(reservoir_ids)
        if not sorted_ids:
            return inflow, {}
        current_flow = max(0.0, inflow)
        step_results = {}
        year = int(self._years[t]) if self._years is not None and t < len(self._years) else self.config.time.time_index[t].year
        infra_year = self._resolve_input_year("infrastructure", year)
        for rid in sorted_ids:
            res = self.network.nodes.get(rid)
            if not isinstance(res, ReservoirNode):
                continue
            if not res.is_online(year=infra_year):
                if res.inflow_ts is not None and t < len(res.inflow_ts):
                    res.inflow_ts[t] = current_flow
                if res.release_ts is not None and t < len(res.release_ts):
                    res.release_ts[t] = 0.0
                if res.spill_ts is not None and t < len(res.spill_ts):
                    res.spill_ts[t] = 0.0
                if res.storage_ts is not None and t < len(res.storage_ts):
                    res.storage_ts[t] = res.current_storage
                continue
            if self._process_enabled('enable_transfers'):
                _, planned_tw = self._process_transfers_at_source(
                    rid, res, t, year, month, current_flow,
                    trans_delivery if trans_delivery is not None else {})
                trans_withdraw[rid] += planned_tw
            demand = self._downstream_demand_for(rid, t)
            step = self.reservoir_mod.step(
                res, t, month, current_flow, demand, trans_withdraw.get(rid, 0.0))
            step_results[rid] = step
            current_flow = max(0.0, step.get('outflow', 0.0))
        return current_flow, step_results

    def _sort_reservoirs(self, reservoir_ids):
        items = []
        for rid in reservoir_ids:
            res = self.network.nodes.get(rid)
            if isinstance(res, ReservoirNode):
                items.append((res.order_in_channel, rid))
        items.sort()
        return [rid for _, rid in items]

    def _downstream_demand_for(self, node_id, t):
        series = self._downstream_demand_series_by_node.get(node_id)
        if series is not None and t < len(series):
            return float(series[t])
        return 0.0

    def _graph_nodes_for_anchor(self, anchor_node_id):
        return self._facility_graph_nodes_cache.get(anchor_node_id, set())

    def _process_facility_graph(self, anchor_node_id, t, month, inflow,
                                trans_withdraw, trans_delivery=None):
        graph_cache = self._facility_graph_cache.get(anchor_node_id)
        if graph_cache is None:
            return inflow, {}

        outgoing = graph_cache['outgoing']
        graph_nodes = graph_cache['graph_nodes']
        topological_order = graph_cache['topological_order']
        year = int(self._years[t]) if self._years is not None and t < len(self._years) else self.config.time.time_index[t].year
        infra_year = self._resolve_input_year("infrastructure", year)
        node_inflow = defaultdict(float)
        node_inflow[FACILITY_ENTRY] = max(0.0, inflow)
        facility_flow = {}

        if trans_delivery is not None:
            for facility_id in graph_nodes:
                delivered = max(0.0, trans_delivery.get(facility_id, 0.0))
                if delivered > 0:
                    node_inflow[facility_id] += delivered

        for node_id in topological_order:
            if node_id == FACILITY_ENTRY:
                current_out = node_inflow[FACILITY_ENTRY]
            elif node_id == FACILITY_OUTLET:
                continue
            else:
                current_in = max(0.0, node_inflow.get(node_id, 0.0))
                facility = self.network.nodes.get(node_id)
                if isinstance(facility, ReservoirNode):
                    if not self._process_enabled('enable_reservoirs'):
                        facility_flow[node_id] = current_in
                        current_out = current_in
                    elif not facility.is_online(year=infra_year):
                        facility_flow[node_id] = 0.0
                        current_out = current_in
                        if facility.inflow_ts is not None and t < len(facility.inflow_ts):
                            facility.inflow_ts[t] = current_in
                        if facility.release_ts is not None and t < len(facility.release_ts):
                            facility.release_ts[t] = 0.0
                        if facility.spill_ts is not None and t < len(facility.spill_ts):
                            facility.spill_ts[t] = 0.0
                        if facility.storage_ts is not None and t < len(facility.storage_ts):
                            facility.storage_ts[t] = facility.current_storage
                    else:
                        demand = self._downstream_demand_for(node_id, t)
                        if self._process_enabled('enable_transfers'):
                            _, planned_tw = self._process_transfers_at_source(
                                node_id, facility, t,
                                year, month,
                                current_in,
                                trans_delivery if trans_delivery is not None else {})
                            trans_withdraw[node_id] += planned_tw
                        tw = trans_withdraw.get(node_id, 0.0)
                        step = self.reservoir_mod.step(facility, t, month, current_in, demand, tw)
                        facility_flow[node_id] = max(0.0, step.get('release', 0.0))
                        current_out = max(0.0, step.get('outflow', 0.0))
                elif isinstance(facility, LakeNode):
                    if not self._process_enabled('enable_lakes'):
                        facility_flow[node_id] = current_in
                        current_out = current_in
                    else:
                        demand = self._downstream_demand_for(node_id, t)
                        if self._process_enabled('enable_transfers'):
                            _, planned_tw = self._process_transfers_at_source(
                                node_id, facility, t,
                                year, month,
                                current_in,
                                trans_delivery if trans_delivery is not None else {})
                            trans_withdraw[node_id] += planned_tw
                        step = self.lake_mod.step(
                            facility, t, month, current_in, demand,
                            trans_withdraw.get(node_id, 0.0))
                        facility_flow[node_id] = max(0.0, step.get('outflow', 0.0))
                        current_out = max(0.0, step.get('outflow', 0.0))
                elif isinstance(facility, RiverDiversionNode):
                    actual = 0.0
                    if self._process_enabled('enable_diversions'):
                        actual = self._compute_diversion_actual(facility, current_in, month)
                    if facility.actual_ts is not None and t < len(facility.actual_ts):
                        facility.actual_ts[t] = actual
                    facility_flow[node_id] = max(0.0, actual)
                    current_out = max(0.0, current_in - actual)
                else:
                    current_out = current_in

            for edge in outgoing.get(node_id, []):
                node_inflow[edge.to_node] += current_out * max(0.0, edge.split_ratio)

        return max(0.0, node_inflow.get(FACILITY_OUTLET, 0.0)), facility_flow

    def _process_anchor_facilities(self, anchor_node_id, reservoir_ids, lake_ids,
                                   t, month, inflow, trans_withdraw,
                                   trans_delivery=None):
        if not (
            self._process_enabled('enable_reservoirs') or
            self._process_enabled('enable_lakes') or
            self._process_enabled('enable_diversions')
        ):
            return inflow, {}
        graph_flow, facility_flow = self._process_facility_graph(
            anchor_node_id, t, month, inflow, trans_withdraw, trans_delivery)
        if facility_flow:
            return graph_flow, facility_flow

        flow = inflow
        direct_facility_flow = {}
        if reservoir_ids and self._process_enabled('enable_reservoirs'):
            flow, res_step = self._process_reservoirs(
                reservoir_ids, t, month, flow, trans_withdraw, trans_delivery)
            for rid, step in res_step.items():
                direct_facility_flow[rid] = max(0.0, step.get('release', 0.0))
        if lake_ids and self._process_enabled('enable_lakes'):
            flow = self._process_lakes(
                lake_ids, t, month, flow, trans_withdraw, trans_delivery)
        return flow, direct_facility_flow

    def _get_demand_subbasins(self, dm):
        if self.source_builder is not None:
            return self.source_builder._get_subbasin_nodes(dm)
        nodes = []
        unit_ids = list(getattr(dm, 'coupled_unit_ids', []) or [])
        for unit_id in unit_ids:
            sb = self.network.nodes.get(f'SB_{unit_id}')
            if isinstance(sb, SubbasinNode):
                nodes.append(sb)
        return nodes

    # ── 湖泊处理（通用） ──

    def _process_lakes(self, lake_ids, t, month, inflow,
                       trans_withdraw=None, trans_delivery=None):
        if not self._process_enabled('enable_lakes'):
            return inflow
        flow = inflow
        year = int(self._years[t]) if self._years is not None and t < len(self._years) else self.config.time.time_index[t].year
        for lk_id in lake_ids:
            lk = self.network.nodes.get(lk_id)
            if isinstance(lk, LakeNode):
                demand = self._downstream_demand_for(lk_id, t)
                planned_tw = 0.0
                if self._process_enabled('enable_transfers'):
                    _, planned_tw = self._process_transfers_at_source(
                        lk_id, lk, t, year, month, flow,
                        trans_delivery if trans_delivery is not None else {})
                if trans_withdraw is not None:
                    trans_withdraw[lk_id] += planned_tw
                    tw = trans_withdraw.get(lk_id, 0.0)
                else:
                    tw = planned_tw
                lr = self.lake_mod.step(lk, t, month, flow, demand, tw)
                flow = lr['outflow']
        return flow

    def _channel_control_min_flow(self, channel_node_id: str, month: int) -> float:
        if not channel_node_id:
            return 0.0
        nodes = self._control_nodes_by_channel.get(channel_node_id, [])
        if not nodes:
            return 0.0
        return max(float(node.get_min_flow(month)) for node in nodes)

    def _diversion_constraints(self, channel_node_id: str, diversion_ids, available_flow: float, month: int):
        eco_ratio = max(0.0, float(getattr(self.config, 'diversion_eco_reserve_ratio', 0.0) or 0.0))
        eco_min_flow = max(0.0, float(getattr(self.config, 'diversion_eco_reserve_min_flow', 0.0) or 0.0))
        downstream_ratio = max(0.0, float(getattr(self.config, 'diversion_downstream_min_ratio', 0.0) or 0.0))
        downstream_min_flow = max(0.0, float(getattr(self.config, 'diversion_downstream_min_flow', 0.0) or 0.0))

        for div_id in diversion_ids or []:
            div = self.network.nodes.get(div_id)
            if isinstance(div, RiverDiversionNode):
                eco_ratio = max(eco_ratio, float(getattr(div, 'eco_reserve_ratio', 0.0) or 0.0))
                eco_min_flow = max(eco_min_flow, float(getattr(div, 'eco_reserve_min_flow', 0.0) or 0.0))
                downstream_ratio = max(downstream_ratio, float(getattr(div, 'downstream_min_ratio', 0.0) or 0.0))
                downstream_min_flow = max(downstream_min_flow, float(getattr(div, 'downstream_min_flow', 0.0) or 0.0))

        eco_reserve = max(eco_min_flow, available_flow * eco_ratio)
        downstream_min = max(downstream_min_flow, available_flow * downstream_ratio)
        downstream_min = max(downstream_min, self._channel_control_min_flow(channel_node_id, month))
        return eco_reserve, downstream_min

    def _compute_diversion_actual(self, div_node: RiverDiversionNode, current_in: float, month: int) -> float:
        eco_reserve, downstream_min = self._diversion_constraints(
            div_node.channel_node_id, [div_node.id], current_in, month)
        allocable = max(0.0, current_in - eco_reserve - downstream_min)
        return min(max(0.0, current_in), max(0.0, div_node.max_capacity), allocable)

    # ── 引水处理（通用） ──

    def _process_diversions(self, node_id, t, available_flow, diversion_ids=None):
        if not self._process_enabled('enable_diversions'):
            return max(0, available_flow)
        if diversion_ids is None:
            diversion_ids = list(self.diversion_mod._by_channel.get(node_id, []))
        eco_reserve, downstream_min = self._diversion_constraints(
            node_id, diversion_ids, available_flow,
            month=int(self._months[t]) if self._months is not None and t < len(self._months) else self.config.time.time_index[t].month)
        if not diversion_ids:
            return max(0, available_flow)
        if node_id in self.diversion_mod._by_channel and set(diversion_ids) == set(self.diversion_mod._by_channel.get(node_id, [])):
            div_alloc = self.diversion_mod.allocate_at_channel(
                node_id, available_flow, eco_reserve, downstream_min, t)
        else:
            div_alloc = self.diversion_mod.allocate_for_diversions(
                diversion_ids, available_flow, eco_reserve, downstream_min, t)
        total_div = sum(div_alloc.values())
        self.audit['diversion_withdraw_total'][t] += max(0.0, total_div)
        return max(0, available_flow - total_div)

    def _process_transfers_at_source(self, source_id, source_node,
                                      t, year, month, available_flow,
                                      trans_delivery):
        """在源节点处处理调水，直接从当前可用流量中扣减。"""
        proj_ids = self.transfer_mod._by_source.get(source_id, [])
        if not proj_ids:
            return available_flow, 0.0
        if not self._process_enabled('enable_transfers'):
            for pid in proj_ids:
                proj = self.transfer_mod.projects.get(pid)
                if proj is not None:
                    self.transfer_mod._record_zero(proj, t)
            return available_flow, 0.0

        remaining = max(0.0, available_flow)
        total_withdraw = 0.0
        is_regulated_transfer_source = False
        projs = [
            self.transfer_mod.projects[pid]
            for pid in proj_ids
            if pid in self.transfer_mod.projects
        ]
        if projs:
            is_regulated_transfer_source = all(
                getattr(proj, "source_type", None) == SourceType.TRANSFER
                for proj in projs
            )
        apply_regulated_storage_constraint = bool(int(getattr(
            self.config, "enable_regulated_transfer_storage_constraint", 0) or 0))
        storage_constrained_regulated_source = (
            is_regulated_transfer_source
            and apply_regulated_storage_constraint
            and isinstance(source_node, (ReservoirNode, LakeNode))
        )
        is_storage_source = (
            isinstance(source_node, (ReservoirNode, LakeNode))
            and not is_regulated_transfer_source
        )
        projs.sort(key=lambda p: p.priority)
        infra_year = self._resolve_input_year("infrastructure", year)

        for proj in projs:
            if not proj.is_online(infra_year):
                self.transfer_mod._record_zero(proj, t)
                continue

            plan = float(proj.monthly_plan[month - 1])
            if not np.isfinite(plan):
                plan = 0.0
            if proj.planned_ts is not None and t < len(proj.planned_ts):
                proj.planned_ts[t] = plan
            if plan <= 0:
                self.transfer_mod._record_zero(proj, t)
                continue

            receivers = self.transfer_mod.normalized_receiving_nodes(proj)
            if not receivers:
                if proj.actual_ts is not None and t < len(proj.actual_ts):
                    proj.actual_ts[t] = 0.0
                    proj.delivered_ts[t] = 0.0
                    proj.shortage_ts[t] = plan
                continue

            transfer_service_mode = str(getattr(
                self.config, "transfer_service_mode", "constrained"
            ) or "constrained").strip().lower()
            if transfer_service_mode in {
                "planned_accounting",
                "plan_accounting",
                "planned_supply",
                "planned",
            }:
                actual = max(0.0, plan)
                delivered = actual * (1.0 - proj.loss_rate)
                self.audit['transfer_withdraw_total'][t] += actual
                self.audit['transfer_delivery_total'][t] += delivered
                if isinstance(source_node, ReservoirNode):
                    self.audit['reservoir_transfer_withdraw_by_id'][source_node.id][t] += actual
                elif isinstance(source_node, LakeNode):
                    self.audit['lake_transfer_withdraw_by_id'][source_node.id][t] += actual

                if proj.actual_ts is not None and t < len(proj.actual_ts):
                    proj.actual_ts[t] = actual
                    proj.delivered_ts[t] = delivered
                    proj.shortage_ts[t] = 0.0

                for recv_id, ratio in receivers.items():
                    amount = delivered * ratio
                    if trans_delivery is not None:
                        trans_delivery[recv_id] = trans_delivery.get(recv_id, 0.0) + amount
                    recv_node = self.network.nodes.get(recv_id)
                    if isinstance(recv_node, DemandNode):
                        self.audit['transfer_delivery_to_demand'][t] += amount
                    elif recv_node is not None:
                        self.audit['transfer_delivery_to_network'][t] += amount
                    else:
                        self.audit['transfer_delivery_to_unresolved'][t] += amount
                continue

            regulated_source = getattr(proj, "source_type", None) == SourceType.TRANSFER
            if regulated_source and storage_constrained_regulated_source:
                min_factor = float(getattr(
                    self.config,
                    "regulated_transfer_storage_min_factor",
                    0.65,
                ) or 0.65)
                min_factor = max(0.0, min(1.0, min_factor))
                target_ratio = float(getattr(
                    self.config,
                    "regulated_transfer_storage_target_ratio",
                    0.75,
                ) or 0.75)
                min_ratio = max(0.0, float(getattr(
                    proj,
                    "min_source_storage_ratio",
                    0.0,
                ) or 0.0))
                if isinstance(source_node, ReservoirNode):
                    sr = max(0.0, min(1.0, source_node.storage_ratio()))
                elif isinstance(source_node, LakeNode):
                    normal = max(1e-6, float(getattr(source_node, "normal_storage", 0.0) or 0.0))
                    eco_min = max(0.0, float(getattr(source_node, "eco_min_storage", 0.0) or 0.0))
                    sr = max(0.0, min(1.0, (
                        float(getattr(source_node, "current_storage", 0.0) or 0.0) - eco_min
                    ) / max(1e-6, normal - eco_min)))
                else:
                    sr = 1.0
                if sr >= target_ratio:
                    storage_factor = 1.0
                elif sr <= min_ratio:
                    storage_factor = min_factor
                else:
                    span = max(1e-6, target_ratio - min_ratio)
                    storage_factor = min_factor + (1.0 - min_factor) * (
                        (sr - min_ratio) / span
                    )
                source_limit = max(0.0, min(plan, proj.intake_capacity)) * storage_factor
            elif regulated_source:
                # Large inter-basin transfer projects are represented as managed
                # water-network boundaries. Their delivered amount is governed by
                # project plan, intake capacity, start year, receiving topology
                # and water-right rules, not by the simplified storage state of
                # the anchor reservoir used only for locating the project.
                source_limit = max(0.0, proj.intake_capacity)
            elif isinstance(source_node, ReservoirNode):
                if source_node.storage_ratio() < proj.min_source_storage_ratio:
                    source_limit = 0.0
                else:
                    source_limit = max(
                        0.0,
                        source_node.current_storage +
                        max(0.0, available_flow) -
                        source_node.dead_storage -
                        total_withdraw)
            elif isinstance(source_node, LakeNode):
                source_limit = max(
                    0.0,
                    source_node.current_storage +
                    max(0.0, available_flow) -
                    source_node.eco_min_storage -
                    total_withdraw)
            else:
                control_min_flow = self._channel_control_min_flow(source_id, month)
                source_limit = max(
                    0.0,
                    remaining - max(proj.min_source_flow, control_min_flow))

            rights_limit = float("inf")
            if self._is_water_right_surface_source(source_id):
                rights_limit = self._water_right_transfer_limit(
                    t,
                    year,
                    month,
                    receivers,
                    proj.loss_rate,
                )
            unconstrained_actual = max(0.0, min(plan, proj.intake_capacity, source_limit))
            actual = max(0.0, min(unconstrained_actual, rights_limit))
            if np.isfinite(rights_limit) and actual < unconstrained_actual:
                self.audit['water_rights_capped_total'][t] += unconstrained_actual - actual
            delivered = actual * (1.0 - proj.loss_rate)
            if not regulated_source:
                total_withdraw += actual
            if not is_storage_source and not regulated_source:
                remaining = max(0.0, remaining - actual)
            self.audit['transfer_withdraw_total'][t] += actual
            self.audit['transfer_delivery_total'][t] += delivered
            if isinstance(source_node, ReservoirNode):
                self.audit['reservoir_transfer_withdraw_by_id'][source_node.id][t] += actual
            elif isinstance(source_node, LakeNode):
                self.audit['lake_transfer_withdraw_by_id'][source_node.id][t] += actual

            if proj.actual_ts is not None and t < len(proj.actual_ts):
                proj.actual_ts[t] = actual
                proj.delivered_ts[t] = delivered
                proj.shortage_ts[t] = max(0.0, plan - actual)

            if self._is_water_right_surface_source(source_id):
                self._charge_transfer_water_rights(
                    t,
                    year,
                    month,
                    receivers,
                    actual,
                    proj.loss_rate,
                )

            for recv_id, ratio in receivers.items():
                amount = delivered * ratio
                if trans_delivery is not None:
                    trans_delivery[recv_id] = trans_delivery.get(recv_id, 0.0) + amount
                recv_node = self.network.nodes.get(recv_id)
                if isinstance(recv_node, DemandNode):
                    self.audit['transfer_delivery_to_demand'][t] += amount
                elif recv_node is not None:
                    self.audit['transfer_delivery_to_network'][t] += amount
                else:
                    self.audit['transfer_delivery_to_unresolved'][t] += amount

        return remaining, total_withdraw

    # ── 需水配置 ──

    def _apply_historical_supply_closure(
        self,
        dm: DemandNode,
        t: int,
        result: AllocationResult,
        source_map: dict,
    ) -> None:
        """Close historical actual water use without reporting scenario shortage.

        S2 uses observed historical water-use quantities. If the explicit
        process sources cannot fully explain that use, the residual is a model
        diagnostic rather than a simulated shortage. S3 leaves this switch off
        so shortages remain visible under current-demand stress tests.
        """
        if not self._process_enabled("enable_historical_supply_closure", default=False):
            return

        closure_source_id = f"{dm.id}_historical_closure"
        closure_added = 0.0
        for sector, shortage in list(result.shortages.items()):
            residual = max(0.0, float(shortage or 0.0))
            if residual <= 0:
                continue
            result.alloc_detail.setdefault(sector, {})
            result.alloc_detail[sector][closure_source_id] = (
                result.alloc_detail[sector].get(closure_source_id, 0.0) + residual
            )
            result.shortages[sector] = 0.0
            closure_added += residual

        if closure_added <= 0:
            return

        source_map[closure_source_id] = WaterSource(
            source_id=closure_source_id,
            source_name="historical_supply_closure",
            source_type="historical_closure",
            available=closure_added,
            priority=99,
            loss_rate=0.0,
        )
        result.total_supply += closure_added
        result.total_shortage = 0.0
        result.satisfaction_rate = 1.0 if result.total_demand > 0 else 1.0

    def _process_demand(self, dm, t, month, node_flow, trans_delivery):
        year = int(self._years[t]) if self._years is not None and t < len(self._years) else self.config.time.time_index[t].year
        sources = self.source_builder.build_sources(dm, t, node_flow, trans_delivery)
        self._cap_sources_by_water_rights(dm, sources, t, year, month)
        source_map = {src.source_id: src for src in sources}

        demands = {}
        for s in self._sector_values:
            arr = dm.demands.get(s)
            demands[s] = arr[t] if arr is not None and t < len(arr) else 0.0

        result = self.alloc_engine.allocate(demands, sources)
        self._charge_demand_water_rights(dm, t, year, month, result, source_map)
        self._apply_historical_supply_closure(dm, t, result, source_map)

        source_type_supply = defaultdict(float)
        source_withdrawals = defaultdict(float)
        for alloc_detail in result.alloc_detail.values():
            for source_id, delivered in alloc_detail.items():
                src = source_map.get(source_id)
                if src is not None:
                    source_type_supply[src.source_type] += delivered
                    gross = delivered / max(1e-6, 1.0 - src.loss_rate)
                    source_withdrawals[source_id] += gross

        sector_return_total = 0.0
        sector_consumption_total = 0.0
        enable_return_flows = self._process_enabled('enable_return_flows')
        for s in self._sector_values:
            demand_val = demands.get(s, 0.0)
            alloc = sum(result.alloc_detail.get(s, {}).values())
            shortage_val = result.shortages.get(s, 0)
            if enable_return_flows:
                return_ratio = max(0.0, min(1.0, float(dm.return_ratios.get(s, 0.5))))
            else:
                return_ratio = 0.0
            return_val = alloc * return_ratio
            consumption_val = max(0.0, alloc - return_val)
            if s in dm.allocation and t < len(dm.allocation[s]):
                dm.allocation[s][t] = alloc
            if s in dm.shortage and t < len(dm.shortage[s]):
                dm.shortage[s][t] = shortage_val
            if s in dm.return_flow_by_sector and t < len(dm.return_flow_by_sector[s]):
                dm.return_flow_by_sector[s][t] = return_val
            if s in dm.consumption and t < len(dm.consumption[s]):
                dm.consumption[s][t] = consumption_val
            self.audit['demand_total'][t] += demand_val
            self.audit['allocation_total'][t] += alloc
            self.audit['shortage_total'][t] += shortage_val
            sector_return_total += return_val
            sector_consumption_total += consumption_val
        self.audit['consumption_total'][t] += sector_consumption_total

        for src_type, series in dm.source_supply.items():
            if t < len(series):
                value = source_type_supply.get(src_type, 0.0)
                series[t] = value
                if src_type == 'local_sw':
                    self.audit['source_supply_local_sw'][t] += value
                elif src_type == 'groundwater':
                    self.audit['source_supply_groundwater'][t] += value
                    self.audit['groundwater_supply_total'][t] += value
                    if self.source_builder is not None:
                        self.source_builder.charge_groundwater_balance(dm, t, value)
                elif src_type == 'unconventional':
                    self.audit['source_supply_unconventional'][t] += value
                elif src_type == 'reservoir':
                    self.audit['source_supply_reservoir'][t] += value
                elif src_type == 'lake':
                    self.audit['source_supply_lake'][t] += value
                elif src_type == 'diversion':
                    self.audit['source_supply_diversion'][t] += value
                elif src_type == 'transfer':
                    self.audit['source_supply_transfer'][t] += value
                elif src_type == 'historical_closure':
                    self.audit['source_supply_historical_closure'][t] += value

        for source_id, gross in source_withdrawals.items():
            physical_source_id = self._physical_source_id(source_id)
            if physical_source_id in node_flow:
                node_flow[physical_source_id] = max(
                    0.0, node_flow.get(physical_source_id, 0.0) - gross)
            src_node = self.network.nodes.get(physical_source_id)
            if isinstance(src_node, ReservoirNode):
                ch_id = getattr(src_node, 'channel_node_id', '')
                if ch_id in node_flow:
                    node_flow[ch_id] = max(
                        0.0, node_flow.get(ch_id, 0.0) - gross)
            elif isinstance(src_node, LakeNode):
                ch_id = getattr(src_node, 'channel_node_id', '')
                if ch_id in node_flow:
                    node_flow[ch_id] = max(
                        0.0, node_flow.get(ch_id, 0.0) - gross)

        # Return flow is generated by sectoral allocation and routed by unit-weighted
        # return links as next-month lateral inflow. This keeps river topology acyclic.
        ret = sector_return_total
        if ret > 0 and self._process_enabled('enable_return_flows'):
            self.audit['return_flow_total'][t] += ret
            return_links = [
                link for link in self._downstream_links_cache.get(dm.id, ())
                if link.link_type == LinkType.RETURN_FLOW and link.is_active
            ]
            total_ratio = sum(
                max(0.0, float(getattr(link, 'return_ratio', 1.0) or 0.0))
                for link in return_links
            )
            if total_ratio > 0:
                for link in return_links:
                    ratio = max(0.0, float(getattr(link, 'return_ratio', 1.0) or 0.0))
                    amount = ret * ratio / total_ratio
                    self._pending_return_flow[link.to_node] += amount
            else:
                self.audit['return_flow_unrouted_total'][t] += ret

    def _step_ecology(self, t, month, node_flow):
        for node in self._nodes(NodeType.OCEAN):
            if isinstance(node, OceanNode):
                flow = node_flow.get(node.id, 0)
                self.audit['ocean_outflow_total'][t] += flow
                self.ecology_mod.check(node, flow, month, t)
        for node in self._nodes(NodeType.ECO_CONTROL):
            if isinstance(node, EcoControlNode):
                flow = node_flow.get(node.channel_node_id, 0)
                self.ecology_mod.check(node, flow, month, t)

    # ══════════════════════════════════════
    #  ⑤ 地下水
    # ══════════════════════════════════════

    def _step_groundwater(self, t):
        if (not self._process_enabled('enable_gw_overexploit') or
                not self._process_enabled('enable_demands') or
                not self._process_enabled('enable_groundwater_allocation')):
            return
        for dm in self._nodes(NodeType.DEMAND):
            if not isinstance(dm, DemandNode):
                continue
            if 'groundwater' in dm.source_supply and t < len(dm.source_supply['groundwater']):
                gw_exploit = max(0.0, dm.source_supply['groundwater'][t])
            else:
                total_alloc = sum(
                    dm.allocation[s][t] for s in dm.allocation
                    if t < len(dm.allocation[s]))
                gw_exploit = max(0, min(
                    total_alloc * 0.3,
                    dm.gw_exploitable_annual / 12))
            gw_recharge = 0.0
            for sb in self._get_demand_subbasins(dm):
                if sb.gw_recharge is not None and t < len(sb.gw_recharge):
                    gw_recharge += sb.gw_recharge[t]
            dm.gw_cumulative_over = self.gw_mod.update_overexploit(
                gw_exploit, gw_recharge, dm.gw_cumulative_over)

    # ══════════════════════════════════════
    #  结果构建
    # ══════════════════════════════════════

    def _build_audit_results(self):
        if not self.audit:
            return {
                'audit_monthly': pd.DataFrame(),
                'audit_summary': pd.DataFrame(),
                'reservoir_balance_audit': pd.DataFrame(),
                'lake_balance_audit': pd.DataFrame(),
            }

        n = self.config.time.n_steps
        idx = self.config.time.time_index
        monthly = pd.DataFrame({
            'date': idx.strftime('%Y-%m'),
            'natural_runoff_total': self.audit['natural_runoff_total'],
            'gw_recharge_total': self.audit['gw_recharge_total'],
            'channel_loss_total': self.audit['channel_loss_total'],
            'diversion_withdraw_total': self.audit['diversion_withdraw_total'],
            'transfer_withdraw_total': self.audit['transfer_withdraw_total'],
            'transfer_delivery_total': self.audit['transfer_delivery_total'],
            'transfer_delivery_to_demand': self.audit['transfer_delivery_to_demand'],
            'transfer_delivery_to_network': self.audit['transfer_delivery_to_network'],
            'transfer_delivery_to_unresolved': self.audit['transfer_delivery_to_unresolved'],
            'demand_total': self.audit['demand_total'],
            'allocation_total': self.audit['allocation_total'],
            'shortage_total': self.audit['shortage_total'],
            'return_flow_total': self.audit['return_flow_total'],
            'return_flow_unrouted_total': self.audit['return_flow_unrouted_total'],
            'consumption_total': self.audit['consumption_total'],
            'source_supply_local_sw': self.audit['source_supply_local_sw'],
            'source_supply_groundwater': self.audit['source_supply_groundwater'],
            'source_supply_unconventional': self.audit['source_supply_unconventional'],
            'source_supply_reservoir': self.audit['source_supply_reservoir'],
            'source_supply_lake': self.audit['source_supply_lake'],
            'source_supply_diversion': self.audit['source_supply_diversion'],
            'source_supply_transfer': self.audit['source_supply_transfer'],
            'source_supply_historical_closure': self.audit['source_supply_historical_closure'],
            'groundwater_supply_total': self.audit['groundwater_supply_total'],
            'ocean_outflow_total': self.audit['ocean_outflow_total'],
            'water_rights_quota_total': self.audit['water_rights_quota_total'],
            'water_rights_use_total': self.audit['water_rights_use_total'],
            'water_rights_capped_total': self.audit['water_rights_capped_total'],
        })
        monthly['allocation_balance_error'] = monthly['allocation_total'] - (
            monthly['source_supply_local_sw'] +
            monthly['source_supply_groundwater'] +
            monthly['source_supply_unconventional'] +
            monthly['source_supply_reservoir'] +
            monthly['source_supply_lake'] +
            monthly['source_supply_diversion'] +
            monthly['source_supply_transfer'] +
            monthly['source_supply_historical_closure']
        )
        monthly['demand_balance_error'] = monthly['demand_total'] - (
            monthly['allocation_total'] + monthly['shortage_total']
        )
        monthly['consumption_balance_error'] = monthly['allocation_total'] - (
            monthly['return_flow_total'] + monthly['consumption_total']
        )
        monthly['transfer_balance_error'] = monthly['transfer_delivery_total'] - (
            monthly['transfer_delivery_to_demand'] +
            monthly['transfer_delivery_to_network'] +
            monthly['transfer_delivery_to_unresolved']
        )
        monthly['unused_transfer_delivery_to_demand'] = (
            monthly['transfer_delivery_to_demand'] -
            monthly['source_supply_transfer']
        )

        summary_rows = []
        for col in [
            'natural_runoff_total', 'gw_recharge_total', 'channel_loss_total',
            'diversion_withdraw_total', 'transfer_withdraw_total', 'transfer_delivery_total',
            'transfer_delivery_to_demand', 'transfer_delivery_to_network',
            'transfer_delivery_to_unresolved',
            'demand_total', 'allocation_total', 'shortage_total',
            'return_flow_total', 'return_flow_unrouted_total', 'consumption_total',
            'source_supply_local_sw', 'source_supply_groundwater', 'source_supply_reservoir',
            'source_supply_unconventional', 'source_supply_lake', 'source_supply_diversion',
            'source_supply_transfer', 'source_supply_historical_closure',
            'groundwater_supply_total',
            'ocean_outflow_total',
            'water_rights_quota_total', 'water_rights_use_total',
            'water_rights_capped_total',
            'allocation_balance_error', 'demand_balance_error',
            'consumption_balance_error', 'transfer_balance_error',
            'unused_transfer_delivery_to_demand'
        ]:
            series = pd.to_numeric(monthly[col], errors='coerce').fillna(0.0)
            summary_rows.append({
                'item': col,
                'annual_mean': round(float(series.mean()) * 12, 3),
                'period_sum': round(float(series.sum()), 3),
                'max_month': round(float(series.max()), 3),
                'min_month': round(float(series.min()), 3),
            })

        reservoir_rows = []
        for res in self._nodes(NodeType.RESERVOIR):
            if not isinstance(res, ReservoirNode) or res.storage_ts is None or res.inflow_ts is None:
                continue
            transfer_w = self.audit['reservoir_transfer_withdraw_by_id'].get(res.id, np.zeros(n))
            init_storage = float(self.audit['reservoir_initial_storage_by_id'].get(res.id, res.initial_storage))
            prev_storage = np.concatenate(([init_storage], np.asarray(res.storage_ts[:-1], dtype=float)))
            inflow = np.asarray(res.inflow_ts, dtype=float)
            release = np.asarray(res.release_ts if res.release_ts is not None else np.zeros(n), dtype=float)
            spill = np.asarray(res.spill_ts if res.spill_ts is not None else np.zeros(n), dtype=float)
            storage = np.asarray(res.storage_ts, dtype=float)
            evap = np.asarray(getattr(res, 'evap_loss_ts', np.zeros(n)), dtype=float)
            seepage = np.asarray(getattr(res, 'seepage_loss_ts', np.zeros(n)), dtype=float)
            loss = evap + seepage
            balance_error = prev_storage + inflow - release - spill - transfer_w - loss - storage
            online_flags = np.array([
                1 if res.is_online(self._resolve_input_year("infrastructure", ts.year)) else 0
                for ts in idx
            ], dtype=int)
            inactive_bypass = np.where(online_flags == 0, np.maximum(0.0, inflow - storage), 0.0)
            reservoir_rows.append({
                'reservoir_id': res.id,
                'name': res.name,
                'status': res.status,
                'online_year': res.online_year,
                'scenario_group': res.scenario_group,
                'mean_inflow_annual': round(float(inflow.mean()) * 12, 3),
                'mean_release_annual': round(float(release.mean()) * 12, 3),
                'mean_spill_annual': round(float(spill.mean()) * 12, 3),
                'mean_transfer_withdraw_annual': round(float(np.mean(transfer_w)) * 12, 3),
                'mean_evap_seepage_annual': round(float(np.mean(loss)) * 12, 3),
                'max_abs_balance_error': round(float(np.max(np.abs(balance_error))), 6),
                'offline_months': int(np.sum(online_flags == 0)),
                'inactive_bypass_annual': round(float(np.mean(inactive_bypass)) * 12, 3),
                'balance_flag': 'CHECK' if float(np.max(np.abs(balance_error))) > 1e-3 else 'OK',
            })

        lake_rows = []
        for lake in self._nodes(NodeType.LAKE):
            if not isinstance(lake, LakeNode) or lake.storage_ts is None or lake.inflow_ts is None:
                continue
            transfer_w = self.audit['lake_transfer_withdraw_by_id'].get(lake.id, np.zeros(n))
            init_storage = float(self.audit['lake_initial_storage_by_id'].get(lake.id, lake.current_storage))
            prev_storage = np.concatenate(([init_storage], np.asarray(lake.storage_ts[:-1], dtype=float)))
            inflow = np.asarray(lake.inflow_ts, dtype=float)
            outflow = np.asarray(lake.outflow_ts if lake.outflow_ts is not None else np.zeros(n), dtype=float)
            storage = np.asarray(lake.storage_ts, dtype=float)
            evap = np.asarray(getattr(lake, 'evap_loss_ts', np.zeros(n)), dtype=float)
            balance_error = prev_storage + inflow - outflow - transfer_w - evap - storage
            lake_rows.append({
                'lake_id': lake.id,
                'name': lake.name,
                'mean_inflow_annual': round(float(inflow.mean()) * 12, 3),
                'mean_outflow_annual': round(float(outflow.mean()) * 12, 3),
                'mean_transfer_withdraw_annual': round(float(np.mean(transfer_w)) * 12, 3),
                'mean_evap_annual': round(float(np.mean(evap)) * 12, 3),
                'max_abs_balance_error': round(float(np.max(np.abs(balance_error))), 6),
                'balance_flag': 'CHECK' if float(np.max(np.abs(balance_error))) > 1e-3 else 'OK',
            })

        water_right_rows = []
        water_right_annual_rows = []
        water_right_monthly_rows = []
        scheme = self._water_rights_scheme
        quota_by_key = self.audit.get('water_rights_quota_by_key', {})
        use_by_key = self.audit.get('water_rights_use_by_key', {})
        capped_by_key = self.audit.get('water_rights_capped_by_key', {})
        quota_keys = sorted(
            set(getattr(scheme, 'annual_quota', {}) or {})
            | set(quota_by_key)
            | set(use_by_key)
            | set(capped_by_key)
        )
        for quota_key in quota_keys:
            quota = np.asarray(quota_by_key.get(quota_key, np.zeros(n)), dtype=float)
            use = np.asarray(use_by_key.get(quota_key, np.zeros(n)), dtype=float)
            capped = np.asarray(capped_by_key.get(quota_key, np.zeros(n)), dtype=float)
            province_names = []
            if scheme is not None:
                province_names = [
                    province for province, key in scheme.province_quota_key.items()
                    if key == quota_key
                ]
            annual_quota = float(getattr(scheme, 'annual_quota', {}).get(quota_key, 0.0)) if scheme is not None else 0.0
            for year in sorted(set(idx.year)):
                mask = idx.year == year
                quota_year = float(np.sum(quota[mask]))
                use_year = float(np.sum(use[mask]))
                capped_year = float(np.sum(capped[mask]))
                if quota_year <= 0 and use_year <= 0 and capped_year <= 0:
                    continue
                water_right_annual_rows.append({
                    'year': int(year),
                    'quota_key': quota_key,
                    'provinces': ';'.join(sorted(province_names)),
                    'annual_quota_wan_m3': round(annual_quota, 3),
                    'quota_wan_m3': round(quota_year, 3),
                    'use_wan_m3': round(use_year, 3),
                    'capped_wan_m3': round(capped_year, 3),
                    'remaining_wan_m3': round(max(0.0, quota_year - use_year), 3),
                    'use_to_quota_ratio': round(use_year / quota_year, 4) if quota_year > 0 else 0.0,
                })
            for i, date_text in enumerate(idx.strftime('%Y-%m')):
                if quota[i] <= 0 and use[i] <= 0 and capped[i] <= 0:
                    continue
                water_right_monthly_rows.append({
                    'date': date_text,
                    'year': int(idx[i].year),
                    'month': int(idx[i].month),
                    'quota_key': quota_key,
                    'provinces': ';'.join(sorted(province_names)),
                    'quota_wan_m3': round(float(quota[i]), 3),
                    'use_wan_m3': round(float(use[i]), 3),
                    'capped_wan_m3': round(float(capped[i]), 3),
                    'remaining_wan_m3': round(max(0.0, float(quota[i] - use[i])), 3),
                })
            water_right_rows.append({
                'quota_key': quota_key,
                'provinces': ';'.join(sorted(province_names)),
                'annual_quota_wan_m3': round(annual_quota, 3),
                'mean_monthly_quota_wan_m3': round(float(np.mean(quota)), 3) if len(quota) else 0.0,
                'period_quota_wan_m3': round(float(np.sum(quota)), 3),
                'period_use_wan_m3': round(float(np.sum(use)), 3),
                'period_capped_wan_m3': round(float(np.sum(capped)), 3),
                'max_month_use_wan_m3': round(float(np.max(use)), 3) if len(use) else 0.0,
                'max_month_capped_wan_m3': round(float(np.max(capped)), 3) if len(capped) else 0.0,
            })

        return {
            'audit_monthly': monthly,
            'audit_summary': pd.DataFrame(summary_rows),
            'reservoir_balance_audit': pd.DataFrame(reservoir_rows),
            'lake_balance_audit': pd.DataFrame(lake_rows),
            'water_rights_summary': pd.DataFrame(water_right_rows),
            'water_rights_annual': pd.DataFrame(water_right_annual_rows),
            'water_rights_monthly': pd.DataFrame(water_right_monthly_rows),
        }

    def _build_results(self) -> dict:
        if self.results_builder is None:
            self.results_builder = ResultsBuilder(
                self.config, self.network, self.transfer_mod)
        results = self.results_builder.build()
        results.update(self._build_audit_results())
        self.results = results
        return results

    def _load_export_sidecar_tables(self, output_dir: Path) -> dict:
        """Load optional run-adjacent audit tables for workbook export."""
        filenames = {
            "demand_input_validation": "demand_2024_extension_validation.csv",
            "demand_input_issues": "demand_2024_extension_issue_summary.csv",
            "demand_input_status": "demand_2024_extension_source_status.csv",
            "demand_aux_city_supply": "demand_2024_extension_auxiliary_city_supply.csv",
        }
        tables = {}
        search_dirs = []
        if output_dir:
            search_dirs.append(output_dir)
        search_dirs.append(Path("output"))

        seen_dirs = set()
        unique_dirs = []
        for directory in search_dirs:
            key = directory.resolve() if directory.exists() else directory
            if key in seen_dirs:
                continue
            seen_dirs.add(key)
            unique_dirs.append(directory)

        for table_name, filename in filenames.items():
            for directory in unique_dirs:
                path = directory / filename
                if not path.exists():
                    continue
                try:
                    table = pd.read_csv(path, encoding="utf-8-sig")
                except Exception:
                    continue
                if not table.empty:
                    tables[table_name] = table
                break
        return tables

    def get_demand_timeseries(self, node_id):
        node = self.network.nodes.get(node_id)
        if not isinstance(node, DemandNode):
            return None
        ti = self.config.time.time_index
        data = {}
        for s in [s.value for s in Sector]:
            if s in node.demands:
                data[f'{s}_需水'] = node.demands[s]
            if s in node.allocation:
                data[f'{s}_供水'] = node.allocation[s]
            if s in node.shortage:
                data[f'{s}_缺水'] = node.shortage[s]
        if not data:
            return None
        n = min(len(v) for v in data.values())
        return pd.DataFrame(data, index=ti[:n])

    def get_unit_summary(self):
        if self.results:
            return self.results.get('summary_unit', self.results.get('summary'))
        return None

    def get_city_summary(self):
        if self.results:
            return self.results.get('summary_city_total')
        return None

    def get_basin_summary(self):
        if self.results:
            return self.results.get('summary_basin_total')
        return None

    def get_transfer_summary(self):
        if self.results:
            return self.results.get('transfer_summary')
        return None

    def get_source_mix_summary(self):
        if self.results:
            return self.results.get('source_mix_city_total')
        return None

    def get_reservoir_timeseries(self, node_id):
        node = self.network.nodes.get(node_id)
        if not isinstance(node, ReservoirNode):
            return None
        ti = self.config.time.time_index
        data = {}
        if node.inflow_ts is not None: data['入库'] = node.inflow_ts
        if node.release_ts is not None: data['下泄'] = node.release_ts
        if node.spill_ts is not None: data['弃水'] = node.spill_ts
        if node.storage_ts is not None: data['蓄量'] = node.storage_ts
        if not data:
            return None
        n = min(len(v) for v in data.values())
        return pd.DataFrame(data, index=ti[:n])

    def get_subbasin_timeseries(self, node_id):
        node = self.network.nodes.get(node_id)
        if not isinstance(node, SubbasinNode):
            return None
        ti = self.config.time.time_index
        data = {}
        if node.precip is not None: data['降雨mm'] = node.precip
        if node.pet is not None: data['蒸散发mm'] = node.pet
        if node.total_runoff is not None: data['总径流万m³'] = node.total_runoff
        if node.surface_runoff is not None: data['地表径流万m³'] = node.surface_runoff
        if node.baseflow is not None: data['基流万m³'] = node.baseflow
        if not data:
            return None
        n = min(len(v) for v in data.values())
        return pd.DataFrame(data, index=ti[:n])

    def get_channel_timeseries(self, node_id):
        node = self.network.nodes.get(node_id)
        if not isinstance(node, RiverChannelNode):
            return None
        ti = self.config.time.time_index
        data = {}
        if node.inflow_series is not None: data['入流'] = node.inflow_series
        if node.outflow_series is not None: data['出流'] = node.outflow_series
        if getattr(node, 'routing_storage_ts', None) is not None:
            data['河道调蓄'] = node.routing_storage_ts
        if not data:
            return None
        n = min(len(v) for v in data.values())
        return pd.DataFrame(data, index=ti[:n])

    # ══════════════════════════════════════
    #  导出
    # ══════════════════════════════════════

    def export_results(self, filepath: str):
        if not self.results:
            print("no results to export")
            return

        table_keys = [
            'summary_unit', 'summary_city', 'summary_basin',
            'summary_annual', 'summary_city_annual', 'summary_basin_annual',
            'summary_city_total', 'summary_basin_total',
            'source_mix_summary', 'source_mix_city_total',
            'source_mix_unit_total', 'source_mix_annual', 'source_mix_city_annual',
            'reservoir_summary', 'transfer_summary', 'ecology_summary',
            'eco_baseflow_summary',
            'node_topology_summary', 'link_topology_summary',
            'supply_topology_detail', 'return_flow_topology_detail',
            'groundwater_topology_summary',
            'audit_summary', 'audit_monthly',
            'water_rights_summary', 'water_rights_annual', 'water_rights_monthly',
            'reservoir_balance_audit', 'lake_balance_audit',
        ]
        sidecar_tables = self._load_export_sidecar_tables(Path(filepath).parent)

        excel_engine = None
        for engine_name, module_name in [('openpyxl', 'openpyxl'),
                                         ('xlsxwriter', 'xlsxwriter')]:
            try:
                importlib.import_module(module_name)
                excel_engine = engine_name
                break
            except Exception:
                continue

        if excel_engine is not None:
            with pd.ExcelWriter(filepath, engine=excel_engine) as writer:
                for key in table_keys:
                    if key in self.results and len(self.results[key]) > 0:
                        self.results[key].to_excel(
                            writer, sheet_name=key[:31], index=False)
                for key, table in sidecar_tables.items():
                    table.to_excel(writer, sheet_name=key[:31], index=False)

                count = 0
                for dm in self.network.get_nodes(NodeType.DEMAND):
                    if count >= 30:
                        break
                    if isinstance(dm, DemandNode):
                        df = self.get_demand_timeseries(dm.id)
                        if df is not None and len(df) > 0:
                            name = (getattr(dm, 'city_code', '') or getattr(dm, 'unit_id', '') or dm.id)[:24]
                            df.to_excel(writer, sheet_name=f"demand_{name}"[:31])
                            count += 1

                for res in self.network.get_nodes(NodeType.RESERVOIR):
                    if isinstance(res, ReservoirNode):
                        df = self.get_reservoir_timeseries(res.id)
                        if df is not None and len(df) > 0:
                            name = (res.name or res.id)[:20]
                            df.to_excel(writer, sheet_name=f"reservoir_{name}"[:31])

            print(f"exported {filepath}")
            return

        base, _ = os.path.splitext(filepath)
        out_dir = f"{base}_csv"
        os.makedirs(out_dir, exist_ok=True)

        for key in table_keys:
            if key in self.results and len(self.results[key]) > 0:
                self.results[key].to_csv(
                    os.path.join(out_dir, f"{key}.csv"),
                    index=False,
                    encoding='utf-8-sig',
                )
        for key, table in sidecar_tables.items():
            table.to_csv(
                os.path.join(out_dir, f"{key}.csv"),
                index=False,
                encoding='utf-8-sig',
            )

        count = 0
        for dm in self.network.get_nodes(NodeType.DEMAND):
            if count >= 30:
                break
            if isinstance(dm, DemandNode):
                df = self.get_demand_timeseries(dm.id)
                if df is not None and len(df) > 0:
                    name = (getattr(dm, 'city_code', '') or getattr(dm, 'unit_id', '') or dm.id)[:40]
                    df.to_csv(
                        os.path.join(out_dir, f"demand_{name}.csv"),
                        encoding='utf-8-sig',
                    )
                    count += 1

        for res in self.network.get_nodes(NodeType.RESERVOIR):
            if isinstance(res, ReservoirNode):
                df = self.get_reservoir_timeseries(res.id)
                if df is not None and len(df) > 0:
                    name = (res.name or res.id)[:40]
                    df.to_csv(
                        os.path.join(out_dir, f"reservoir_{name}.csv"),
                        encoding='utf-8-sig',
                    )

        print(f"exported {out_dir}")

    def _log(self, msg):
        self.log.append(msg)
        print(f"[WRAM] {msg}")
