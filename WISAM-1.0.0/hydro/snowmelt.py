"""
hydro/snowmelt.py - 融雪模块

采用温度指数法（Degree-Day Method）：
  T ≤ 0℃: 降水全部以雪形式储存
  T > 0℃: 降水为雨，同时融化积雪

融雪量 = min(积雪量, 融雪系数 × T × 天数)

用于高海拔地区（青藏高原、天山等）
"""
import numpy as np
from config import DAYS_IN_MONTH

class SnowModule:
    """
    温度指数融雪模型

    使用方式：
        snow = SnowModule(melt_factor=2.5)
        result = snow.step(P=30, T=-5, month=1)
        effective_P = result['effective_precip']
    """

    def __init__(self, melt_factor: float = 2.5,
                 rain_snow_temp: float = 0.0,
                 initial_snow: float = 0.0):
        """
        Parameters
        ----------
        melt_factor : 融雪系数 mm/℃/day
            典型值：2~5，高海拔取大值
        rain_snow_temp : 雨雪分界温度 ℃
            T ≤ 此温度时降水为雪
        initial_snow : 初始积雪深度 mm（水当量SWE）
        """
        self.melt_factor = melt_factor
        self.rain_snow_temp = rain_snow_temp
        self.snow_storage = initial_snow  # 积雪水当量 mm

    def step(self, P: float, T: float, month: int) -> dict:
        """
        月融雪计算

        Parameters
        ----------
        P : 月降水 mm
        T : 月均温 ℃
        month : 月份 1-12

        Returns
        -------
        dict:
            effective_precip: 有效降水 mm（液态水=雨+融雪，可进入产流）
            snowmelt:         融雪量 mm
            snow_storage:     月末积雪 mm SWE
            rain:             降雨量 mm
            snowfall:         降雪量 mm
        """
        P = max(0.0, P)
        days = DAYS_IN_MONTH[month - 1]

        if T <= self.rain_snow_temp:
            # ── 降温期：降水全部为雪 ──
            snowfall = P
            rain = 0.0
            melt = 0.0
            self.snow_storage += snowfall

        else:
            # ── 升温期：降水为雨，融化积雪 ──
            rain = P
            snowfall = 0.0

            # 潜在融雪量 = 融雪系数 × 温度 × 天数
            potential_melt = self.melt_factor * T * days
            potential_melt = max(0.0, potential_melt)

            # 实际融雪不超过现有积雪
            melt = min(self.snow_storage, potential_melt)
            self.snow_storage = max(0.0, self.snow_storage - melt)

        # 有效降水 = 雨 + 融雪
        effective_precip = rain + melt

        return {
            'effective_precip': effective_precip,
            'snowmelt': melt,
            'snow_storage': self.snow_storage,
            'rain': rain,
            'snowfall': snowfall,
        }

    def reset(self, initial_snow: float = 0.0):
        """重置积雪状态"""
        self.snow_storage = initial_snow
