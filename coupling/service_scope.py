from __future__ import annotations

from collections import deque
from typing import Set

from config import LinkType
from core.nodes import DemandNode, RiverChannelNode, SubbasinNode


class ServiceScopeResolver:
    def __init__(self, network, config=None):
        self.network = network
        self.config = config

    def collect_reservoir_demands(self, start_node_id: str):
        demand_ids: Set[str] = set()
        visited = set()
        queue = deque([(start_node_id, 0)])
        flow_types = {LinkType.RIVER, LinkType.TRIBUTARY}
        service_scope = getattr(self.config, 'reservoir_service_scope', 'downstream_mainstem')
        include_trib = getattr(self.config, 'reservoir_service_include_tributaries', True)
        max_hops = int(getattr(self.config, 'reservoir_service_hops', 8))

        while queue:
            node_id, hops = queue.popleft()
            if node_id in visited:
                continue
            visited.add(node_id)

            node = self.network.nodes.get(node_id)
            if isinstance(node, SubbasinNode):
                for link in self.network.downstream_links(node_id):
                    if link.link_type == LinkType.SUPPLY_LOCAL:
                        target = self.network.nodes.get(link.to_node)
                        if isinstance(target, DemandNode):
                            demand_ids.add(link.to_node)

            if include_trib and isinstance(node, RiverChannelNode):
                for link in self.network.upstream_links(node_id):
                    if link.link_type == LinkType.TRIBUTARY:
                        up_node = self.network.nodes.get(link.from_node)
                        if isinstance(up_node, SubbasinNode):
                            for sp_link in self.network.downstream_links(up_node.id):
                                if sp_link.link_type == LinkType.SUPPLY_LOCAL:
                                    target = self.network.nodes.get(sp_link.to_node)
                                    if isinstance(target, DemandNode):
                                        demand_ids.add(sp_link.to_node)

            for link in self.network.downstream_links(node_id):
                if link.link_type == LinkType.SUPPLY_LOCAL:
                    target = self.network.nodes.get(link.to_node)
                    if isinstance(target, DemandNode):
                        demand_ids.add(link.to_node)
                elif link.link_type in flow_types:
                    if service_scope == 'anchor_only':
                        continue
                    if hops < max_hops:
                        queue.append((link.to_node, hops + 1))

        return sorted(demand_ids)
