"""core/nodes.py - 所有节点类型定义"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import numpy as np
from config import NodeType

@dataclass
class Node:
    """节点基类"""
    id: str = ""
    name: str = ""
    node_type: NodeType = NodeType.SUBBASIN
    x: float = 0.0
    y: float = 0.0
    basin: str = ""
    water_zone_l2: str = ""
    water_zone_l3: str = ""
    province: str = ""

@dataclass
class SubbasinNode(Node):
    """非干流计算单元，负责产流"""
    node_type: NodeType = NodeType.SUBBASIN
    area: float = 0.0
    unit_id: str = ""
    city_code: str = ""
    city_name: str = ""
    is_high_altitude: bool = False

    # ABCD参数
    param_a: float = 0.95
    param_b: float = 200.0
    param_c: float = 0.5
    param_d: float = 0.15

    # GR2M参数
    param_x1: float = 400.0
    param_x2: float = 0.9

    # 产流系数法
    runoff_coeff: float = 0.35
    et_reduction: float = 0.3
    baseflow_index: float = 0.3

    # 挂载设施
    reservoir_ids: List[str] = field(default_factory=list)
    lake_ids: List[str] = field(default_factory=list)
    diversion_ids: List[str] = field(default_factory=list)

    # 气象输入
    precip: Optional[np.ndarray] = None
    temp_mean: Optional[np.ndarray] = None
    temp_max: Optional[np.ndarray] = None
    temp_min: Optional[np.ndarray] = None
    pet: Optional[np.ndarray] = None

    # 产流结果
    total_runoff: Optional[np.ndarray] = None
    surface_runoff: Optional[np.ndarray] = None
    baseflow: Optional[np.ndarray] = None
    gw_recharge: Optional[np.ndarray] = None
   
    # 预计算产流序列
    use_precomputed: bool = False
    precomputed_runoff: Optional[np.ndarray] = None
    precomputed_gw_recharge: Optional[np.ndarray] = None
    precomputed_baseflow: Optional[np.ndarray] = None

    # ABCD内部状态
    soil_storage: float = 100.0
    gw_storage: float = 50.0

    # 生态基流
    eco_priority: int = 1                           # 0=最高（强制保证），3=最低
    eco_baseflow: np.ndarray = field(               # 12个月的生态基流 万m³/月
        default_factory=lambda: np.zeros(12))
    mean_annual_runoff: float = 0.0                 # 多年平均年径流 万m³（蒙大拿法用）
    eco_baseflow_ts: Optional[np.ndarray] = None    # 实际保留的生态基流序列
    eco_deficit_ts: Optional[np.ndarray] = None     # 生态基流缺水序列


@dataclass
class RiverChannelNode(Node):
    """干流计算单元，负责汇流"""
    node_type: NodeType = NodeType.RIVER_CHANNEL
    area: float = 0.0
    unit_id: str = ""
    river_name: str = ""
    channel_node_id: str = ""
    loss_rate: float = 0.02

    reservoir_ids: List[str] = field(default_factory=list)
    lake_ids: List[str] = field(default_factory=list)
    diversion_ids: List[str] = field(default_factory=list)

    inflow_series: Optional[np.ndarray] = None
    outflow_series: Optional[np.ndarray] = None
    routing_storage: float = 0.0
    routing_residence_months: float = 0.0
    routing_storage_ts: Optional[np.ndarray] = None

    # 生态基流
    eco_priority: int = 1
    eco_baseflow: np.ndarray = field(
        default_factory=lambda: np.zeros(12))
    mean_annual_flow: float = 0.0                   # 多年平均过境流量 万m³
    eco_baseflow_ts: Optional[np.ndarray] = None
    eco_deficit_ts: Optional[np.ndarray] = None


@dataclass
class ReservoirNode(Node):
    """水库节点"""
    node_type: NodeType = NodeType.RESERVOIR
    status: str = "built"
    online_year: int = 1900
    retire_year: Optional[int] = None
    scenario_group: str = "all"
    total_capacity: float = 0.0
    normal_storage: float = 0.0
    flood_limit_storage: float = 0.0
    dead_storage: float = 0.0
    initial_storage: float = 0.0
    current_storage: float = 0.0

    monthly_evap: np.ndarray = field(
        default_factory=lambda: np.full(12, 50.0))
    seepage_rate: float = 0.0
    area_coeff: float = 0.001
    flood_season_months: List[int] = field(
        default_factory=lambda: [6, 7, 8, 9])

    # 挂载信息
    channel_node_id: str = ""
    order_in_channel: int = 1

    # 结果序列
    storage_ts: Optional[np.ndarray] = None
    inflow_ts: Optional[np.ndarray] = None
    release_ts: Optional[np.ndarray] = None
    spill_ts: Optional[np.ndarray] = None
    target_storage_ts: Optional[np.ndarray] = None
    release_target_ts: Optional[np.ndarray] = None

    def available_storage(self) -> float:
        """可用蓄量（当前蓄量 - 死库容）"""
        return max(0, self.current_storage - self.dead_storage)

    def storage_ratio(self) -> float:
        """蓄水率（占兴利库容的比例）"""
        usable = self.normal_storage - self.dead_storage
        if usable <= 0:
            return 0.0
        return (self.current_storage - self.dead_storage) / usable

    def water_area(self) -> float:
        """估算水面面积"""
        if self.current_storage <= 0:
            return 0.0
        return self.area_coeff * (self.current_storage ** 0.667)

    def evap_loss(self, month: int) -> float:
        """月蒸发损失 万m³"""
        return self.water_area() * self.monthly_evap[month - 1] * 0.0001

    def max_storage_for_month(self, month: int) -> float:
        """当月允许最大蓄量"""
        if month in self.flood_season_months:
            return self.flood_limit_storage
        return self.normal_storage

    def get_min_flow(self, month: int) -> float:
        return 0.0

    def is_online(self, year: int) -> bool:
        if year < int(self.online_year or 1900):
            return False
        if self.retire_year and year > int(self.retire_year):
            return False
        return True

@dataclass
class LakeNode(Node):
    """湖泊节点"""
    node_type: NodeType = NodeType.LAKE
    max_storage: float = 0.0
    normal_storage: float = 0.0
    dead_storage: float = 0.0
    eco_min_storage: float = 0.0
    outflow_capacity: float = 0.0
    current_storage: float = 0.0

    monthly_evap: np.ndarray = field(
        default_factory=lambda: np.full(12, 80.0))
    area_coeff: float = 0.001
    channel_node_id: str = ""

    # 结果序列
    storage_ts: Optional[np.ndarray] = None
    inflow_ts: Optional[np.ndarray] = None
    outflow_ts: Optional[np.ndarray] = None
    release_ts: Optional[np.ndarray] = None
    spill_ts: Optional[np.ndarray] = None

@dataclass
class GroundwaterNode(Node):
    """城市地下水供水源节点."""
    node_type: NodeType = NodeType.GROUNDWATER
    city_code: str = ""
    city_name: str = ""
    exploitable_annual: float = 0.0
    initial_over: float = 0.0
    over_decay: float = 0.98
    demand_node_id: str = ""

@dataclass
class DemandNode(Node):
    """
    需水节点。

    现行模型优先按“三级流域-地市”建立需求节点。一个 DemandNode
    通常对应一个非干流水文计算单元，city_code 保留所属地市代码。
    """
    node_type: NodeType = NodeType.DEMAND
    city_code: str = ""
    city_name: str = ""
    unit_id: str = ""
    coupled_unit_ids: List[str] = field(default_factory=list)
    demand_area_weight: float = 1.0
    demand_sector_weights: Dict[str, float] = field(default_factory=dict)
    demand_split_basis: Dict[str, str] = field(default_factory=dict)
    water_zone_id: str = ""
    water_zone_name: str = ""
    basin_l2: str = ""

    # 各行业需水/配置/缺水序列
    demands: Dict[str, np.ndarray] = field(default_factory=dict)
    allocation: Dict[str, np.ndarray] = field(default_factory=dict)
    shortage: Dict[str, np.ndarray] = field(default_factory=dict)
    source_supply: Dict[str, np.ndarray] = field(default_factory=dict)
    return_flow_by_sector: Dict[str, np.ndarray] = field(default_factory=dict)
    consumption: Dict[str, np.ndarray] = field(default_factory=dict)

    # 退水率
    return_ratios: Dict[str, float] = field(default_factory=lambda: {
        '生活': 0.7, '工业': 0.6, '农业': 0.3, '生态': 0.0})

    # 地下水参数
    gw_exploitable_annual: float = 0.0
    gw_cumulative_over: float = 0.0
    gw_over_decay: float = 0.98

    def total_demand(self, t: int) -> float:
        """当月总需水"""
        total = 0.0
        for s, arr in self.demands.items():
            if t < len(arr):
                total += arr[t]
        return total

    def return_flow(self, t: int) -> float:
        """当月退水量"""
        if self.return_flow_by_sector:
            ret = 0.0
            for arr in self.return_flow_by_sector.values():
                if t < len(arr):
                    ret += arr[t]
            return ret
        ret = 0.0
        for s, arr in self.allocation.items():
            if t < len(arr):
                ratio = self.return_ratios.get(s, 0.5)
                ret += arr[t] * ratio
        return ret

@dataclass
class RiverDiversionNode(Node):
    """河道引水口"""
    node_type: NodeType = NodeType.RIVER_DIVERSION
    max_capacity: float = 0.0
    priority: int = 1
    diversion_type: str = ""
    supply_to: Dict[str, float] = field(default_factory=dict)
    channel_node_id: str = ""
    eco_reserve_ratio: float = 0.0
    eco_reserve_min_flow: float = 0.0
    downstream_min_ratio: float = 0.0
    downstream_min_flow: float = 0.0
    actual_ts: Optional[np.ndarray] = None

@dataclass
class OceanNode(Node):
    """入海口"""
    node_type: NodeType = NodeType.OCEAN
    river_name: str = ""
    min_eco_flow: np.ndarray = field(
        default_factory=lambda: np.zeros(12))
    actual_outflow_ts: Optional[np.ndarray] = None
    eco_deficit_ts: Optional[np.ndarray] = None

    def get_min_flow(self, month: int) -> float:
        return self.min_eco_flow[month - 1]

@dataclass
class EcoControlNode(Node):
    """生态控制断面"""
    node_type: NodeType = NodeType.ECO_CONTROL
    river_name: str = ""
    min_eco_flow: np.ndarray = field(
        default_factory=lambda: np.zeros(12))
    channel_node_id: str = ""
    actual_flow_ts: Optional[np.ndarray] = None
    eco_deficit_ts: Optional[np.ndarray] = None

    def get_min_flow(self, month: int) -> float:
        return self.min_eco_flow[month - 1]

@dataclass
class JunctionNode(Node):
    """汇合节点"""
    node_type: NodeType = NodeType.JUNCTION
