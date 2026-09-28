import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from collections import defaultdict

from config import SourceType
from core.nodes import (
    ReservoirNode,
    LakeNode,
    SubbasinNode,
    RiverChannelNode,
    RiverDiversionNode,
    DemandNode,
)


@dataclass
class TransferProject:
    id: str = ""
    name: str = ""
    status: str = "built"
    scenario_group: str = "all"
    source_node_id: str = ""
    source_type: SourceType = SourceType.RIVER
    intake_capacity: float = 0.0
    receiving_nodes: Dict[str, float] = field(default_factory=dict)
    annual_plan: float = 0.0
    monthly_plan: np.ndarray = field(default_factory=lambda: np.zeros(12))
    loss_rate: float = 0.05
    priority: int = 1
    start_year: int = 1900
    end_year: Optional[int] = None
    min_source_storage_ratio: float = 0.3
    min_source_flow: float = 0.0
    dynamic_plan: bool = True
    plan_mode: str = "specified"
    unit_cost: float = 0.0

    planned_ts: Optional[np.ndarray] = None
    actual_ts: Optional[np.ndarray] = None
    delivered_ts: Optional[np.ndarray] = None
    shortage_ts: Optional[np.ndarray] = None

    def is_online(self, year: int) -> bool:
        if year < int(self.start_year or 1900):
            return False
        if self.end_year and year > int(self.end_year):
            return False
        return True


class TransferModule:
    def __init__(self):
        self.projects: Dict[str, TransferProject] = {}
        self._by_source: Dict[str, List[str]] = defaultdict(list)

    def add(self, proj: TransferProject):
        self.projects[proj.id] = proj
        if proj.source_node_id:
            self._by_source[proj.source_node_id].append(proj.id)

    def init_series(self, n_steps: int):
        for proj in self.projects.values():
            proj.planned_ts = np.zeros(n_steps)
            proj.actual_ts = np.zeros(n_steps)
            proj.delivered_ts = np.zeros(n_steps)
            proj.shortage_ts = np.zeros(n_steps)

    def normalized_receiving_nodes(self, proj: TransferProject) -> Dict[str, float]:
        weights = {
            str(node_id): max(0.0, float(ratio))
            for node_id, ratio in proj.receiving_nodes.items()
        }
        total = sum(weights.values())
        if total <= 0:
            return {}
        return {node_id: ratio / total for node_id, ratio in weights.items()}

    def prepare_monthly_plans(self, network, config=None):
        for proj in self.projects.values():
            mode = self._resolve_plan_mode(proj, config)
            if mode == "specified":
                continue
            proj.monthly_plan = self._generate_monthly_plan(
                proj, network, config=config, mode=mode
            )

    def _resolve_plan_mode(self, proj: TransferProject, config=None) -> str:
        mode = str(getattr(proj, "plan_mode", "") or "").strip().lower()
        if not mode:
            mode = "specified"
        if mode == "specified":
            return mode
        if mode == "auto":
            return self._infer_auto_mode(proj)
        if mode in {"uniform", "demand_weighted", "flow_weighted", "hybrid"}:
            return mode
        return str(
            getattr(config, "transfer_plan_mode_default", "uniform")
        ).strip().lower()

    def _infer_auto_mode(self, proj: TransferProject) -> str:
        has_demand = any(str(node_id).startswith("DM_") for node_id in proj.receiving_nodes)
        has_non_demand = any(
            str(node_id).startswith(("CH_", "SB_", "RES_", "LK_", "DIV_"))
            for node_id in proj.receiving_nodes
        )
        if has_demand and has_non_demand:
            return "hybrid"
        if has_demand:
            return "demand_weighted"
        if has_non_demand:
            return "flow_weighted"
        return "uniform"

    def _generate_monthly_plan(self, proj: TransferProject, network,
                               config=None, mode: str = "uniform") -> np.ndarray:
        annual_plan = max(0.0, float(proj.annual_plan))
        if annual_plan <= 0:
            return np.zeros(12)

        if mode == "uniform":
            weights = np.ones(12, dtype=float) / 12.0
        elif mode == "demand_weighted":
            weights = self._demand_weights(proj, network)
            if weights is None:
                weights = np.ones(12, dtype=float) / 12.0
        elif mode == "flow_weighted":
            weights = self._flow_weights(proj, network)
            if weights is None:
                weights = np.ones(12, dtype=float) / 12.0
        elif mode == "hybrid":
            demand_weights = self._demand_weights(proj, network)
            flow_weights = self._flow_weights(proj, network)
            if demand_weights is not None and flow_weights is not None:
                alpha = float(getattr(config, "transfer_hybrid_demand_weight", 0.5))
                alpha = min(1.0, max(0.0, alpha))
                weights = alpha * demand_weights + (1.0 - alpha) * flow_weights
            else:
                weights = demand_weights if demand_weights is not None else flow_weights
            if weights is None:
                weights = np.ones(12, dtype=float) / 12.0
        else:
            weights = np.ones(12, dtype=float) / 12.0

        weights = np.maximum(np.asarray(weights, dtype=float), 0.0)
        if weights.sum() <= 0:
            weights = np.ones(12, dtype=float) / 12.0
        else:
            weights = weights / weights.sum()
        return annual_plan * weights

    def _demand_weights(self, proj: TransferProject, network) -> Optional[np.ndarray]:
        monthly = np.zeros(12, dtype=float)
        for recv_id, ratio in proj.receiving_nodes.items():
            node = network.nodes.get(recv_id)
            if not isinstance(node, DemandNode):
                continue
            total = np.zeros(12, dtype=float)
            found = False
            for arr in node.demands.values():
                if arr is None or len(arr) == 0:
                    continue
                total += self._monthly_climatology(np.asarray(arr, dtype=float))
                found = True
            if found:
                monthly += max(0.0, float(ratio)) * total
        if monthly.sum() <= 0:
            return None
        return monthly / monthly.sum()

    def _flow_weights(self, proj: TransferProject, network) -> Optional[np.ndarray]:
        monthly = np.zeros(12, dtype=float)
        found_any = False
        for recv_id, ratio in proj.receiving_nodes.items():
            if str(recv_id).startswith("DM_"):
                continue
            profile = self._node_natural_profile(recv_id, network)
            if profile is None or profile.sum() <= 0:
                continue
            monthly += max(0.0, float(ratio)) * profile
            found_any = True
        if found_any and monthly.sum() > 0:
            return monthly / monthly.sum()

        source_profile = self._node_natural_profile(proj.source_node_id, network)
        if source_profile is None or source_profile.sum() <= 0:
            return None
        return source_profile / source_profile.sum()

    def _node_natural_profile(self, node_id: str, network) -> Optional[np.ndarray]:
        node = network.nodes.get(node_id)
        if isinstance(node, SubbasinNode):
            series = node.precomputed_runoff
            if series is None or len(series) == 0:
                series = node.total_runoff
            if series is None or len(series) == 0:
                series = node.precip
            if series is None or len(series) == 0:
                return None
            return self._monthly_climatology(np.asarray(series, dtype=float))

        if isinstance(node, RiverChannelNode):
            monthly = np.zeros(12, dtype=float)
            found = False
            for upstream in self._collect_upstream_subbasins(node_id, network):
                profile = self._node_natural_profile(upstream.id, network)
                if profile is None:
                    continue
                monthly += profile
                found = True
            if found and monthly.sum() > 0:
                return monthly / monthly.sum()
            return None

        if isinstance(node, (ReservoirNode, LakeNode, RiverDiversionNode)):
            anchor_id = getattr(node, "channel_node_id", "")
            if anchor_id:
                return self._node_natural_profile(anchor_id, network)
        return None

    def _collect_upstream_subbasins(self, node_id: str, network) -> List[SubbasinNode]:
        results: List[SubbasinNode] = []
        stack = [node_id]
        seen = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            for link in network.upstream_links(current):
                if str(link.link_type.value) not in {"river", "tributary"}:
                    continue
                up_id = link.from_node
                up_node = network.nodes.get(up_id)
                if isinstance(up_node, SubbasinNode):
                    results.append(up_node)
                elif isinstance(up_node, RiverChannelNode):
                    stack.append(up_id)
        return results

    def _monthly_climatology(self, series: np.ndarray) -> np.ndarray:
        arr = np.asarray(series, dtype=float)
        if arr.size == 0:
            return np.ones(12, dtype=float) / 12.0
        trimmed = arr[: (arr.size // 12) * 12]
        if trimmed.size == 0:
            padded = np.pad(arr, (0, max(0, 12 - arr.size)), mode="edge")[:12]
            return np.maximum(padded, 0.0)
        month_grid = trimmed.reshape(-1, 12)
        return np.maximum(month_grid.mean(axis=0), 0.0)

    def step(self, t: int, year: int, month: int,
             node_flow: dict, src_nodes: dict) -> dict:
        source_withdraw = defaultdict(float)
        receiver_delivery = defaultdict(float)

        for src_id, proj_ids in self._by_source.items():
            projs = []
            for pid in proj_ids:
                if pid in self.projects:
                    projs.append((self.projects[pid].priority, pid))
            projs.sort()

            for _, pid in projs:
                proj = self.projects[pid]

                if year < proj.start_year:
                    self._record_zero(proj, t)
                    continue
                if proj.end_year and year > proj.end_year:
                    self._record_zero(proj, t)
                    continue

                plan = proj.monthly_plan[month - 1]
                if proj.planned_ts is not None and t < len(proj.planned_ts):
                    proj.planned_ts[t] = plan
                if plan <= 0:
                    continue

                available = self._check_source(
                    proj, src_nodes, src_id, source_withdraw)

                actual = min(plan, available, proj.intake_capacity)
                actual = max(0, actual)

                delivered = actual * (1.0 - proj.loss_rate)

                source_withdraw[src_id] += actual
                if proj.actual_ts is not None and t < len(proj.actual_ts):
                    proj.actual_ts[t] = actual
                    proj.delivered_ts[t] = delivered
                    proj.shortage_ts[t] = max(0, plan - actual)

                for recv_id, ratio in self.normalized_receiving_nodes(proj).items():
                    receiver_delivery[recv_id] += delivered * ratio

        return {
            "source_withdraw": dict(source_withdraw),
            "receiver_delivery": dict(receiver_delivery),
        }

    def _record_zero(self, proj: TransferProject, t: int):
        if proj.planned_ts is not None and t < len(proj.planned_ts):
            proj.planned_ts[t] = 0
            proj.actual_ts[t] = 0
            proj.delivered_ts[t] = 0
            proj.shortage_ts[t] = 0

    def _check_source(self, proj: TransferProject,
                       src_nodes: dict, src_id: str,
                       already_withdrawn: dict) -> float:
        src_node = src_nodes.get(src_id)
        already = already_withdrawn.get(src_id, 0)

        if src_node is None:
            return proj.intake_capacity

        if isinstance(src_node, ReservoirNode):
            sr = src_node.storage_ratio()
            if sr < proj.min_source_storage_ratio:
                return 0.0
            available = src_node.available_storage() * 0.5 - already
            return max(0, min(available, proj.intake_capacity))

        if isinstance(src_node, LakeNode):
            available = max(0, src_node.current_storage - src_node.eco_min_storage - already)
            return max(0, min(available * 0.3, proj.intake_capacity))

        return max(0, proj.intake_capacity)
