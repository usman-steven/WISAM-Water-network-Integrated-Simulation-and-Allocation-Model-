"""
water/demand.py - 需水计算模块

核心思路：
  年需水量 × 当月分配系数 = 月需水量
  分配系数根据每个城市每月的实际气象动态计算：
  
  农业：基于 (PET - 有效降水) 的月分布
    PET高降水少的月份灌溉需求大，降水多的月份需求小
    不同城市气候不同 → 分配自然不同
    
  生活：基于温度偏离年均值
    夏季高温→用水多，冬季→用水少
    
  工业：基本均匀，温度微调
  
  生态：基于PET月分布

调用时机：
  simulator.initialize() 中，气象和需水数据都加载后调用一次
  precompute_monthly_weights() 预计算所有节点所有时步的月需水量
"""
import numpy as np
from typing import Dict, Optional
from config import ModelConfig

class DemandModule:
    """
    需水量月分配与动态调整

    接口：
      simulator.initialize() 调用：
        demand_mod.precompute_monthly_weights(network, n_steps)
    """

    def __init__(self, config: ModelConfig):
        self.config = config

    def precompute_monthly_weights(self, network, n_steps: int) -> int:
        """
        预计算所有需水节点的月分配权重

        基于每个城市关联子流域的实际气象序列计算，
        将年均月值（年/12）重新分配为考虑季节变化的月值。

        Parameters
        ----------
        network : WaterNetwork
        n_steps : 总步数

        Returns
        -------
        int : 调整了多少个需水节点
        """
        from config import NodeType
        from core.nodes import SubbasinNode, DemandNode

        demand_nodes = network.get_nodes(NodeType.DEMAND)
        adjusted_count = 0

        for dm in demand_nodes:
            if not isinstance(dm, DemandNode):
                continue

            # ── 收集该城市关联子流域的平均气象序列 ──
            precip_series = np.zeros(n_steps)
            temp_series = np.full(n_steps, 15.0)
            pet_series = np.zeros(n_steps)
            count = 0

            unit_ids = list(getattr(dm, 'coupled_unit_ids', []) or [])
            for unit_id in unit_ids:
                sb_id = f'SB_{unit_id}'
                sb = network.nodes.get(sb_id)
                if not isinstance(sb, SubbasinNode):
                    continue

                n_sb = n_steps  # 取较短的长度
                if sb.precip is not None:
                    n_use = min(len(sb.precip), n_steps)
                    precip_series[:n_use] += sb.precip[:n_use]
                if sb.temp_mean is not None:
                    n_use = min(len(sb.temp_mean), n_steps)
                    temp_series[:n_use] += sb.temp_mean[:n_use]
                if sb.pet is not None:
                    n_use = min(len(sb.pet), n_steps)
                    pet_series[:n_use] += sb.pet[:n_use]
                count += 1

            if count > 0:
                precip_series /= count
                temp_series /= count
                pet_series /= count
            else:
                # 没有关联子流域，跳过
                continue

            # ── 逐年重新分配月需水 ──
            n_years = n_steps // 12
            for sector in ['生活', '工业', '农业', '生态']:
                if sector not in dm.demands:
                    continue
                if len(dm.demands[sector]) < n_steps:
                    continue

                for yr in range(n_years):
                    t_start = yr * 12
                    t_end = t_start + 12

                    # 该年12个月的气象
                    yr_precip = precip_series[t_start:t_end]
                    yr_temp = temp_series[t_start:t_end]
                    yr_pet = pet_series[t_start:t_end]

                    # 该年的年需水量（12个月之和）
                    yr_demand = np.sum(dm.demands[sector][t_start:t_end])
                    if yr_demand <= 0:
                        continue

                    # 计算12个月的分配权重
                    weights = self._calc_monthly_weights(
                        sector, yr_precip, yr_temp, yr_pet)

                    # 按权重重新分配
                    for m in range(12):
                        dm.demands[sector][t_start + m] = yr_demand * weights[m]

            adjusted_count += 1

        return adjusted_count

    def _calc_monthly_weights(self, sector: str,
                               precip_12: np.ndarray,
                               temp_12: np.ndarray,
                               pet_12: np.ndarray) -> np.ndarray:
        """
        计算某行业某年的12个月分配权重

        Returns
        -------
        12个月的权重（和为1.0）
        """
        if sector == '农业':
            return self._agri_weights(precip_12, pet_12)
        elif sector == '生活':
            return self._domestic_weights(temp_12)
        elif sector == '工业':
            return self._industrial_weights(temp_12)
        elif sector == '生态':
            return self._eco_weights(pet_12)
        else:
            return np.ones(12) / 12.0

    def _agri_weights(self, precip: np.ndarray,
                       pet: np.ndarray) -> np.ndarray:
        """
        农业需水月分配

        灌溉需求 ∝ max(0, PET - 有效降水)
        有效降水 ≈ 降水 × 0.7（部分成为径流）

        华北春季：PET↑降水↓ → 权重大
        江南梅雨季：降水多 → 权重小
        """
        eff_precip = precip * 0.7
        net_demand = np.maximum(0, pet - eff_precip)

        total = np.sum(net_demand)
        if total > 0:
            weights = net_demand / total
        else:
            weights = np.ones(12) / 12.0

        # 最低保障，避免某月完全为0
        weights = np.maximum(weights, 0.02)
        weights /= weights.sum()
        return weights

    def _domestic_weights(self, temp: np.ndarray) -> np.ndarray:
        """
        生活需水月分配

        高温月份→用水多（空调/洗浴/饮水）
        低温月份→用水少

        权重 ∝ 1 + α × (T - T_mean) / T_range
        """
        t_mean = np.mean(temp)
        t_range = max(1.0, np.max(temp) - np.min(temp))

        alpha = 0.4
        factors = 1.0 + alpha * (temp - t_mean) / t_range
        factors = np.maximum(factors, 0.5)
        weights = factors / factors.sum()
        return weights

    def _industrial_weights(self, temp: np.ndarray) -> np.ndarray:
        """
        工业需水月分配

        全年较均匀，夏季冷却水需求略高
        波动幅度比生活和农业小
        """
        t_mean = np.mean(temp)
        t_range = max(1.0, np.max(temp) - np.min(temp))

        alpha = 0.15  # 小幅波动
        factors = 1.0 + alpha * (temp - t_mean) / t_range
        factors = np.maximum(factors, 0.7)
        weights = factors / factors.sum()
        return weights

    def _eco_weights(self, pet: np.ndarray) -> np.ndarray:
        """
        生态需水月分配

        与蒸散发正相关：
        夏季蒸发大→需更多水维持河湖水位
        """
        total = np.sum(pet)
        if total > 0:
            weights = pet / total
        else:
            weights = np.ones(12) / 12.0

        weights = np.maximum(weights, 0.03)
        weights /= weights.sum()
        return weights
