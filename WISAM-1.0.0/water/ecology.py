"""
water/ecology.py - 生态流量检查模块

功能：
  检查入海口/控制断面的实际流量是否满足最低生态流量要求
  记录缺水量

接口（simulator调用）：
  ecology_mod.init_nodes(eco_nodes, n_steps)
  ecology_mod.check(node, actual_flow, month, t)
"""
import numpy as np
from core.nodes import OceanNode, EcoControlNode

class EcologyModule:
    """生态流量检查模块"""

    def __init__(self):
        pass

    def init_nodes(self, eco_nodes: list, n_steps: int):
        """
        初始化生态断面的结果序列

        Parameters
        ----------
        eco_nodes : OceanNode 和 EcoControlNode 的列表
        n_steps : 序列长度
        """
        for node in eco_nodes:
            if isinstance(node, OceanNode):
                node.actual_outflow_ts = np.zeros(n_steps)
                node.eco_deficit_ts = np.zeros(n_steps)
            elif isinstance(node, EcoControlNode):
                node.actual_flow_ts = np.zeros(n_steps)
                node.eco_deficit_ts = np.zeros(n_steps)

    def check(self, node, actual_flow: float,
              month: int, t: int):
        """
        检查某断面是否满足生态流量

        Parameters
        ----------
        node : OceanNode 或 EcoControlNode
        actual_flow : 实际流量 万m³/月
        month : 月份 1-12
        t : 时段索引
        """
        min_flow = node.get_min_flow(month)
        deficit = max(0, min_flow - actual_flow)

        if isinstance(node, OceanNode):
            if (node.actual_outflow_ts is not None
                    and t < len(node.actual_outflow_ts)):
                node.actual_outflow_ts[t] = actual_flow
                node.eco_deficit_ts[t] = deficit

        elif isinstance(node, EcoControlNode):
            if (node.actual_flow_ts is not None
                    and t < len(node.actual_flow_ts)):
                node.actual_flow_ts[t] = actual_flow
                node.eco_deficit_ts[t] = deficit
