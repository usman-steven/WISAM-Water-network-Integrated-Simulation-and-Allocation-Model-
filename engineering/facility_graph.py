from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


FACILITY_ENTRY = "__ENTRY__"
FACILITY_OUTLET = "__OUTLET__"


@dataclass
class FacilityEdge:
    from_node: str
    to_node: str
    split_ratio: float = 1.0
    priority: int = 1


@dataclass
class AnchorFacilityGraph:
    anchor_node_id: str
    facility_types: Dict[str, str] = field(default_factory=dict)
    facility_orders: Dict[str, int] = field(default_factory=dict)
    explicit_edges: List[FacilityEdge] = field(default_factory=list)
    edges: List[FacilityEdge] = field(default_factory=list)

    def register_facility(self, facility_id: str, facility_type: str, order: int = 1):
        self.facility_types[facility_id] = facility_type
        self.facility_orders[facility_id] = int(order)

    def add_explicit_edge(self, from_node: str, to_node: str,
                          split_ratio: float = 1.0, priority: int = 1):
        self.explicit_edges.append(FacilityEdge(
            from_node=from_node,
            to_node=to_node,
            split_ratio=float(split_ratio),
            priority=int(priority),
        ))

    def _normalize_explicit_edges(self):
        edges = [
            FacilityEdge(
                from_node=edge.from_node,
                to_node=edge.to_node,
                split_ratio=max(0.0, float(edge.split_ratio)),
                priority=int(edge.priority),
            )
            for edge in self.explicit_edges
        ]

        used_nodes = {
            node_id
            for edge in edges
            for node_id in (edge.from_node, edge.to_node)
            if node_id not in {FACILITY_ENTRY, FACILITY_OUTLET}
        }
        outgoing = defaultdict(list)
        incoming = defaultdict(list)
        for edge in edges:
            outgoing[edge.from_node].append(edge)
            incoming[edge.to_node].append(edge)

        if not any(edge.from_node == FACILITY_ENTRY for edge in edges):
            roots = sorted(
                node_id for node_id in used_nodes
                if len(incoming[node_id]) == 0
            )
            if roots:
                ratio = 1.0 / len(roots)
                for node_id in roots:
                    edges.append(FacilityEdge(
                        from_node=FACILITY_ENTRY,
                        to_node=node_id,
                        split_ratio=ratio,
                        priority=1,
                    ))

        for node_id in sorted(used_nodes):
            if len(outgoing[node_id]) == 0:
                edges.append(FacilityEdge(
                    from_node=node_id,
                    to_node=FACILITY_OUTLET,
                    split_ratio=1.0,
                    priority=1,
                ))

        self.edges = self._normalized_ratios(edges)

    def _build_default_edges(self):
        storage_types = {"reservoir", "lake"}
        type_rank = {"reservoir": 0, "lake": 1, "diversion": 2}
        facility_ids = [
            facility_id for facility_id, facility_type in self.facility_types.items()
            if facility_type in storage_types
        ]
        facility_ids.sort(
            key=lambda facility_id: (
                type_rank.get(self.facility_types.get(facility_id, ""), 99),
                self.facility_orders.get(facility_id, 1),
                facility_id,
            )
        )
        if not facility_ids:
            self.edges = []
            return

        edges: List[FacilityEdge] = [
            FacilityEdge(FACILITY_ENTRY, facility_ids[0], 1.0, 1),
        ]
        for upstream_id, downstream_id in zip(facility_ids[:-1], facility_ids[1:]):
            edges.append(FacilityEdge(upstream_id, downstream_id, 1.0, 1))
        edges.append(FacilityEdge(facility_ids[-1], FACILITY_OUTLET, 1.0, 1))
        self.edges = edges

    def _normalized_ratios(self, edges: List[FacilityEdge]) -> List[FacilityEdge]:
        grouped = defaultdict(list)
        for edge in edges:
            grouped[edge.from_node].append(edge)

        normalized: List[FacilityEdge] = []
        for from_node, from_edges in grouped.items():
            total = sum(max(0.0, edge.split_ratio) for edge in from_edges)
            if total <= 0:
                ratio = 1.0 / len(from_edges)
                for edge in from_edges:
                    normalized.append(FacilityEdge(
                        from_node=edge.from_node,
                        to_node=edge.to_node,
                        split_ratio=ratio,
                        priority=edge.priority,
                    ))
                continue

            if total <= 1.0 + 1e-9:
                normalized.extend(from_edges)
                continue

            for edge in from_edges:
                normalized.append(FacilityEdge(
                    from_node=edge.from_node,
                    to_node=edge.to_node,
                    split_ratio=max(0.0, edge.split_ratio) / total,
                    priority=edge.priority,
                ))
        return normalized

    def finalize(self):
        if self.explicit_edges:
            self._normalize_explicit_edges()
        else:
            self._build_default_edges()

    def contains(self, facility_id: str) -> bool:
        return facility_id in self.facility_types

    def graph_nodes(self) -> Set[str]:
        return {
            node_id
            for edge in self.edges
            for node_id in (edge.from_node, edge.to_node)
            if node_id not in {FACILITY_ENTRY, FACILITY_OUTLET}
        }

    def topological_order(self) -> List[str]:
        nodes = self.graph_nodes() | {FACILITY_ENTRY, FACILITY_OUTLET}
        if not nodes:
            return []

        adjacency: Dict[str, List[str]] = defaultdict(list)
        indegree: Dict[str, int] = {node_id: 0 for node_id in nodes}

        for edge in self.edges:
            adjacency[edge.from_node].append(edge.to_node)
            indegree[edge.to_node] = indegree.get(edge.to_node, 0) + 1
            indegree.setdefault(edge.from_node, 0)

        queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
        order: List[str] = []
        while queue:
            node_id = queue.popleft()
            order.append(node_id)
            for neighbor in adjacency.get(node_id, []):
                indegree[neighbor] -= 1
                if indegree[neighbor] == 0:
                    queue.append(neighbor)

        remaining = nodes - set(order)
        if remaining:
            order.extend(sorted(remaining))
        return order


class FacilityGraphRegistry:
    def __init__(self):
        self._graphs: Dict[str, AnchorFacilityGraph] = {}
        self._facility_to_anchor: Dict[str, str] = {}

    def register_facility(self, anchor_node_id: str, facility_id: str,
                          facility_type: str, order: int = 1):
        if not anchor_node_id or not facility_id:
            return
        graph = self._graphs.setdefault(anchor_node_id, AnchorFacilityGraph(anchor_node_id))
        graph.register_facility(facility_id, facility_type, order)
        self._facility_to_anchor[facility_id] = anchor_node_id

    def add_edge(self, anchor_node_id: str, from_node: str, to_node: str,
                 split_ratio: float = 1.0, priority: int = 1):
        if not anchor_node_id:
            return
        graph = self._graphs.setdefault(anchor_node_id, AnchorFacilityGraph(anchor_node_id))
        graph.add_explicit_edge(from_node, to_node, split_ratio, priority)

    def finalize(self):
        for graph in self._graphs.values():
            graph.finalize()

    def get(self, anchor_node_id: str) -> Optional[AnchorFacilityGraph]:
        return self._graphs.get(anchor_node_id)

    def contains(self, anchor_node_id: str, facility_id: str) -> bool:
        graph = self._graphs.get(anchor_node_id)
        return graph.contains(facility_id) if graph is not None else False

    def graph_nodes(self, anchor_node_id: str) -> Set[str]:
        graph = self._graphs.get(anchor_node_id)
        if graph is None:
            return set()
        return graph.graph_nodes()

    def anchor_for(self, facility_id: str) -> str:
        return self._facility_to_anchor.get(facility_id, "")
