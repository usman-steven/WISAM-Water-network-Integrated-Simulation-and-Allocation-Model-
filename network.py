"""core/network.py - 水资源网络管理"""
from collections import defaultdict, deque
from typing import Dict, List, Optional
from config import NodeType, LinkType

class WaterNetwork:
    """
    水资源网络

    管理所有节点和连接，提供：
    - 按类型查询节点/连接
    - 查询上下游关系
    - 拓扑排序（Kahn算法）
    """

    def __init__(self, name: str = ""):
        self.name = name
        self.nodes: Dict[str, object] = {}
        self.links: Dict[str, object] = {}

        # 按类型索引
        self._by_type: Dict[NodeType, List[str]] = defaultdict(list)
        self._links_by_type: Dict[LinkType, List[str]] = defaultdict(list)

        # 邻接表
        self._upstream: Dict[str, List[str]] = defaultdict(list)
        self._downstream: Dict[str, List[str]] = defaultdict(list)

        # 拓扑排序缓存
        self._topo_cache: Optional[List[str]] = None
        self._topo_cache_managed: Optional[List[str]] = None

    def add_node(self, node):
        """添加节点（自动去重）"""
        self.nodes[node.id] = node
        if node.id not in self._by_type[node.node_type]:
            self._by_type[node.node_type].append(node.id)
        self._topo_cache = None
        self._topo_cache_managed = None

    def add_link(self, link):
        """添加连接（自动去重）"""
        if link.id in self.links:
            return
        self.links[link.id] = link
        self._links_by_type[link.link_type].append(link.id)
        self._upstream[link.to_node].append(link.id)
        self._downstream[link.from_node].append(link.id)
        self._topo_cache = None
        self._topo_cache_managed = None

    def get_nodes(self, node_type: NodeType) -> list:
        """获取指定类型的所有节点"""
        return [self.nodes[nid] for nid in self._by_type.get(node_type, [])
                if nid in self.nodes]

    def get_links(self, link_type: LinkType) -> list:
        """获取指定类型的所有连接"""
        return [self.links[lid] for lid in self._links_by_type.get(link_type, [])
                if lid in self.links]

    def upstream_links(self, node_id: str) -> list:
        """获取某节点的所有上游连接"""
        return [self.links[lid] for lid in self._upstream.get(node_id, [])
                if lid in self.links]

    def downstream_links(self, node_id: str) -> list:
        """获取某节点的所有下游连接"""
        return [self.links[lid] for lid in self._downstream.get(node_id, [])
                if lid in self.links]

    def topological_sort(self, include_management: bool = False) -> List[str]:
        """
        Kahn算法拓扑排序

        只考虑RIVER和TRIBUTARY连接（水流方向），
        保证上游节点排在下游节点前面。
        """
        if include_management and self._topo_cache_managed is not None:
            return self._topo_cache_managed
        if (not include_management) and self._topo_cache is not None:
            return self._topo_cache

        flow_types = {LinkType.RIVER, LinkType.TRIBUTARY}
        if include_management:
            flow_types = flow_types | {
                LinkType.SUPPLY_LOCAL,
                LinkType.SUPPLY_RESERVOIR,
                LinkType.SUPPLY_LAKE,
                LinkType.SUPPLY_GROUNDWATER,
                LinkType.SUPPLY_DIVERSION,
                LinkType.SUPPLY_TRANSFER,
            }
        in_degree = defaultdict(int)
        adj = defaultdict(list)
        all_nodes = set(self.nodes.keys())
        ordered_nodes = sorted(all_nodes)

        for nid in ordered_nodes:
            in_degree[nid] = 0

        link_values = list(self.links.values())
        if include_management:
            supply_types = {
                LinkType.SUPPLY_LOCAL,
                LinkType.SUPPLY_RESERVOIR,
                LinkType.SUPPLY_LAKE,
                LinkType.SUPPLY_GROUNDWATER,
                LinkType.SUPPLY_DIVERSION,
                LinkType.SUPPLY_TRANSFER,
            }
            link_values.sort(key=lambda link: 0 if link.link_type in supply_types else 1)

        for link in link_values:
            if link.link_type in flow_types:
                if link.from_node in all_nodes and link.to_node in all_nodes:
                    in_degree[link.to_node] += 1
                    adj[link.from_node].append(link.to_node)

        if include_management:
            facility_types = {
                NodeType.RESERVOIR,
                NodeType.LAKE,
                NodeType.RIVER_DIVERSION,
            }
            for node_id in ordered_nodes:
                node = self.nodes.get(node_id)
                if getattr(node, 'node_type', None) not in facility_types:
                    continue
                anchor_id = getattr(node, 'channel_node_id', '')
                if anchor_id in all_nodes and anchor_id != node_id:
                    in_degree[node_id] += 1
                    adj[anchor_id].append(node_id)

        # Keep traversal deterministic; scenario and resilience comparisons
        # should not depend on Python set iteration order.
        queue = deque([n for n in ordered_nodes if in_degree[n] == 0])
        result = []

        while queue:
            node = queue.popleft()
            result.append(node)
            for nb in adj[node]:
                in_degree[nb] -= 1
                if in_degree[nb] == 0:
                    queue.append(nb)

        # 有环或孤立节点，加到末尾
        remaining = all_nodes - set(result)
        if remaining:
            result.extend(sorted(remaining))

        if include_management:
            self._topo_cache_managed = result
        else:
            self._topo_cache = result
        return result

    def summary(self) -> str:
        """网络摘要"""
        lines = [f"WaterNetwork: {self.name}"]
        lines.append(f"  节点总数: {len(self.nodes)}")
        for nt in NodeType:
            count = len(self._by_type.get(nt, []))
            if count > 0:
                lines.append(f"    {nt.value}: {count}")
        lines.append(f"  连接总数: {len(self.links)}")
        for lt in LinkType:
            count = len(self._links_by_type.get(lt, []))
            if count > 0:
                lines.append(f"    {lt.value}: {count}")
        return '\n'.join(lines)
