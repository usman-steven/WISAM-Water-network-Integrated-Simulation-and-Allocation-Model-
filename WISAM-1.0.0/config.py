"""config.py - 全局配置与枚举"""
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict
import pandas as pd

from model_metadata import DEFAULT_NETWORK_NAME

class NodeType(Enum):
    SUBBASIN = 'subbasin'
    RIVER_CHANNEL = 'river_channel'
    RESERVOIR = 'reservoir'
    LAKE = 'lake'
    GROUNDWATER = 'groundwater'
    DEMAND = 'demand'
    RIVER_DIVERSION = 'river_diversion'
    OCEAN = 'ocean'
    ECO_CONTROL = 'eco_control'
    JUNCTION = 'junction'

class LinkType(Enum):
    RIVER = 'river'
    TRIBUTARY = 'tributary'
    SUPPLY_LOCAL = 'supply_local'
    SUPPLY_RESERVOIR = 'supply_reservoir'
    SUPPLY_LAKE = 'supply_lake'
    SUPPLY_GROUNDWATER = 'supply_groundwater'
    SUPPLY_DIVERSION = 'supply_diversion'
    SUPPLY_TRANSFER = 'supply_transfer'
    TRANSFER = 'transfer'
    RETURN_FLOW = 'return_flow'

class Sector(Enum):
    DOMESTIC = '生活'
    INDUSTRIAL = '工业'
    AGRICULTURAL = '农业'
    ECOLOGICAL = '生态'

class SourceType(Enum):
    RESERVOIR = 'reservoir'
    RIVER = 'river'
    LAKE = 'lake'
    GROUNDWATER = 'groundwater'
    LOCAL = 'local'
    TRANSFER = 'transfer'

class RunoffMethod(Enum):
    ABCD = 'abcd'
    GR2M = 'gr2m'
    COEFFICIENT = 'coefficient'

class ETMethod(Enum):
    HARGREAVES = 'hargreaves'
    PENMAN_MONTEITH = 'penman_monteith'
    INPUT = 'input'

class AllocationMethod(Enum):
    PRIORITY = 'priority'
    PROPORTIONAL = 'proportional'

class RunMode(Enum):
    SIMULATION = 'simulation'
    CALIBRATION = 'calibration'
    NATURAL = 'natural'

# 模型过程开关中心注册表。
# 所有过程开关都统一放在这里维护：
#   description：中文说明
#   full：完整水网调配模式默认值
#   natural：天然水循环模式默认值
# 开关值统一采用 1/0，1=开启，0=关闭；True/False 会自动转换为 1/0。
PROCESS_SWITCH_REGISTRY = {
    'enable_snowmelt': {
        'description': '融雪产流过程',
        'full': 1,
        'natural': 1,
    },
    'enable_channel_routing': {
        'description': '河道汇流/调蓄过程',
        'full': 1,
        'natural': 1,
    },
    'enable_reservoirs': {
        'description': '水库调蓄过程',
        'full': 1,
        'natural': 0,
    },
    'enable_lakes': {
        'description': '湖泊调蓄过程',
        'full': 1,
        'natural': 0,
    },
    'enable_diversions': {
        'description': '引水取水过程',
        'full': 0,
        'natural': 0,
    },
    'enable_transfers': {
        'description': '跨区调水过程',
        'full': 1,
        'natural': 0,
    },
    'enable_demands': {
        'description': '需水与供水配置过程',
        'full': 1,
        'natural': 0,
    },
    'enable_dynamic_demand': {
        'description': '需水月内动态分配过程',
        'full': 1,
        'natural': 0,
    },
    'enable_return_flows': {
        'description': '用水退水过程',
        'full': 1,
        'natural': 0,
    },
    'enable_groundwater_allocation': {
        'description': '地下水供水配置过程',
        'full': 1,
        'natural': 0,
    },
    'enable_gw_overexploit': {
        'description': '地下水超采更新过程',
        'full': 1,
        'natural': 0,
    },
    'enable_water_rights': {
        'description': '水权/分水方案约束过程',
        'full': 0,
        'natural': 0,
    },
    'enable_historical_supply_closure': {
        'description': '历史实际用水供水闭合项',
        'full': 0,
        'natural': 0,
    },
    'enable_historical_irrigation_canal_buffer': {
        'description': '历史灌区渠系季节调蓄/取水可达性过程',
        'full': 0,
        'natural': 0,
    },
    'include_planned_infrastructure': {
        'description': '规划工程参与模拟',
        'full': 1,
        'natural': 0,
    },
}

PROCESS_SWITCH_KEYS = tuple(PROCESS_SWITCH_REGISTRY.keys())
PROCESS_SWITCH_DESCRIPTIONS = {
    key: meta['description']
    for key, meta in PROCESS_SWITCH_REGISTRY.items()
}
RUN_PRESET_SWITCHES = {
    preset: {
        key: meta[preset]
        for key, meta in PROCESS_SWITCH_REGISTRY.items()
    }
    for preset in ('full', 'natural')
}

RUN_PRESET_ALIASES = {
    '': 'full',
    'simulation': 'full',
    'managed': 'full',
    'complete': 'full',
    'full': 'full',
    'natural': 'natural',
    'natural_only': 'natural',
    'hydro_only': 'natural',
    'hydrology': 'natural',
}

DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

SECTOR_PRIORITY = {'生活': 1, '生态': 2, '工业': 3, '农业': 4}

@dataclass
class TimeConfig:
    start_year: int = 1951
    end_year: int = 2024
    _time_index_cache: Optional[pd.DatetimeIndex] = field(default=None, init=False, repr=False)
    _time_index_cache_key: Optional[tuple] = field(default=None, init=False, repr=False)

    @property
    def n_steps(self) -> int:
        return (self.end_year - self.start_year + 1) * 12

    @property
    def n_years(self) -> int:
        return self.end_year - self.start_year + 1

    @property
    def time_index(self) -> pd.DatetimeIndex:
        key = (int(self.start_year), int(self.end_year))
        if self._time_index_cache is None or self._time_index_cache_key != key:
            self._time_index_cache = pd.date_range(
                f'{self.start_year}-01-01',
                f'{self.end_year}-12-01', freq='MS')
            self._time_index_cache_key = key
        return self._time_index_cache
# ═══════════════════════════════════════
#  蒙大拿法（Tennant法）生态基流百分比
# ═══════════════════════════════════════

# 不同等级对应的百分比
# key: 等级名, value: (非汛期百分比, 汛期百分比)
TENNANT_LEVELS = {
    'excellent': (0.40, 0.60),   # 极好
    'good':      (0.20, 0.40),   # 好（推荐）
    'fair':      (0.10, 0.20),   # 一般
    'poor':      (0.05, 0.10),   # 差
    'minimum':   (0.02, 0.05),   # 最低
}

# 汛期月份
FLOOD_SEASON_MONTHS = [6, 7, 8, 9, 10]

# 默认生态基流等级
DEFAULT_TENNANT_LEVEL = 'good'

# 生态基流优先级说明：
#   0: 最高优先级 - 无论如何保证生态基流（强制约束）
#   1: 高优先级   - 优先生态基流，但极端干旱可部分削减
#   2: 中优先级   - 生态和需水各占一半
#   3: 低优先级   - 优先满足需水，剩余给生态
DEFAULT_ECO_PRIORITY = 1

@dataclass
class ModelConfig:
    name: str = DEFAULT_NETWORK_NAME
    time: TimeConfig = field(default_factory=TimeConfig)
    runoff_method: RunoffMethod = RunoffMethod.ABCD
    et_method: ETMethod = ETMethod.INPUT
    allocation_method: AllocationMethod = AllocationMethod.PRIORITY
    run_mode: RunMode = RunMode.SIMULATION
    run_preset: str = 'full'
    model_scenario_id: str = ''
    model_scenario_name: str = ''
    model_scenario_layer: str = ''
    model_scenario_description: str = ''

    # 不同输入层的年份解释规则。默认 calendar 表示随模拟年份变化；
    # fixed 表示所有模拟年都使用 fixed_input_years 中指定的现状年。
    # 典型例子：2024 现状水网长序列模拟中，hydrology=calendar，
    # demand/infrastructure/source_structure/unconventional=fixed(2024)。
    input_year_policies: Dict[str, str] = field(default_factory=dict)
    fixed_input_years: Dict[str, int] = field(default_factory=dict)

    # 统一模型开关字典。推荐以后都通过这个入口控制过程：
    #   {'enable_transfers': 1, 'enable_demands': 0, ...}
    # 下方 enable_* 字段作为脚本友好的显式配置项，初始化后会同步到 model_switches。
    model_switches: Dict[str, int] = field(default_factory=dict)

    # 过程开关：1=开启，0=关闭。自然率定时应关闭社会水循环过程。
    enable_snowmelt: int = 1
    enable_gw_overexploit: int = 1
    enable_water_rights: int = 0
    enable_historical_supply_closure: int = 0
    enable_historical_irrigation_canal_buffer: int = 0
    water_rights_policy_file: str = 'policies/yellow_river_1987_allocation_plan.csv'
    water_rights_scheme_id: str = 'yellow_river_1987'
    water_rights_apply_from_year: int = 1987
    water_rights_target_basin: str = '黄河'
    water_rights_count_transfer_loss: int = 1
    water_rights_budget_mode: str = 'monthly'
    water_rights_demand_accounting: str = 'gross_withdrawal'
    enable_dynamic_demand: int = 1
    enable_reservoirs: int = 1
    enable_lakes: int = 1
    enable_diversions: int = 0
    enable_transfers: int = 1
    enable_demands: int = 1
    enable_return_flows: int = 1
    enable_groundwater_allocation: int = 1

    # 生态基流配置
    tennant_level: str = 'good'          # 蒙大拿法等级
    default_eco_priority: int = 1         # 默认生态优先级
    eco_warmup_years: int = 5             # 预热期年数（计算多年均值用）

    reservoir_service_scope: str = 'downstream_mainstem'
    reservoir_service_hops: int = 8
    reservoir_service_include_tributaries: bool = True
    reservoir_release_mode: str = 'allocation'
    reservoir_conservative_storage_ratio: float = 0.45
    reservoir_min_supply_storage_ratio: float = 0.20
    reservoir_emergency_supply_ratio: float = 0.30
    reservoir_supply_target_storage_ratio: float = 0.75
    reservoir_refill_target_storage_ratio: float = 0.90
    reservoir_excess_release_factor: float = 1.00
    reservoir_release_buffer: float = 1.05
    reservoir_flood_release_factor: float = 0.60
    enable_channel_routing: int = 1
    channel_base_residence_months: float = 0.10
    channel_area_residence_factor: float = 0.15
    channel_area_scale: float = 10000.0
    channel_max_residence_months: float = 0.75
    diversion_eco_reserve_ratio: float = 0.10
    diversion_eco_reserve_min_flow: float = 0.0
    diversion_downstream_min_ratio: float = 0.20
    diversion_downstream_min_flow: float = 0.0
    local_surface_access_ratio: float = 0.35
    local_surface_max_units: int = 3
    share_local_surface_by_demand: int = 1
    source_structure_fill_missing_years: int = 1
    historical_irrigation_canal_scope_prefixes: str = 'D030100,D030300,D030400,D030500'
    historical_irrigation_canal_months: str = '4,5,6,7,8,9,10'
    historical_irrigation_canal_target_access_ratio: float = 0.90
    historical_irrigation_canal_priority: float = 1.5
    groundwater_balance_mode: str = 'managed_overdraft'
    groundwater_recharge_balance_factor: float = 1.0
    groundwater_balance_storage_buffer_ratio: float = 0.0
    groundwater_recharge_balance_factor_by_basin: str = ''
    groundwater_balance_storage_buffer_by_basin: str = ''
    unconventional_supply_priority: float = 0.8
    transfer_plan_mode_default: str = 'auto'
    transfer_hybrid_demand_weight: float = 0.5
    transfer_service_mode: str = 'constrained'
    enable_regulated_transfer_storage_constraint: int = 0
    regulated_transfer_storage_target_ratio: float = 0.75
    regulated_transfer_storage_min_factor: float = 0.65
    include_planned_infrastructure: int = 1
    active_infrastructure_scenario: str = 'all'

    def __post_init__(self):
        if isinstance(self.run_mode, str):
            self.run_mode = RunMode(self.run_mode)
        initial_model_switches = dict(self.model_switches or {})
        preset = self.normalize_run_preset(self.run_preset)
        self.run_preset = preset
        if preset != 'full':
            self.apply_run_preset(preset)
        else:
            self._sync_model_switches_from_fields()
        if initial_model_switches:
            self.set_process_switches(**initial_model_switches)

    @staticmethod
    def normalize_run_preset(preset: Optional[str]) -> str:
        key = str(preset or 'full').strip().lower().replace('-', '_')
        normalized = RUN_PRESET_ALIASES.get(key)
        if normalized is None:
            allowed = ', '.join(sorted(set(RUN_PRESET_ALIASES.values())))
            raise ValueError(f"Unknown run_preset '{preset}'. Allowed presets: {allowed}")
        return normalized

    def apply_run_preset(self, preset: Optional[str] = None):
        preset = self.normalize_run_preset(preset or self.run_preset)
        self.run_preset = preset
        self.model_switches = {}

        if preset == 'full':
            self.run_mode = RunMode.SIMULATION
            # full：完整水网调配，所有已实现过程按中心注册表默认值开启。
            self.set_process_switches(**RUN_PRESET_SWITCHES['full'])
            return self

        if preset == 'natural':
            self.run_mode = RunMode.NATURAL
            # natural：只保留天然产汇流，关闭取用水、调水、退水等社会水循环过程。
            self.set_process_switches(**RUN_PRESET_SWITCHES['natural'])
            return self

        raise ValueError(f"Unsupported run_preset '{preset}'")

    @staticmethod
    def _switch_value(value) -> int:
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {'1', 'true', 'yes', 'on', 'open', 'enable', 'enabled', '开启', '开'}:
                return 1
            if text in {'0', 'false', 'no', 'off', 'close', 'disable', 'disabled', '关闭', '关'}:
                return 0
        return 1 if bool(value) else 0

    def _sync_model_switches_from_fields(self):
        self.model_switches = {}
        for key in PROCESS_SWITCH_KEYS:
            default = PROCESS_SWITCH_REGISTRY[key]['full']
            value = self._switch_value(getattr(self, key, default))
            self.model_switches[key] = value
            setattr(self, key, value)

    def set_process_switches(self, **switches):
        for key, value in switches.items():
            if value is None:
                continue
            if key not in PROCESS_SWITCH_REGISTRY:
                raise AttributeError(f"Unknown process switch '{key}'")
            value = self._switch_value(value)
            self.model_switches[key] = value
            setattr(self, key, value)
        return self

    def process_switches(self) -> Dict[str, int]:
        switches = {}
        for key in PROCESS_SWITCH_KEYS:
            default = PROCESS_SWITCH_REGISTRY[key]['full']
            value = self.model_switches.get(key, getattr(self, key, default))
            value = self._switch_value(value)
            switches[key] = value
            self.model_switches[key] = value
            setattr(self, key, value)
        return switches

    def is_process_enabled(self, key: str) -> bool:
        if key not in PROCESS_SWITCH_REGISTRY:
            raise AttributeError(f"Unknown process switch '{key}'")
        default = PROCESS_SWITCH_REGISTRY[key]['full']
        value = self.model_switches.get(key, getattr(self, key, default))
        value = self._switch_value(value)
        self.model_switches[key] = value
        setattr(self, key, value)
        return bool(value)

    def process_switch_table(self) -> pd.DataFrame:
        switches = self.process_switches()
        return pd.DataFrame([
            {
                'switch': key,
                'value': switches[key],
                'description': PROCESS_SWITCH_DESCRIPTIONS[key],
            }
            for key in PROCESS_SWITCH_KEYS
        ])

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'start_year': self.time.start_year,
            'end_year': self.time.end_year,
            'n_steps': self.time.n_steps,
            'runoff_method': self.runoff_method.value,
            'et_method': self.et_method.value,
            'allocation_method': self.allocation_method.value,
            'run_mode': self.run_mode.value,
            'run_preset': self.run_preset,
            'model_scenario_id': self.model_scenario_id,
            'model_scenario_name': self.model_scenario_name,
            'model_scenario_layer': self.model_scenario_layer,
            'model_scenario_description': self.model_scenario_description,
            'input_year_policies': dict(self.input_year_policies or {}),
            'fixed_input_years': dict(self.fixed_input_years or {}),
            'model_switches': self.process_switches(),
            'process_switches': self.process_switches(),
            'process_switch_descriptions': PROCESS_SWITCH_DESCRIPTIONS,
            'enable_snowmelt': self.enable_snowmelt,
            'enable_gw_overexploit': self.enable_gw_overexploit,
            'enable_dynamic_demand': self.enable_dynamic_demand,
            'enable_reservoirs': self.enable_reservoirs,
            'enable_lakes': self.enable_lakes,
            'enable_diversions': self.enable_diversions,
            'enable_transfers': self.enable_transfers,
            'enable_demands': self.enable_demands,
            'enable_return_flows': self.enable_return_flows,
            'enable_groundwater_allocation': self.enable_groundwater_allocation,
            'enable_water_rights': self.enable_water_rights,
            'enable_historical_supply_closure': self.enable_historical_supply_closure,
            'enable_historical_irrigation_canal_buffer': self.enable_historical_irrigation_canal_buffer,
            'water_rights_policy_file': self.water_rights_policy_file,
            'water_rights_scheme_id': self.water_rights_scheme_id,
            'water_rights_apply_from_year': self.water_rights_apply_from_year,
            'water_rights_target_basin': self.water_rights_target_basin,
            'water_rights_count_transfer_loss': self.water_rights_count_transfer_loss,
            'water_rights_budget_mode': self.water_rights_budget_mode,
            'water_rights_demand_accounting': self.water_rights_demand_accounting,
            'tennant_level': self.tennant_level,
            'eco_priority': self.default_eco_priority,
            'reservoir_service_scope': self.reservoir_service_scope,
            'reservoir_release_mode': self.reservoir_release_mode,
            'reservoir_supply_target_storage_ratio': self.reservoir_supply_target_storage_ratio,
            'reservoir_refill_target_storage_ratio': self.reservoir_refill_target_storage_ratio,
            'enable_channel_routing': self.enable_channel_routing,
            'channel_base_residence_months': self.channel_base_residence_months,
            'diversion_eco_reserve_ratio': self.diversion_eco_reserve_ratio,
            'diversion_downstream_min_ratio': self.diversion_downstream_min_ratio,
            'local_surface_access_ratio': self.local_surface_access_ratio,
            'local_surface_max_units': self.local_surface_max_units,
            'share_local_surface_by_demand': self.share_local_surface_by_demand,
            'source_structure_fill_missing_years': self.source_structure_fill_missing_years,
            'historical_irrigation_canal_scope_prefixes': self.historical_irrigation_canal_scope_prefixes,
            'historical_irrigation_canal_months': self.historical_irrigation_canal_months,
            'historical_irrigation_canal_target_access_ratio': self.historical_irrigation_canal_target_access_ratio,
            'historical_irrigation_canal_priority': self.historical_irrigation_canal_priority,
            'groundwater_balance_mode': self.groundwater_balance_mode,
            'groundwater_recharge_balance_factor': self.groundwater_recharge_balance_factor,
            'groundwater_balance_storage_buffer_ratio': self.groundwater_balance_storage_buffer_ratio,
            'groundwater_recharge_balance_factor_by_basin': self.groundwater_recharge_balance_factor_by_basin,
            'groundwater_balance_storage_buffer_by_basin': self.groundwater_balance_storage_buffer_by_basin,
            'unconventional_supply_priority': self.unconventional_supply_priority,
            'transfer_plan_mode_default': self.transfer_plan_mode_default,
            'transfer_hybrid_demand_weight': self.transfer_hybrid_demand_weight,
            'transfer_service_mode': self.transfer_service_mode,
            'enable_regulated_transfer_storage_constraint': self.enable_regulated_transfer_storage_constraint,
            'regulated_transfer_storage_target_ratio': self.regulated_transfer_storage_target_ratio,
            'regulated_transfer_storage_min_factor': self.regulated_transfer_storage_min_factor,
            'include_planned_infrastructure': self.include_planned_infrastructure,
            'active_infrastructure_scenario': self.active_infrastructure_scenario,
        }

    def set_model_scenario(
        self,
        scenario_id: str,
        name: str = '',
        layer: str = '',
        description: str = '',
    ):
        self.model_scenario_id = str(scenario_id or '').strip()
        self.model_scenario_name = str(name or '').strip()
        self.model_scenario_layer = str(layer or '').strip()
        self.model_scenario_description = str(description or '').strip()
        return self

    @staticmethod
    def normalize_year_policy(policy: Optional[str]) -> str:
        key = str(policy or 'calendar').strip().lower().replace('-', '_')
        aliases = {
            '': 'calendar',
            'calendar': 'calendar',
            'transient': 'calendar',
            'historical': 'calendar',
            'by_year': 'calendar',
            'simulation_year': 'calendar',
            'fixed': 'fixed',
            'constant': 'fixed',
            'present': 'fixed',
            'current': 'fixed',
            'status_quo': 'fixed',
        }
        normalized = aliases.get(key)
        if normalized is None:
            raise ValueError(
                f"Unknown input year policy '{policy}'. "
                "Allowed: calendar, fixed"
            )
        return normalized

    def set_input_year_policy(
        self,
        domain: str,
        policy: str = 'calendar',
        fixed_year: Optional[int] = None,
    ):
        domain_key = self.normalize_input_domain(domain)
        normalized = self.normalize_year_policy(policy)
        self.input_year_policies[domain_key] = normalized
        if fixed_year is not None:
            self.fixed_input_years[domain_key] = int(fixed_year)
        return self

    @staticmethod
    def normalize_input_domain(domain: str) -> str:
        key = str(domain or '').strip().lower().replace('-', '_')
        aliases = {
            'demand_calibration': 'demand',
            'water_use': 'demand',
            'social_demand': 'demand',
            'unconventional_supply': 'unconventional',
            'other_supply': 'unconventional',
            'source_factor': 'source_structure',
            'source_factors': 'source_structure',
            'source_structure_factor': 'source_structure',
            'source_structure_factors': 'source_structure',
            'infra': 'infrastructure',
            'engineering': 'infrastructure',
            'water_network': 'infrastructure',
            'network': 'infrastructure',
        }
        return aliases.get(key, key)

    def resolve_input_year(self, domain: str, calendar_year: int) -> int:
        domain_key = self.normalize_input_domain(domain)
        policy = self.input_year_policies.get(domain_key)
        if policy is None and domain_key in {'demand', 'unconventional', 'source_structure'}:
            policy = self.input_year_policies.get('allocation_inputs')
        if policy is None:
            policy = 'calendar'
        policy = self.normalize_year_policy(policy)
        if policy == 'calendar':
            return int(calendar_year)

        fixed_year = self.fixed_input_years.get(domain_key)
        if fixed_year is None and domain_key in {'demand', 'unconventional', 'source_structure'}:
            fixed_year = self.fixed_input_years.get('allocation_inputs')
        if fixed_year is None:
            raise ValueError(
                f"Input year policy for '{domain_key}' is fixed but no fixed year is configured."
            )
        return int(fixed_year)

    def input_year_policy_table(self) -> pd.DataFrame:
        domains = sorted(set(self.input_year_policies) | set(self.fixed_input_years))
        if not domains:
            domains = ['hydrology', 'demand', 'infrastructure', 'source_structure', 'unconventional']
        rows = []
        for domain in domains:
            policy = self.input_year_policies.get(domain, 'calendar')
            rows.append(
                {
                    'domain': domain,
                    'policy': policy,
                    'fixed_year': self.fixed_input_years.get(domain, ''),
                }
            )
        return pd.DataFrame(rows)
