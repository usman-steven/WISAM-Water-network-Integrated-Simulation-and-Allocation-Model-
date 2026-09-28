"""
water/allocation.py - 水资源配置引擎

根据可用水源和需水量，按优先级或比例进行配置。

配置规则（优先级法）：
  行业优先级：生活 > 生态 > 工业 > 农业
  水源优先级：调水(0) > 本地水(1) > 地下水(2) > 水库(3) > 引水(3)
  
  先满足高优先级行业，再满足低优先级行业
  同一行业内，优先使用高优先级水源

接口（simulator调用）：
  alloc_engine = AllocationEngine(method)
  result = alloc_engine.allocate(demands, sources)
  result.alloc_detail → {sector: {source_id: amount}}
  result.shortages → {sector: shortage}
"""
from dataclasses import dataclass, field
from typing import Dict, List
from config import AllocationMethod, SECTOR_PRIORITY

@dataclass
class WaterSource:
    """单个水源"""
    source_id: str = ""
    source_name: str = ""
    source_type: str = ""
    available: float = 0.0
    priority: int = 1
    loss_rate: float = 0.0
    unit_cost: float = 0.0

@dataclass
class AllocationResult:
    """配置结果"""
    total_demand: float = 0.0
    total_supply: float = 0.0
    total_shortage: float = 0.0
    satisfaction_rate: float = 0.0
    shortages: Dict[str, float] = field(default_factory=dict)
    alloc_detail: Dict[str, Dict[str, float]] = field(default_factory=dict)

class AllocationEngine:
    """水资源配置引擎"""

    def __init__(self, method: AllocationMethod = AllocationMethod.PRIORITY):
        self.method = method

    def allocate(self, demands: Dict[str, float],
                  sources: List[WaterSource]) -> AllocationResult:
        """
        执行水资源配置

        Parameters
        ----------
        demands : {行业: 需水量 万m³}
        sources : 可用水源列表

        Returns
        -------
        AllocationResult
        """
        if self.method == AllocationMethod.PRIORITY:
            return self._priority_allocate(demands, sources)
        elif self.method == AllocationMethod.PROPORTIONAL:
            return self._proportional_allocate(demands, sources)
        else:
            return self._priority_allocate(demands, sources)

    def _priority_allocate(self, demands: Dict[str, float],
                            sources: List[WaterSource]) -> AllocationResult:
        """
        优先级配置法

        步骤：
        1. 水源按优先级排序
        2. 行业按优先级排序（生活>生态>工业>农业）
        3. 逐行业逐水源分配，高优先级行业先用水
        """
        result = AllocationResult()
        result.total_demand = sum(max(0, v) for v in demands.values())

        # 按优先级排序水源
        sorted_sources = sorted(sources, key=lambda s: s.priority)

        # 各水源剩余可用量（扣除输水损失后的有效水量）
        remaining = {}
        for s in sorted_sources:
            effective = s.available * (1.0 - s.loss_rate)
            remaining[s.source_id] = max(0, effective)

        # 按行业优先级排序
        sorted_sectors = sorted(
            demands.keys(),
            key=lambda s: SECTOR_PRIORITY.get(s, 99))

        for sector in sorted_sectors:
            need = max(0, demands.get(sector, 0))
            allocated = {}
            total_got = 0.0

            if need <= 0:
                result.alloc_detail[sector] = {}
                result.shortages[sector] = 0.0
                continue

            for src in sorted_sources:
                if total_got >= need:
                    break
                avail = remaining.get(src.source_id, 0)
                if avail <= 0:
                    continue

                give = min(need - total_got, avail)
                allocated[src.source_id] = give
                remaining[src.source_id] -= give
                total_got += give

            result.alloc_detail[sector] = allocated
            result.shortages[sector] = max(0, need - total_got)

        result.total_supply = sum(
            sum(d.values()) for d in result.alloc_detail.values())
        result.total_shortage = max(0,
            result.total_demand - result.total_supply)

        if result.total_demand > 0:
            result.satisfaction_rate = result.total_supply / result.total_demand
        else:
            result.satisfaction_rate = 1.0

        return result

    def _proportional_allocate(self, demands: Dict[str, float],
                                sources: List[WaterSource]) -> AllocationResult:
        """
        按比例配置法

        所有行业按相同比例削减
        """
        result = AllocationResult()
        result.total_demand = sum(max(0, v) for v in demands.values())

        total_avail = sum(
            s.available * (1.0 - s.loss_rate) for s in sources)

        if result.total_demand <= 0:
            result.satisfaction_rate = 1.0
            for sector in demands:
                result.alloc_detail[sector] = {}
                result.shortages[sector] = 0.0
            return result

        ratio = min(1.0, total_avail / result.total_demand)

        remaining_sources = {}
        for s in sources:
            remaining_sources[s.source_id] = s.available * (1.0 - s.loss_rate)

        for sector, need in demands.items():
            need = max(0, need)
            alloc_amount = need * ratio
            allocated = {}
            got = 0.0

            for s in sources:
                if got >= alloc_amount:
                    break
                avail = remaining_sources.get(s.source_id, 0)
                give = min(alloc_amount - got, avail)
                if give > 0:
                    allocated[s.source_id] = give
                    remaining_sources[s.source_id] -= give
                    got += give

            result.alloc_detail[sector] = allocated
            result.shortages[sector] = max(0, need - got)

        result.total_supply = sum(
            sum(d.values()) for d in result.alloc_detail.values())
        result.total_shortage = max(0,
            result.total_demand - result.total_supply)
        result.satisfaction_rate = ratio

        return result
