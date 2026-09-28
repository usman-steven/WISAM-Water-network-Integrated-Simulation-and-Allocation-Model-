"""
water/water_rights.py - 水权配额约束

实现类似黄河"八七分水方案"的水权管理：
  各省有年度用水配额，按月分配
  丰水年可以适当增加，枯水年必须压减

使用方式：
  scheme = WaterRightsScheme(
      annual_quota={'山西': 43.1亿m³, '河南': 55.4亿m³, ...})
  monthly = scheme.get_monthly_quota('山西', month=3, annual_flow_ratio=0.8)
"""
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, Optional

@dataclass
class WaterRightsScheme:
    """水权分配方案"""
    id: str = ""
    name: str = ""
    river: str = ""

    # 各水权控制区年度配额 万m³。多数控制区与省份一致；
    # “河北天津”这类共用指标用 province_quota_key 映射到同一个控制区。
    annual_quota: Dict[str, float] = field(default_factory=dict)
    province_quota_key: Dict[str, str] = field(default_factory=dict)

    # 月分配模式（12个月的权重，和为1.0）
    monthly_pattern: np.ndarray = field(
        default_factory=lambda: np.array([
            0.05, 0.05, 0.08, 0.10, 0.12, 0.12,
            0.10, 0.10, 0.08, 0.08, 0.06, 0.06]))

    # 丰枯调节系数
    wet_year_factor: float = 1.15
    dry_year_factor: float = 0.85
    extreme_dry_factor: float = 0.70

    def quota_key_for(self, province: str) -> str:
        """Return the quota-pool key used by one province/region."""
        province = str(province or "").strip()
        return self.province_quota_key.get(province, province)

    def get_monthly_quota_by_key(self, quota_key: str, month: int,
                                 annual_flow_ratio: float = 1.0) -> float:
        """
        获取某水权控制区某月的配额

        Parameters
        ----------
        quota_key : 水权控制区名称
        month : 月份 1-12
        annual_flow_ratio : 当年来水量 / 多年平均来水量
            >1.2: 丰水年, 0.8~1.2: 平水年,
            0.5~0.8: 枯水年, <0.5: 特枯年

        Returns
        -------
        该月配额 万m³ (如果该控制区无配额限制，返回inf)
        """
        annual = self.annual_quota.get(str(quota_key or "").strip(), None)
        if annual is None:
            return float('inf')

        # 丰枯调节
        if annual_flow_ratio > 1.2:
            factor = self.wet_year_factor
        elif annual_flow_ratio < 0.5:
            factor = self.extreme_dry_factor
        elif annual_flow_ratio < 0.8:
            factor = self.dry_year_factor
        else:
            factor = 1.0

        return annual * factor * self.monthly_pattern[month - 1]

    def get_monthly_quota(self, province: str, month: int,
                           annual_flow_ratio: float = 1.0) -> float:
        """
        获取某省某月的配额。

        对共用指标区（如河北天津），返回该共用控制区的月配额。
        真正扣减时应按 quota_key 汇总扣减，避免把共用指标重复计算。
        """
        return self.get_monthly_quota_by_key(
            self.quota_key_for(province),
            month=month,
            annual_flow_ratio=annual_flow_ratio,
        )

    def check_compliance(self, province: str, month: int,
                          actual_use: float,
                          annual_flow_ratio: float = 1.0) -> dict:
        """
        检查某省某月用水是否合规

        Returns
        -------
        dict:
            quota: 配额
            actual: 实际用量
            compliant: 是否合规
            excess: 超额量（负值=有余量）
        """
        quota = self.get_monthly_quota(province, month, annual_flow_ratio)

        return {
            'quota': quota,
            'actual': actual_use,
            'compliant': actual_use <= quota,
            'excess': actual_use - quota,
        }
