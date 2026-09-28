"""
water/diversion.py - 河道引水模块

河道引水口从干流或支流取水，供给下游城市。
同一节点上可能有多个引水口，按优先级竞争水量。

约束：
  1. 不超过引水口最大能力
  2. 保留生态流量
  3. 保留下游最低过境流量
  4. 按优先级分配

接口（simulator调用）：
  diversion_mod.init_series(n)
  diversion_mod.allocate_at_channel(node_id, flow, eco, downstream, t) → dict
"""
import numpy as np
from typing import Dict, List
from collections import defaultdict
from core.nodes import RiverDiversionNode

class DiversionModule:
    """河道引水分配模块"""

    def __init__(self):
        # channel_node_id → [diversion_node_id, ...]
        self._by_channel: Dict[str, List[str]] = defaultdict(list)
        # div_id → RiverDiversionNode
        self._nodes: Dict[str, RiverDiversionNode] = {}

    def init_series(self, n_steps: int):
        """
        初始化引水序列

        注意：实际的引水口注册在TopologyBuilder.add_diversion中完成，
        此处仅作为simulator.initialize()的标准接口。
        如果有已注册的引水口，初始化其时间序列。
        """
        for div_node in self._nodes.values():
            if div_node.actual_ts is None:
                div_node.actual_ts = np.zeros(n_steps)

    def register(self, div_node: RiverDiversionNode):
        """
        注册引水口

        由数据加载过程调用，
        在水库/引水口挂载到网络节点之后执行。
        """
        self._nodes[div_node.id] = div_node
        ch_id = div_node.channel_node_id
        if ch_id and div_node.id not in self._by_channel[ch_id]:
            self._by_channel[ch_id].append(div_node.id)

        # 初始化时间序列
        if div_node.actual_ts is None:
            div_node.actual_ts = np.zeros(732)

    def allocate_for_diversions(self, diversion_ids: List[str],
                               available_flow: float,
                               eco_reserve: float,
                               downstream_min: float,
                               t: int) -> Dict[str, float]:
        allocable = max(0, available_flow - eco_reserve - downstream_min)
        sorted_divs = []
        for did in diversion_ids:
            node = self._nodes.get(did)
            if node is not None:
                sorted_divs.append((node.priority, did, node))
        sorted_divs.sort(key=lambda x: x[0])

        result = {}
        remaining = allocable
        for _, did, node in sorted_divs:
            if remaining <= 0:
                result[did] = 0.0
                continue
            actual = min(remaining, node.max_capacity)
            result[did] = actual
            remaining -= actual
            if node.actual_ts is not None and t < len(node.actual_ts):
                node.actual_ts[t] = actual
        return result

    def allocate_at_channel(self, channel_id: str,
                             available_flow: float,
                             eco_reserve: float,
                             downstream_min: float,
                             t: int) -> Dict[str, float]:
        """
        在某个河道节点上分配引水量

        Parameters
        ----------
        channel_id : 干流/非干流节点ID
        available_flow : 节点当前可用流量 万m³
        eco_reserve : 生态保留量 万m³
        downstream_min : 下游最低过境量 万m³
        t : 时段索引

        Returns
        -------
        {diversion_id: actual_diversion_amount}
        """
        div_ids = self._by_channel.get(channel_id, [])
        if not div_ids:
            return {}

        # 可分配量
        allocable = max(0, available_flow - eco_reserve - downstream_min)

        # 按优先级排序（数字小优先）
        sorted_divs = []
        for did in div_ids:
            node = self._nodes.get(did)
            if node is not None:
                sorted_divs.append((node.priority, did, node))
        sorted_divs.sort(key=lambda x: x[0])

        result = {}
        remaining = allocable

        for _, did, node in sorted_divs:
            if remaining <= 0:
                result[did] = 0.0
                continue

            actual = min(remaining, node.max_capacity)
            result[did] = actual
            remaining -= actual

            # 记录
            if node.actual_ts is not None and t < len(node.actual_ts):
                node.actual_ts[t] = actual

        return result
