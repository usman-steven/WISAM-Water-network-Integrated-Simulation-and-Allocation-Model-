"""core - 核心数据结构"""
from core.nodes import (
    Node, SubbasinNode, RiverChannelNode, ReservoirNode,
    LakeNode, GroundwaterNode, DemandNode, RiverDiversionNode,
    OceanNode, EcoControlNode, JunctionNode
)
from core.links import (
    Link, RiverLink, SupplyLink, TransferLink, ReturnFlowLink
)
from core.network import WaterNetwork
