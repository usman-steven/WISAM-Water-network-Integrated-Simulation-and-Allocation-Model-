"""
data_io/topology.py - 从计算单元数据构建网络拓扑

处理流程：
  1. 非干流单元 → SubbasinNode
  2. 干流单元   → RiverChannelNode
  3. 城市去重   → DemandNode
  4. topology.csv手动连接 → RiverLink/TributaryLink
  5. 未定义的非干流单元 → 自动连到同区干流
  6. 供水/退水连接
  7. 基础设施挂载（水库/湖泊/引水口，支持干流和非干流）
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional
from collections import defaultdict, deque

from core.nodes import (SubbasinNode, RiverChannelNode, DemandNode,
                         ReservoirNode, LakeNode, RiverDiversionNode,
                         OceanNode, EcoControlNode)
from core.links import RiverLink, SupplyLink, ReturnFlowLink
from core.network import WaterNetwork
from config import NodeType, LinkType
from coupling import DemandMappingRegistry, ServiceScopeResolver
from engineering import FacilityGraphRegistry
from model_metadata import DEFAULT_NETWORK_NAME

class TopologyBuilder:

    def __init__(self, config=None):
        self.network = WaterNetwork()
        self.config = config
        self.units_df = None
        self.channel_topo_df = None

        self._zone_to_channels = defaultdict(list)
        self._zone_to_subbasins = defaultdict(list)
        self._city_to_subbasins = defaultdict(list)
        self._city_to_channels = defaultdict(list)
        self._city_to_demand_nodes = defaultdict(list)
        self._uid_to_unit = {}
        self.demand_mapping = DemandMappingRegistry()
        self.facility_graph = FacilityGraphRegistry()

        self.stats = {
            'subbasin_nodes': 0, 'channel_nodes': 0,
            'demand_nodes': 0, 'river_links': 0,
            'supply_links': 0, 'return_links': 0,
            'tributary_links': 0,
        }

    # ══════════════ 数据加载 ══════════════

    def load_units(self, units_df):
        self.units_df = units_df.copy()

        col_map = {
            'UID': 'uid', 'UNAME': 'uname', 'CID': 'cid',
            'CNAME': 'cname', 'WID': 'wid', 'WNAME': 'wname',
            'AREA': 'area', '干流': 'is_mainstream',
        }
        for source_col, target_col in col_map.items():
            if source_col in self.units_df.columns:
                self.units_df.rename(columns={source_col: target_col}, inplace=True)

        self.units_df['is_mainstream'] = self.units_df['is_mainstream'].astype(int)
        self.units_df['cid'] = self.units_df['cid'].astype(str)
        self.units_df['uid'] = self.units_df['uid'].astype(str)
        self.units_df['wid'] = self.units_df['wid'].astype(str)
        self.units_df['area'] = self.units_df['area'].astype(float)

        if 'basin' not in self.units_df.columns:
            def _basin(wid):
                p = str(wid)[0].upper()
                return {'D': '黄河', 'E': '淮河', 'C': '海河', 'F': '长江'}.get(p, '未知')
            self.units_df['basin'] = self.units_df['wid'].apply(_basin)

        for col in ['basin_l2', 'province']:
            if col not in self.units_df.columns:
                self.units_df[col] = ''

        for _, row in self.units_df.iterrows():
            uid, wid, cid = row['uid'], row['wid'], row['cid']
            self._uid_to_unit[uid] = row.to_dict()
            if row['is_mainstream']:
                self._zone_to_channels[wid].append(uid)
                self._city_to_channels[cid].append(uid)
            else:
                self._zone_to_subbasins[wid].append(uid)
                self._city_to_subbasins[cid].append(uid)

        self.demand_mapping = DemandMappingRegistry.from_units_df(self.units_df)

        n_sub = int((self.units_df['is_mainstream'] == 0).sum())
        n_ch = int((self.units_df['is_mainstream'] == 1).sum())
        print(f"  加载 {len(self.units_df)} 个计算单元: {n_sub} 产流 + {n_ch} 干流")

    def load_channel_topology(self, topo_df):
        self.channel_topo_df = topo_df.copy()
        print(f"  加载 {len(topo_df)} 条拓扑关系")

    # ══════════════ 构建 ══════════════

    def build(self, network_name=DEFAULT_NETWORK_NAME):
        self.network = WaterNetwork(network_name)
        self._city_to_demand_nodes.clear()
        self.demand_mapping = DemandMappingRegistry.from_units_df(self.units_df)
        self.facility_graph = FacilityGraphRegistry()
        self.network._demand_mapping = self.demand_mapping
        self.network._facility_graph = self.facility_graph

        print("\n[1/6] 创建产流节点...")
        self._create_subbasin_nodes()
        print("[2/6] 创建干流节点...")
        self._create_channel_nodes()
        print("[3/6] 创建需水节点...")
        self._create_demand_nodes()
        print("[4/6] 创建手动拓扑连接...")
        self._create_river_links()
        print("[5/6] 自动补齐连接...")
        self._create_tributary_links()
        print("[6/6] 创建供水/退水连接...")
        self._create_supply_and_return_links()

        self._print_summary()
        return self.network

    def _create_subbasin_nodes(self):
        sub_df = self.units_df[self.units_df['is_mainstream'] == 0]
        for _, row in sub_df.iterrows():
            node = SubbasinNode(
                id=f"SB_{row['uid']}", name=row.get('uname', ''),
                x=float(row.get('x', 0)), y=float(row.get('y', 0)),
                basin=row.get('basin', ''),
                water_zone_l2=row.get('basin_l2', ''),
                water_zone_l3=row.get('wid', ''),
                province=row.get('province', ''),
                area=row['area'], unit_id=row['uid'],
                city_code=row['cid'], city_name=row.get('cname', ''),
            )
            self.network.add_node(node)
            self.stats['subbasin_nodes'] += 1

    def _create_channel_nodes(self):
        ch_df = self.units_df[self.units_df['is_mainstream'] == 1]
        for _, row in ch_df.iterrows():
            node = RiverChannelNode(
                id=f"CH_{row['uid']}", name=row.get('uname', ''),
                x=float(row.get('x', 0)), y=float(row.get('y', 0)),
                basin=row.get('basin', ''),
                water_zone_l2=row.get('basin_l2', ''),
                water_zone_l3=row.get('wid', ''),
                province=row.get('province', ''),
                area=row['area'], unit_id=row['uid'],
                river_name=row.get('wname', ''),
            )
            self.network.add_node(node)
            self.stats['channel_nodes'] += 1

    def _create_demand_nodes(self):
        for link in self.demand_mapping.links:
            unit_info = self._uid_to_unit.get(link.unit_id, {})
            node = DemandNode(
                id=f"DM_{link.unit_id}",
                name=f"{link.city_name}-{link.zone_name or link.unit_name or link.unit_id}",
                basin=link.basin,
                water_zone_l2=link.basin_l2 or unit_info.get('basin_l2', ''),
                water_zone_l3=link.zone_id or unit_info.get('wid', ''),
                province=link.province,
                unit_id=link.unit_id,
                city_code=link.city_code,
                city_name=link.city_name,
                coupled_unit_ids=[link.unit_id],
                demand_area_weight=float(link.weight),
                water_zone_id=link.zone_id or unit_info.get('wid', ''),
                water_zone_name=link.zone_name or unit_info.get('wname', ''),
                basin_l2=link.basin_l2 or unit_info.get('basin_l2', ''),
            )
            self.network.add_node(node)
            self._city_to_demand_nodes[link.city_code].append(node.id)
            self.stats['demand_nodes'] += 1

    def _demand_mapping_links_for_node(self, dm: DemandNode):
        unit_ids = list(getattr(dm, "coupled_unit_ids", []) or [])
        if unit_ids:
            links = []
            for unit_id in unit_ids:
                links.extend(self.demand_mapping.get_unit_links(unit_id))
            if links:
                return links
        return self.demand_mapping.get_city_links(dm.city_code)

    def _demand_unit_ids_for_node(self, dm: DemandNode):
        unit_ids = list(getattr(dm, "coupled_unit_ids", []) or [])
        if unit_ids:
            return [str(unit_id) for unit_id in unit_ids]
        return self.demand_mapping.get_city_units(dm.city_code)

    def _demand_weight_for_node_unit(self, dm: DemandNode, unit_id: str) -> float:
        node_unit_id = str(getattr(dm, "unit_id", "") or "")
        if node_unit_id and str(unit_id) == node_unit_id:
            return 1.0
        unit_ids = self._demand_unit_ids_for_node(dm)
        if len(unit_ids) == 1 and str(unit_id) == str(unit_ids[0]):
            return 1.0
        weight = self.demand_mapping.get_city_weight(dm.city_code, unit_id)
        if weight > 0:
            return float(weight)
        return 0.0 if not unit_ids else 1.0 / len(unit_ids)

    # ══════════════ 手动拓扑 ══════════════

    def _create_river_links(self):
        if self.channel_topo_df is None or len(self.channel_topo_df) == 0:
            print("    ⚠ 未提供拓扑文件")
            return
        for _, row in self.channel_topo_df.iterrows():
            from_uid = str(row.get('from_uid', '')).strip()
            to_uid = str(row.get('to_uid', '')).strip()
            if not from_uid or not to_uid:
                continue
            from_id = self._resolve_node_id(from_uid)
            to_id = self._resolve_node_id(to_uid)
            if from_id is None or to_id is None:
                continue
            if from_id == to_id:
                continue

            from_node = self.network.nodes[from_id]
            to_node = self.network.nodes[to_id]
            if (isinstance(from_node, RiverChannelNode) and
                    isinstance(to_node, RiverChannelNode)):
                ltype = LinkType.RIVER
            else:
                ltype = LinkType.TRIBUTARY

            loss = float(row.get('loss_rate', 0.02))
            river = str(row.get('river_name', ''))
            link = RiverLink(
                id=f"TP_{from_uid[:15]}_to_{to_uid[:15]}",
                name=f"{river}:{from_uid[:12]}→{to_uid[:12]}",
                link_type=ltype, from_node=from_id, to_node=to_id,
                loss_rate=loss,
            )
            self.network.add_link(link)
            self.stats['river_links'] += 1

    def _resolve_node_id(self, uid):
        for prefix in ['CH_', 'SB_', '']:
            c = f"{prefix}{uid}"
            if c in self.network.nodes:
                return c
        return None

    # ══════════════ 自动补齐 ══════════════

    def _create_tributary_links(self):
        manually_linked_from = set()
        manually_linked_to = set()
        if self.channel_topo_df is not None:
            for _, row in self.channel_topo_df.iterrows():
                f = str(row.get('from_uid', '')).strip()
                t = str(row.get('to_uid', '')).strip()
                if f: manually_linked_from.add(f)
                if t: manually_linked_to.add(t)

        all_uids = set(self.units_df['uid'].tolist())
        linked = manually_linked_from | manually_linked_to
        coverage = len(linked & all_uids) / max(1, len(all_uids))

        if coverage > 0.5:
            not_linked = all_uids - linked
            print(f"    全连接模式（覆盖率{coverage:.1%}）")
            print(f"    已连接: {len(linked & all_uids)}, 未连接: {len(not_linked)}")
            return

        sub_df = self.units_df[self.units_df['is_mainstream'] == 0]
        auto_count, skip_count = 0, 0
        for _, row in sub_df.iterrows():
            uid = row['uid']
            if uid in manually_linked_from:
                skip_count += 1
                continue
            target_ch = self._find_target_channel(row['wid'], row['cid'])
            if target_ch:
                link = RiverLink(
                    id=f"TR_{uid}_to_{target_ch}",
                    name=f"{row.get('uname', '')}→干流",
                    link_type=LinkType.TRIBUTARY,
                    from_node=f"SB_{uid}", to_node=f"CH_{target_ch}",
                    loss_rate=0.02,
                )
                self.network.add_link(link)
                self.stats['tributary_links'] += 1
                auto_count += 1
        print(f"    手动: {skip_count}, 自动补齐: {auto_count}")

    def _find_target_channel(self, wid, cid):
        channels = self._zone_to_channels.get(wid, [])
        for ch_uid in channels:
            if self._uid_to_unit.get(ch_uid, {}).get('cid') == cid:
                return ch_uid
        if channels:
            return channels[0]
        for z in sorted(self._zone_to_channels.keys()):
            if z[:4] == wid[:4] and self._zone_to_channels[z]:
                return self._zone_to_channels[z][0]
        return None

    # ══════════════ 供水/退水 ══════════════

    def _create_supply_and_return_links(self):
        for dm in self.network.get_nodes(NodeType.DEMAND):
            if not isinstance(dm, DemandNode):
                continue
            cid = dm.city_code

            for source_id, meta in self._build_surface_supply_targets(dm).items():
                link = SupplyLink(
                    id=f"SP_{source_id}_to_{dm.id}",
                    name=f"local_surface_{source_id}_to_{dm.id}",
                    link_type=LinkType.SUPPLY_LOCAL,
                    from_node=source_id, to_node=dm.id,
                    loss_rate=0.05, source_priority=1,
                    service_weight=float(meta.get("weight", 1.0)),
                    source_unit_ids=";".join(sorted(meta.get("unit_ids", []))),
                    selection_method=str(meta.get("selection_method", "local_surface_intake")),
                )
                self.network.add_link(link)
                self.stats['supply_links'] += 1

            for ret_id, meta in self._build_return_targets(dm).items():
                ratio = float(meta.get("ratio", 0.0))
                unit_ids = ";".join(sorted(meta.get("unit_ids", [])))
                link = ReturnFlowLink(
                    id=f"RF_{dm.id}_to_{ret_id}",
                    name=f"{dm.city_name}退水",
                    from_node=dm.id, to_node=ret_id,
                    return_ratio=ratio,
                    source_unit_ids=unit_ids,
                    selection_method="coupled_hydrologic_unit",
                )
                self.network.add_link(link)
                self.stats['return_links'] += 1

    def _expand_demand_targets(self, demand_ref, ratio):
        """Expand legacy city refs or current unit refs onto active demand-unit nodes."""
        return self.demand_mapping.resolve_demand_ref(
            str(demand_ref), float(ratio), set(self.network.nodes.keys()))

    def _expand_transfer_targets(self, target_ref, ratio):
        raw = str(target_ref).strip()
        if not raw:
            return {}
        if raw in self.network.nodes and raw.startswith(
            ('DM_', 'CH_', 'SB_', 'RES_', 'LK_', 'DIV_')
        ):
            return {raw: float(ratio)}
        anchor_id = self._resolve_anchor_node_id(raw)
        if anchor_id:
            return {anchor_id: float(ratio)}
        return self._expand_demand_targets(raw, ratio)

    def _find_return_target(self, cid):
        chs = self._city_to_channels.get(cid, [])
        if chs:
            return chs[0]
        sbs = self._city_to_subbasins.get(cid, [])
        for sb_uid in sbs:
            info = self._uid_to_unit.get(sb_uid, {})
            wid = info.get('wid', '')
            zc = self._zone_to_channels.get(wid, [])
            if zc:
                return zc[0]
        return None

    def _find_unit_surface_supply_node_id(self, unit_id):
        """Use the local routed river node as the surface-water intake source."""
        unit_id = str(unit_id)
        direct_channel_id = f"CH_{unit_id}"
        if direct_channel_id in self.network.nodes:
            return direct_channel_id, "local_channel"

        start_id = f"SB_{unit_id}"
        if start_id in self.network.nodes:
            queue = deque([start_id])
            visited = {start_id}
            while queue:
                cur = queue.popleft()
                for link in self.network.downstream_links(cur):
                    if link.link_type not in (LinkType.RIVER, LinkType.TRIBUTARY):
                        continue
                    nxt = link.to_node
                    if nxt in visited:
                        continue
                    node = self.network.nodes.get(nxt)
                    if isinstance(node, RiverChannelNode):
                        return nxt, "downstream_channel"
                    if isinstance(node, SubbasinNode):
                        visited.add(nxt)
                        queue.append(nxt)
            return start_id, "local_hydrologic_unit"

        return None, ""

    def _build_surface_supply_targets(self, dm: DemandNode):
        """Build local surface-water intake links from routed local units/channels."""
        targets = defaultdict(lambda: {"weight": 0.0, "unit_ids": [], "selection_method": ""})
        for link in self._demand_mapping_links_for_node(dm):
            source_id, method = self._find_unit_surface_supply_node_id(link.unit_id)
            if not source_id:
                continue
            targets[source_id]["weight"] += max(
                0.0, self._demand_weight_for_node_unit(dm, link.unit_id))
            targets[source_id]["unit_ids"].append(str(link.unit_id))
            targets[source_id]["selection_method"] = method

        total = sum(float(meta["weight"]) for meta in targets.values())
        if total > 0:
            for meta in targets.values():
                meta["weight"] = float(meta["weight"]) / total
        return targets

    def _find_unit_return_node_id(self, unit_id):
        """Route unit return flow back to its coupled hydrologic unit first."""
        unit_id = str(unit_id)
        local_subbasin_id = f"SB_{unit_id}"
        if local_subbasin_id in self.network.nodes:
            return local_subbasin_id

        direct_channel_id = f"CH_{unit_id}"
        if direct_channel_id in self.network.nodes:
            return direct_channel_id

        info = self._uid_to_unit.get(unit_id, {})
        source_node = self.network.nodes.get(f"SB_{unit_id}") or self.network.nodes.get(direct_channel_id)
        channels = [
            ch for ch in self.network.get_nodes(NodeType.RIVER_CHANNEL)
            if isinstance(ch, RiverChannelNode)
        ]
        if not channels:
            return None

        wid = str(info.get('wid', '') or '')
        basin_l2 = str(info.get('basin_l2', '') or '')
        basin = str(info.get('basin', '') or getattr(source_node, 'basin', '') or '')
        province = str(info.get('province', '') or getattr(source_node, 'province', '') or '')

        def channel_info(ch):
            uid = str(getattr(ch, 'unit_id', '') or ch.id.replace('CH_', ''))
            return self._uid_to_unit.get(uid, {})

        candidate_groups = []
        if wid:
            candidate_groups.append([
                ch for ch in channels
                if str(channel_info(ch).get('wid', '') or '') == wid
            ])
        if basin_l2 and province:
            candidate_groups.append([
                ch for ch in channels
                if str(channel_info(ch).get('basin_l2', '') or '') == basin_l2
                and str(channel_info(ch).get('province', '') or '') == province
            ])
        if basin_l2:
            candidate_groups.append([
                ch for ch in channels
                if str(channel_info(ch).get('basin_l2', '') or '') == basin_l2
            ])
        if basin and province:
            candidate_groups.append([
                ch for ch in channels
                if getattr(ch, 'basin', '') == basin and getattr(ch, 'province', '') == province
            ])
        if basin:
            candidate_groups.append([ch for ch in channels if getattr(ch, 'basin', '') == basin])
        candidate_groups.append(channels)

        for candidates in candidate_groups:
            candidates = [ch for ch in candidates if ch is not None]
            if candidates:
                return self._nearest_node_id(source_node, candidates)
        return None

    def _nearest_node_id(self, source_node, candidates):
        if not candidates:
            return None
        sx = float(getattr(source_node, 'x', 0.0) or 0.0) if source_node is not None else 0.0
        sy = float(getattr(source_node, 'y', 0.0) or 0.0) if source_node is not None else 0.0
        has_source_xy = abs(sx) > 1e-9 or abs(sy) > 1e-9
        if has_source_xy:
            return min(
                candidates,
                key=lambda ch: (
                    (float(getattr(ch, 'x', 0.0) or 0.0) - sx) ** 2
                    + (float(getattr(ch, 'y', 0.0) or 0.0) - sy) ** 2,
                    ch.id,
                ),
            ).id
        return sorted(candidates, key=lambda ch: ch.id)[0].id

    def _build_return_targets(self, dm: DemandNode):
        """Build weighted unit-level return-flow targets for a demand-unit node."""
        targets = defaultdict(lambda: {"ratio": 0.0, "unit_ids": []})
        for link in self._demand_mapping_links_for_node(dm):
            ret_id = self._find_unit_return_node_id(link.unit_id)
            if not ret_id:
                target_uid = self._find_return_target(dm.city_code)
                ret_id = self._resolve_node_id(target_uid) if target_uid else None
            if ret_id:
                targets[ret_id]["ratio"] += max(
                    0.0, self._demand_weight_for_node_unit(dm, link.unit_id))
                targets[ret_id]["unit_ids"].append(str(link.unit_id))

        if not targets:
            target_uid = self._find_return_target(dm.city_code)
            ret_id = self._resolve_node_id(target_uid) if target_uid else None
            if ret_id:
                targets[ret_id]["ratio"] = 1.0

        total = sum(float(meta["ratio"]) for meta in targets.values())
        if total > 0:
            return {
                ret_id: {
                    "ratio": float(meta["ratio"]) / total,
                    "unit_ids": meta["unit_ids"],
                }
                for ret_id, meta in targets.items()
            }
        return {}

    def _collect_downstream_demands(self, start_node_id):
        resolver = ServiceScopeResolver(self.network, self.config)
        return resolver.collect_reservoir_demands(start_node_id)

    def _resolve_anchor_node_id(self, raw_ref):
        raw = str(raw_ref).strip()
        if not raw:
            return ''
        if raw in self.network.nodes and raw.startswith(('CH_', 'SB_')):
            return raw
        for prefix in ['CH_', 'SB_']:
            candidate = f"{prefix}{raw}"
            if candidate in self.network.nodes:
                return candidate
        return ''

    def load_facility_links(self, facility_df):
        if facility_df is None or len(facility_df) == 0:
            return
        for _, row in facility_df.iterrows():
            anchor_ref = str(row.get('anchor_node_id', '')).strip() or str(row.get('unit_id', '')).strip()
            anchor_node_id = self._resolve_anchor_node_id(anchor_ref)
            if not anchor_node_id:
                continue
            from_node = str(row.get('from_node', '')).strip() or '__ENTRY__'
            to_node = str(row.get('to_node', '')).strip() or '__OUTLET__'
            try:
                split_ratio = float(row.get('split_ratio', 1.0) or 1.0)
            except Exception:
                split_ratio = 1.0
            try:
                priority = int(float(row.get('priority', 1) or 1))
            except Exception:
                priority = 1
            self.facility_graph.add_edge(anchor_node_id, from_node, to_node, split_ratio, priority)

    def finalize_facility_graphs(self):
        self.facility_graph.finalize()

    # ══════════════ 基础设施挂载 ══════════════

    def add_reservoir(self, res_node, channel_uid):
        ch_id = f"CH_{channel_uid}"
        sb_id = f"SB_{channel_uid}"
        if ch_id in self.network.nodes:
            node = self.network.nodes[ch_id]
            if isinstance(node, RiverChannelNode):
                node.reservoir_ids.append(res_node.id)
            res_node.channel_node_id = ch_id
            self.facility_graph.register_facility(ch_id, res_node.id, 'reservoir', res_node.order_in_channel)
        elif sb_id in self.network.nodes:
            node = self.network.nodes[sb_id]
            if isinstance(node, SubbasinNode):
                node.reservoir_ids.append(res_node.id)
            res_node.channel_node_id = sb_id
            self.facility_graph.register_facility(sb_id, res_node.id, 'reservoir', res_node.order_in_channel)
        else:
            print(f"    ⚠ 水库 {res_node.name} 挂载点 {channel_uid} 不存在")
        self.network.add_node(res_node)

    def add_lake(self, lake_node, channel_uid):
        ch_id = f"CH_{channel_uid}"
        sb_id = f"SB_{channel_uid}"
        if ch_id in self.network.nodes:
            node = self.network.nodes[ch_id]
            if isinstance(node, RiverChannelNode):
                node.lake_ids.append(lake_node.id)
            lake_node.channel_node_id = ch_id
            self.facility_graph.register_facility(ch_id, lake_node.id, 'lake', 1000)
        elif sb_id in self.network.nodes:
            node = self.network.nodes[sb_id]
            if isinstance(node, SubbasinNode):
                node.lake_ids.append(lake_node.id)
            lake_node.channel_node_id = sb_id
            self.facility_graph.register_facility(sb_id, lake_node.id, 'lake', 1000)
        else:
            print(f"    ⚠ 湖泊 {lake_node.name} 挂载点 {channel_uid} 不存在")
        self.network.add_node(lake_node)

    def add_diversion(self, div_node, channel_uid):
        ch_id = f"CH_{channel_uid}"
        sb_id = f"SB_{channel_uid}"
        if ch_id in self.network.nodes:
            node = self.network.nodes[ch_id]
            if isinstance(node, RiverChannelNode):
                node.diversion_ids.append(div_node.id)
            div_node.channel_node_id = ch_id
            self.facility_graph.register_facility(ch_id, div_node.id, 'diversion', div_node.priority)
        elif sb_id in self.network.nodes:
            node = self.network.nodes[sb_id]
            if isinstance(node, SubbasinNode):
                node.diversion_ids.append(div_node.id)
            div_node.channel_node_id = sb_id
            self.facility_graph.register_facility(sb_id, div_node.id, 'diversion', div_node.priority)
        else:
            print(f"    ⚠ 引水口 {div_node.name} 挂载点 {channel_uid} 不存在")
        self.network.add_node(div_node)
        for dm_id, ratio in div_node.supply_to.items():
            if dm_id in self.network.nodes:
                link = SupplyLink(
                    id=f"SP_{div_node.id}_to_{dm_id}",
                    name=f"引水{div_node.name}→{dm_id}",
                    link_type=LinkType.SUPPLY_DIVERSION,
                    from_node=div_node.id, to_node=dm_id,
                    loss_rate=0.05, source_priority=3,
                )
                self.network.add_link(link)

    def add_ocean(self, ocean_node, channel_uid):
        self.network.add_node(ocean_node)
        target_id = self._resolve_node_id(channel_uid)
        if target_id:
            ocean_node.channel_node_id = target_id
            link = RiverLink(
                id=f"RV_{target_id}_to_{ocean_node.id}",
                name=f"→{ocean_node.name}",
                from_node=target_id, to_node=ocean_node.id, loss_rate=0.01,
            )
            self.network.add_link(link)

    def add_eco_control(self, eco_node, channel_uid):
        target_id = self._resolve_node_id(channel_uid)
        if target_id:
            eco_node.channel_node_id = target_id
        self.network.add_node(eco_node)

    # ══════════════ 汇总 ══════════════

    def _print_summary(self):
        s = self.stats
        print(f"\n{'='*50}")
        print(f"  网络: {self.network.name}")
        print(f"  产流节点: {s['subbasin_nodes']}")
        print(f"  干流节点: {s['channel_nodes']}")
        print(f"  需水节点: {s['demand_nodes']}")
        print(f"  手动拓扑: {s['river_links']}")
        print(f"  自动补齐: {s['tributary_links']}")
        print(f"  供水连接: {s['supply_links']}")
        print(f"  退水连接: {s['return_links']}")
        total_n = sum(len(ids) for ids in self.network._by_type.values())
        print(f"  总节点: {total_n}, 总连接: {len(self.network.links)}")
        print(f"{'='*50}")
