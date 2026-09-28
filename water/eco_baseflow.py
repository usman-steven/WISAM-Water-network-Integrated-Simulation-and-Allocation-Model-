"""
water/eco_baseflow.py - 生态基流计算模块

蒙大拿法（Tennant法, 1976）：
  生态基流 = 多年平均流量 × 百分比
  
  不同季节采用不同百分比：
    汛期（6-10月）：较高比例（河道需要更多水维持生态）
    非汛期（11-5月）：较低比例

  不同等级：
    excellent: 非汛期40%, 汛期60%
    good:      非汛期20%, 汛期40%
    fair:      非汛期10%, 汛期20%
    poor:      非汛期5%,  汛期10%

优先级机制：
  eco_priority=0: 强制约束，生态基流不可侵占
  eco_priority=1: 优先生态，极端干旱可削减至50%
  eco_priority=2: 平衡，需水和生态各50%
  eco_priority=3: 优先需水，仅剩余水量留给生态

使用方式：
  eco_bf = EcoBaseflowModule(config)
  
  # 初始化时（预热期结束后）
  eco_bf.compute_eco_baseflow(network, n_steps)
  
  # 每步汇流时
  eco_reserve, available = eco_bf.split_flow(
      node, total_flow, month, t)
"""
import numpy as np
from typing import Dict, Optional
from config import (ModelConfig, TENNANT_LEVELS, FLOOD_SEASON_MONTHS,
                     DEFAULT_TENNANT_LEVEL, NodeType)
from core.nodes import SubbasinNode, RiverChannelNode

class EcoBaseflowModule:
    """
    生态基流计算与管理

    两个阶段：
    1. 初始化阶段：根据预热期数据计算多年平均流量 → 蒙大拿法确定生态基流
    2. 模拟阶段：  每步根据优先级分配流量
    """

    def __init__(self, config: ModelConfig):
        self.config = config
        self.tennant_level = config.tennant_level
        self.warmup_years = config.eco_warmup_years
        self.default_priority = config.default_eco_priority

        # 获取百分比
        level_params = TENNANT_LEVELS.get(
            self.tennant_level, TENNANT_LEVELS['good'])
        self.dry_ratio = level_params[0]   # 非汛期百分比
        self.wet_ratio = level_params[1]   # 汛期百分比

    def compute_eco_baseflow(self, network, n_steps: int):
        """
        计算所有节点的生态基流

        调用时机：simulator.initialize() 末尾

        策略：
        1. 如果有预热期数据（前N年的产流），用实际数据计算多年均值
        2. 如果没有（第一次运行），用 P × runoff_coeff × area 粗略估算
        3. 蒙大拿法：生态基流 = 多年均流量 × 百分比
        """
        warmup_steps = self.warmup_years * 12
        computed_sb = 0
        computed_ch = 0

        # ══════ 非干流单元 ══════
        for sb in network.get_nodes(NodeType.SUBBASIN):
            if not isinstance(sb, SubbasinNode):
                continue

            # 估算多年平均年径流
            if sb.total_runoff is not None and warmup_steps > 0:
                # 用预热期数据（如果已运行过）
                n_use = min(warmup_steps, len(sb.total_runoff))
                if np.sum(sb.total_runoff[:n_use]) > 0:
                    # 月均径流 × 12 = 年均径流
                    sb.mean_annual_runoff = float(
                        np.mean(sb.total_runoff[:n_use])) * 12
                else:
                    sb.mean_annual_runoff = self._estimate_runoff(sb)
            else:
                sb.mean_annual_runoff = self._estimate_runoff(sb)

            # 蒙大拿法计算12个月生态基流
            monthly_mean = sb.mean_annual_runoff / 12.0
            eco_flow = np.zeros(12)
            for m in range(12):
                month = m + 1
                if month in FLOOD_SEASON_MONTHS:
                    eco_flow[m] = monthly_mean * self.wet_ratio
                else:
                    eco_flow[m] = monthly_mean * self.dry_ratio

            sb.eco_baseflow = eco_flow

            # 设置优先级（如果节点没有自定义过）
            if sb.eco_priority == 1:  # 还是默认值
                sb.eco_priority = self.default_priority

            # 初始化结果序列
            sb.eco_baseflow_ts = np.zeros(n_steps)
            sb.eco_deficit_ts = np.zeros(n_steps)

            computed_sb += 1

        # ══════ 干流单元 ══════
        channel_flow_cache = {}
        for ch in network.get_nodes(NodeType.RIVER_CHANNEL):
            if not isinstance(ch, RiverChannelNode):
                continue

            # 干流的多年平均过境流量：
            # 暂时用上游子流域的汇总估算
            ch.mean_annual_flow = self._estimate_channel_flow(
                ch, network, cache=channel_flow_cache, visiting=set())

            monthly_mean = ch.mean_annual_flow / 12.0
            eco_flow = np.zeros(12)
            for m in range(12):
                month = m + 1
                if month in FLOOD_SEASON_MONTHS:
                    eco_flow[m] = monthly_mean * self.wet_ratio
                else:
                    eco_flow[m] = monthly_mean * self.dry_ratio

            ch.eco_baseflow = eco_flow

            if ch.eco_priority == 1:
                ch.eco_priority = self.default_priority

            ch.eco_baseflow_ts = np.zeros(n_steps)
            ch.eco_deficit_ts = np.zeros(n_steps)

            computed_ch += 1

        return computed_sb, computed_ch

    def update_eco_baseflow(self, network, n_steps: int,
                             current_step: int):
        """
        动态更新生态基流（可选）

        在模拟一段时间后，用实际径流数据更新多年均值。
        推荐在每年年底调用。
        """
        if current_step < 60:  # 至少5年数据
            return

        for sb in network.get_nodes(NodeType.SUBBASIN):
            if not isinstance(sb, SubbasinNode):
                continue
            if sb.total_runoff is None:
                continue

            # 用已有的模拟数据重新计算多年均值
            data = sb.total_runoff[:current_step]
            if len(data) >= 12:
                sb.mean_annual_runoff = float(np.mean(data)) * 12

                monthly_mean = sb.mean_annual_runoff / 12.0
                for m in range(12):
                    month = m + 1
                    if month in FLOOD_SEASON_MONTHS:
                        sb.eco_baseflow[m] = monthly_mean * self.wet_ratio
                    else:
                        sb.eco_baseflow[m] = monthly_mean * self.dry_ratio

    def split_flow(self, node, total_flow: float,
                    month: int, t: int) -> tuple:
        """
        根据优先级分割流量为"生态保留"和"可用"

        Parameters
        ----------
        node : SubbasinNode 或 RiverChannelNode
        total_flow : 当步总流量 万m³
        month : 月份 1-12
        t : 时段索引

        Returns
        -------
        (eco_reserve, available_for_use)
          eco_reserve: 生态保留量 万m³
          available_for_use: 可供取用的量 万m³

        优先级逻辑：
          priority=0: eco_reserve = min(total, eco_baseflow)
                      available = total - eco_reserve
                      → 生态基流完全不可侵占

          priority=1: eco_reserve = min(total, eco_baseflow)
                      但极端干旱时（total < eco_baseflow×0.5）允许削减
                      available = total - eco_reserve
                      → 基本保证生态，极端情况可削减

          priority=2: eco_reserve = min(total, eco_baseflow) × 0.5
                      available = total - eco_reserve
                      → 生态和需水各让一半

          priority=3: eco_reserve = max(0, total - demand_estimate)
                      → 先满足需水，剩余才给生态
        """
        eco_need = node.eco_baseflow[month - 1]
        priority = node.eco_priority
        total_flow = max(0, total_flow)

        if priority == 0:
            # 强制约束：生态基流不可侵占
            eco_reserve = min(total_flow, eco_need)
            available = total_flow - eco_reserve

        elif priority == 1:
            # 高优先级：基本保证，极端可削减
            if total_flow >= eco_need:
                eco_reserve = eco_need
            elif total_flow >= eco_need * 0.5:
                # 流量不足但不算极端：保留全部流量的生态部分
                eco_reserve = eco_need
                eco_reserve = min(eco_reserve, total_flow * 0.8)
            else:
                # 极端干旱：保留50%的生态需求
                eco_reserve = min(total_flow, eco_need * 0.5)
            available = max(0, total_flow - eco_reserve)

        elif priority == 2:
            # 平衡：各让一半
            eco_reserve = min(total_flow, eco_need * 0.5)
            available = max(0, total_flow - eco_reserve)

        else:
            # 低优先级（priority>=3）：先满足需水
            # 生态基流只从剩余中保留
            eco_reserve = 0
            available = total_flow

        # 记录
        if hasattr(node, 'eco_baseflow_ts') and node.eco_baseflow_ts is not None:
            if t < len(node.eco_baseflow_ts):
                node.eco_baseflow_ts[t] = eco_reserve
        if hasattr(node, 'eco_deficit_ts') and node.eco_deficit_ts is not None:
            if t < len(node.eco_deficit_ts):
                node.eco_deficit_ts[t] = max(0, eco_need - eco_reserve)

        return eco_reserve, available

    def post_allocation_eco(self, node, total_flow: float,
                             actual_use: float,
                             month: int, t: int) -> float:
        """
        低优先级（priority>=3）时，在需水配置后检查生态

        Parameters
        ----------
        node : 节点
        total_flow : 总流量
        actual_use : 实际取水量
        month : 月份
        t : 时段

        Returns
        -------
        eco_reserve : 实际保留的生态基流
        """
        if node.eco_priority < 3:
            return 0  # 已在split_flow中处理

        remaining = max(0, total_flow - actual_use)
        eco_need = node.eco_baseflow[month - 1]
        eco_reserve = min(remaining, eco_need)

        if hasattr(node, 'eco_baseflow_ts') and node.eco_baseflow_ts is not None:
            if t < len(node.eco_baseflow_ts):
                node.eco_baseflow_ts[t] = eco_reserve
        if hasattr(node, 'eco_deficit_ts') and node.eco_deficit_ts is not None:
            if t < len(node.eco_deficit_ts):
                node.eco_deficit_ts[t] = max(0, eco_need - eco_reserve)

        return eco_reserve

    # ══════════════ 内部估算方法 ══════════════

    def _estimate_runoff(self, sb: SubbasinNode) -> float:
        """
        粗略估算多年平均年径流（无实测数据时）

        用 年均降水 × 产流系数 × 面积 估算
        """
        if sb.precip is not None and len(sb.precip) >= 12:
            annual_P = float(np.mean(sb.precip)) * 12  # mm/年
        else:
            annual_P = 500  # 默认

        # mm → 万m³: 1mm × 1km² = 0.1 万m³
        annual_runoff = annual_P * sb.runoff_coeff * sb.area * 0.1
        return max(0, annual_runoff)

    def _estimate_channel_flow(self, ch: RiverChannelNode,
                                 network,
                                 cache: Optional[Dict[str, float]] = None,
                                 visiting: Optional[set] = None) -> float:
        """
        估算干流节点的多年平均过境流量

        递推汇总上游子流域和上游干流的自然来水，并考虑连接损失。
        """
        from config import LinkType
        if cache is None:
            cache = {}
        if visiting is None:
            visiting = set()
        if ch.id in cache:
            return cache[ch.id]
        if ch.id in visiting:
            return 0.0

        visiting.add(ch.id)
        total = 0.0

        for link in network.upstream_links(ch.id):
            if link.link_type not in (LinkType.RIVER, LinkType.TRIBUTARY):
                continue
            loss_factor = max(0.0, 1.0 - float(getattr(link, 'loss_rate', 0.0) or 0.0))
            up_node = network.nodes.get(link.from_node)
            if isinstance(up_node, SubbasinNode):
                inflow = float(getattr(up_node, 'mean_annual_runoff', 0.0) or 0.0)
                if inflow <= 0:
                    inflow = self._estimate_runoff(up_node)
                total += inflow * loss_factor
            elif isinstance(up_node, RiverChannelNode):
                inflow = self._estimate_channel_flow(
                    up_node, network, cache=cache, visiting=visiting)
                total += inflow * loss_factor

        visiting.remove(ch.id)

        if total <= 0:
            total = max(0.0, ch.area * 300 * 0.3 * 0.1)

        cache[ch.id] = max(0.0, total)
        return cache[ch.id]
