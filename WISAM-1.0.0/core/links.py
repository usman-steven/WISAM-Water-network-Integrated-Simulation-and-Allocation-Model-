"""core/links.py - 连接类型定义"""
from dataclasses import dataclass
from config import LinkType

@dataclass
class Link:
    """连接基类"""
    id: str = ""
    name: str = ""
    link_type: LinkType = LinkType.RIVER
    from_node: str = ""
    to_node: str = ""
    loss_rate: float = 0.0
    is_active: bool = True

@dataclass
class RiverLink(Link):
    """河道连接（干流→干流 或 非干流→干流）"""
    link_type: LinkType = LinkType.RIVER

@dataclass
class SupplyLink(Link):
    """供水连接（水源→需水节点）"""
    link_type: LinkType = LinkType.SUPPLY_LOCAL
    source_priority: int = 1
    service_weight: float = 1.0
    source_unit_ids: str = ""
    selection_method: str = ""

@dataclass
class TransferLink(Link):
    """调水连接"""
    link_type: LinkType = LinkType.TRANSFER
    project_id: str = ""

@dataclass
class ReturnFlowLink(Link):
    """退水连接（需水节点→河道）"""
    link_type: LinkType = LinkType.RETURN_FLOW
    return_ratio: float = 1.0
    unit_id: str = ""
    source_unit_ids: str = ""
    selection_method: str = ""
