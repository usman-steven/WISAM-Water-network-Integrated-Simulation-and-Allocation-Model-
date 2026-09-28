"""
hydro/pet.py - 蒸散发计算

支持三种模式：
  INPUT:           直接使用CSV加载的PET数据（推荐）
  HARGREAVES:      Hargreaves-Samani法（只需温度）
  PENMAN_MONTEITH: 简化Penman法

单位：
  输入：温度 ℃，辐射 MJ/m²/day
  输出：PET mm/月
"""
import numpy as np
from config import ETMethod, DAYS_IN_MONTH

def calc_pet(sb, month: int, t: int, method: ETMethod) -> float:
    """
    计算某单元某月的潜在蒸散发

    注意：如果PET已从CSV加载（sb.pet不为None且有值），
    simulator.initialize()中不会调用此函数。
    此函数仅在PET未加载时作为后备计算。

    Parameters
    ----------
    sb : SubbasinNode
    month : 月份 1-12
    t : 时段索引
    method : ET计算方法

    Returns
    -------
    PET mm/月
    """
    if method == ETMethod.HARGREAVES:
        return _hargreaves(sb, month, t)
    elif method == ETMethod.PENMAN_MONTEITH:
        return _penman_simplified(sb, month, t)
    else:
        # INPUT模式 或 未知方法 → Hargreaves作为后备
        return _hargreaves(sb, month, t)

def _hargreaves(sb, month: int, t: int) -> float:
    """
    Hargreaves-Samani (1985) 公式

    ET0 = 0.0023 × Ra × (T + 17.8) × (Tmax - Tmin)^0.5

    其中 Ra 为天文辐射（mm/day 等效值）

    只需要温度数据，适合数据稀缺地区
    """
    # 获取温度
    if sb.temp_mean is not None and t < len(sb.temp_mean):
        T = sb.temp_mean[t]
    else:
        T = 15.0

    if (sb.temp_max is not None and sb.temp_min is not None
            and t < len(sb.temp_max) and t < len(sb.temp_min)):
        Tmax = sb.temp_max[t]
        Tmin = sb.temp_min[t]
    else:
        # 没有极值温度时估算
        Tmax = T + 5.0
        Tmin = T - 5.0

    TR = max(0.1, Tmax - Tmin)  # 日温差，至少0.1防止sqrt(0)

    # 纬度（使用y坐标作为纬度，如果没有默认35°N）
    lat_deg = abs(sb.y) if hasattr(sb, 'y') and sb.y != 0 else 35.0
    lat_rad = lat_deg * np.pi / 180.0

    # 日序数（月中）
    day_of_year = sum(DAYS_IN_MONTH[:month - 1]) + DAYS_IN_MONTH[month - 1] // 2

    # 太阳赤纬角
    delta = 0.409 * np.sin(2 * np.pi * day_of_year / 365.0 - 1.39)

    # 日落时角（处理极昼/极夜）
    cos_ws = -np.tan(lat_rad) * np.tan(delta)
    cos_ws = max(-1.0, min(1.0, cos_ws))  # 钳制到[-1, 1]
    ws = np.arccos(cos_ws)

    # 日地距离修正系数
    dr = 1.0 + 0.033 * np.cos(2 * np.pi * day_of_year / 365.0)

    # 天文辐射 Ra (MJ/m²/day)
    Gsc = 0.0820  # 太阳常数 MJ/m²/min
    Ra_MJ = (24.0 * 60.0 / np.pi) * Gsc * dr * (
        ws * np.sin(lat_rad) * np.sin(delta) +
        np.cos(lat_rad) * np.cos(delta) * np.sin(ws))
    Ra_MJ = max(0.0, Ra_MJ)

    # 转换为 mm/day 等效值 (1 MJ/m² ≈ 0.408 mm蒸发)
    Ra_mm = Ra_MJ * 0.408

    # Hargreaves公式 → ET0 (mm/day)
    ET0_daily = 0.0023 * Ra_mm * (T + 17.8) * np.sqrt(TR)
    ET0_daily = max(0.0, ET0_daily)

    # 月累计 (mm/month)
    days = DAYS_IN_MONTH[month - 1]
    ET0_monthly = ET0_daily * days

    return ET0_monthly

def _penman_simplified(sb, month: int, t: int) -> float:
    """
    简化Penman法

    在Hargreaves基础上乘以修正系数
    实际Penman-Monteith需要风速/湿度/日照等数据，
    这里用简化版本作为近似
    """
    har = _hargreaves(sb, month, t)
    # 湿润地区Penman偏高约10%，干旱地区差异更大
    return har * 1.1
