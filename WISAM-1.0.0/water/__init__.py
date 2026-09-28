"""water - 水资源管理模块"""
from water.demand import DemandModule
from water.reservoir import ReservoirModule
from water.lake import LakeModule
from water.diversion import DiversionModule
from water.transfer import TransferModule, TransferProject
from water.allocation import AllocationEngine, WaterSource, AllocationResult
from water.ecology import EcologyModule
from water.eco_baseflow import EcoBaseflowModule
from water.water_rights import WaterRightsScheme
