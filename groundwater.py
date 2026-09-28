"""
hydro/groundwater.py - 地下水模块

功能：
1. 计算每月可开采量（考虑超采惩罚）
2. 更新累计超采量

超采惩罚机制：
  累计超采越多 → 可开采量越少 → 迫使减少开采
  模拟华北平原地下水漏斗持续扩大的过程
"""

class GroundwaterModule:
    """
    地下水可开采量与超采管理

    使用方式：
        gw = GroundwaterModule(enable_overexploit=True)

        # 每月计算可开采量
        avail = gw.calc_monthly_exploitable(
            annual_exploitable=50000,  # 年可开采量 万m³
            monthly_recharge=2000,     # 当月补给量 万m³
            cumulative_over=10000,     # 累计超采量 万m³
            decay=0.97)                # 衰减系数

        # 每月更新超采量
        new_over = gw.update_overexploit(
            exploit=3000,    # 当月实际开采
            recharge=2000,   # 当月补给
            cumulative=10000)
    """

    def __init__(self, enable_overexploit: bool = True):
        """
        Parameters
        ----------
        enable_overexploit : 是否启用超采惩罚机制
            True: 累计超采会逐步减少可开采量
            False: 每月可开采量固定为年量/12
        """
        self.enable = enable_overexploit

    def calc_monthly_exploitable(self,
                                  annual_exploitable: float,
                                  monthly_recharge: float,
                                  cumulative_over: float,
                                  decay: float) -> float:
        """
        计算当月地下水可开采量

        Parameters
        ----------
        annual_exploitable : 年可开采量 万m³
            水资源评价确定的可持续开采量
        monthly_recharge : 当月地下水补给量 万m³
            来自产流模型的基流/入渗
        cumulative_over : 累计超采量 万m³
            历史累计超过可持续开采量的部分
        decay : 超采衰减系数 (0.90~0.99)
            越小惩罚越大
            0.96: 华北严重超采区
            0.99: 轻度超采区

        Returns
        -------
        当月可开采量 万m³
        """
        if annual_exploitable <= 0:
            return 0.0

        # 基础月可开采量
        monthly_base = annual_exploitable / 12.0

        if not self.enable:
            return monthly_base

        # 超采惩罚：指数衰减
        # 累计超采/年可采量 = 超采倍数
        # decay^超采倍数 = 惩罚系数
        if cumulative_over > 0 and annual_exploitable > 0:
            over_ratio = cumulative_over / annual_exploitable
            penalty = decay ** over_ratio
            # penalty接近1: 几乎无惩罚
            # penalty接近0: 严重惩罚
            penalty = max(0.1, min(1.0, penalty))
            monthly_base *= penalty

        # 当月补给可以少量增加可采量
        # （补给的30%可额外开采，模拟地下水的动态补给）
        monthly_avail = monthly_base + monthly_recharge * 0.3

        return max(0.0, monthly_avail)

    def update_overexploit(self,
                            exploit: float,
                            recharge: float,
                            cumulative: float) -> float:
        """
        更新累计超采量

        Parameters
        ----------
        exploit : 当月实际开采量 万m³
        recharge : 当月地下水补给量 万m³
        cumulative : 当前累计超采量 万m³

        Returns
        -------
        更新后的累计超采量 万m³

        逻辑：
          开采 > 补给 → 超采增加
          开采 < 补给 → 超采恢复（但不低于0）
        """
        if not self.enable:
            return 0.0

        net = exploit - recharge
        if net > 0:
            # 开采超过补给 → 累计超采增加
            cumulative += net
        else:
            # 补给超过开采 → 累计超采恢复
            cumulative = max(0.0, cumulative + net)

        return cumulative
