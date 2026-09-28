"""
water/reservoir.py - 水库调蓄模块

功能：
1. 单库调度 step()
2. 串联多库调度 step_cascade()

调度规则：
  蓄水率高 → 多放水（供水系数大）
  蓄水率低 → 少放水（保蓄保死库容）
  汛期     → 降到汛限水位（防洪）

水量平衡：
  V(t+1) = V(t) + 入库 - 下泄 - 调水取水 - 蒸发 - 渗漏
  超过最大蓄量 → 弃水
  低于死库容   → 减少下泄

接口（simulator调用）：
  reservoir_mod.init_reservoir(res, n_steps=n)
  reservoir_mod.step_cascade(ids, nodes, t, month, inflow, demands, tw)
"""
import numpy as np
from typing import Dict, List, Tuple
from core.nodes import ReservoirNode

class ReservoirModule:
    """水库调蓄模块"""

    def __init__(self, config=None):
        self.config = config

    def init_reservoir(self, res: ReservoirNode,
                        ratio: float = None,
                        n_steps: int = 732):
        """
        初始化水库状态和结果序列

        Parameters
        ----------
        res : 水库节点
        ratio : 初始蓄水率（覆盖节点自身值），None则用节点已有值
        n_steps : 结果序列长度
        """
        if ratio is not None:
            usable = res.normal_storage - res.dead_storage
            res.initial_storage = res.dead_storage + usable * ratio
            res.current_storage = res.initial_storage
        elif res.current_storage <= 0:
            res.current_storage = res.initial_storage

        # 确保初始蓄量合理
        if res.current_storage <= 0:
            usable = res.normal_storage - res.dead_storage
            res.current_storage = res.dead_storage + usable * 0.5

        res.storage_ts = np.zeros(n_steps)
        res.inflow_ts = np.zeros(n_steps)
        res.release_ts = np.zeros(n_steps)
        res.spill_ts = np.zeros(n_steps)
        res.target_storage_ts = np.zeros(n_steps)
        res.release_target_ts = np.zeros(n_steps)
        res.evap_loss_ts = np.zeros(n_steps)
        res.seepage_loss_ts = np.zeros(n_steps)

    def step_cascade(self, reservoir_ids: List[str],
                      network_nodes: dict,
                      t: int, month: int,
                      initial_inflow: float,
                      demands: Dict[str, float],
                      transfer_withdraws: Dict[str, float]
                      ) -> Tuple[float, Dict[str, dict]]:
        """
        串联多水库一步调度

        上游水库的出流 = 下游水库的入流

        Parameters
        ----------
        reservoir_ids : 按上下游排序的水库ID列表
        network_nodes : 网络节点字典
        t : 时段
        month : 月份
        initial_inflow : 最上游入流
        demands : {res_id: demand} 各库下游需水
        transfer_withdraws : {res_id: tw} 各库调水取水

        Returns
        -------
        (final_outflow, {res_id: step_result})
        """
        results = {}
        current_flow = initial_inflow

        for res_id in reservoir_ids:
            res = network_nodes.get(res_id)
            if not isinstance(res, ReservoirNode):
                continue

            demand = demands.get(res_id, 0)
            tw = transfer_withdraws.get(res_id, 0)

            r = self.step(res, t, month, current_flow, demand, tw)
            results[res_id] = r
            current_flow = r['outflow']

        return current_flow, results

    def step(self, res: ReservoirNode, t: int, month: int,
             inflow: float, demand: float = 0,
             transfer_withdraw: float = 0) -> dict:
        """配置驱动的简化调配型水库调度。"""
        inflow = max(0.0, inflow)
        demand = max(0.0, demand)
        transfer_withdraw = max(0.0, transfer_withdraw)

        sr = res.storage_ratio()
        conservative_ratio = getattr(
            self.config, 'reservoir_conservative_storage_ratio', 0.45)
        min_supply_ratio = getattr(
            self.config, 'reservoir_min_supply_storage_ratio', 0.20)
        emergency_supply_ratio = getattr(
            self.config, 'reservoir_emergency_supply_ratio', 0.30)
        supply_target_ratio = getattr(
            self.config, 'reservoir_supply_target_storage_ratio', 0.75)
        refill_target_ratio = getattr(
            self.config, 'reservoir_refill_target_storage_ratio', 0.90)
        excess_release_factor = getattr(
            self.config, 'reservoir_excess_release_factor', 1.00)
        release_buffer = getattr(
            self.config, 'reservoir_release_buffer', 1.05)
        flood_release_factor = getattr(
            self.config, 'reservoir_flood_release_factor', 0.60)

        if sr >= conservative_ratio:
            support_ratio = 1.0
        elif sr <= min_supply_ratio:
            support_ratio = emergency_supply_ratio
        else:
            span = max(1e-6, conservative_ratio - min_supply_ratio)
            frac = (sr - min_supply_ratio) / span
            support_ratio = (
                emergency_supply_ratio +
                (1.0 - emergency_supply_ratio) * frac)

        evap = res.evap_loss(month)
        pre_release_volume = max(
            0.0,
            res.current_storage + inflow - transfer_withdraw - evap - res.seepage_rate
        )

        usable_storage = max(0.0, res.normal_storage - res.dead_storage)
        flood_months = set(res.flood_season_months or [])
        refill_start = (max(flood_months) + 1) if flood_months else 10
        if month in flood_months:
            target_storage = res.flood_limit_storage
        elif month >= refill_start:
            target_storage = res.dead_storage + usable_storage * refill_target_ratio
        else:
            target_storage = res.dead_storage + usable_storage * supply_target_ratio
        target_storage = min(target_storage, res.max_storage_for_month(month))
        target_storage = max(res.dead_storage, target_storage)

        support_release = demand * support_ratio * release_buffer
        storage_excess = max(0.0, pre_release_volume - target_storage)
        target = max(support_release, storage_excess * excess_release_factor)

        if month in flood_months:
            limit = res.flood_limit_storage
            excess = max(0.0, res.current_storage + inflow - limit)
            target = max(target, excess * flood_release_factor)

        available = max(0.0, pre_release_volume - res.dead_storage)
        target = min(target, available)

        new_v = (
            res.current_storage + inflow - target -
            transfer_withdraw - evap - res.seepage_rate
        )

        spill = 0.0
        max_v = res.max_storage_for_month(month)
        if new_v > max_v:
            spill = new_v - max_v
            new_v = max_v

        actual_release = target
        if new_v < res.dead_storage:
            deficit = res.dead_storage - new_v
            actual_release = max(0.0, target - deficit)
            new_v = res.dead_storage

        res.current_storage = max(0.0, new_v)

        if res.storage_ts is not None and t < len(res.storage_ts):
            res.storage_ts[t] = res.current_storage
            res.inflow_ts[t] = inflow
            res.release_ts[t] = actual_release
            res.spill_ts[t] = spill
            if getattr(res, 'evap_loss_ts', None) is not None:
                res.evap_loss_ts[t] = evap
            if getattr(res, 'seepage_loss_ts', None) is not None:
                res.seepage_loss_ts[t] = res.seepage_rate
            if res.target_storage_ts is not None:
                res.target_storage_ts[t] = target_storage
            if res.release_target_ts is not None:
                res.release_target_ts[t] = target

        return {
            'release': actual_release,
            'spill': spill,
            'storage': res.current_storage,
            'outflow': actual_release + spill,
        }
