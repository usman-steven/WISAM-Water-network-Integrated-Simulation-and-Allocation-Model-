"""
hydro/landuse.py - 土地利用对产流参数的动态修正

核心机制：
  城镇化↑ → 不透水面↑ → 产流系数↑，基流↓
  森林↑   → 截留蒸腾↑ → ET↑，径流↓
  退耕还林 → 产流系数↓

1960-2020年中国城镇化率从20%升至64%，
对水文过程有显著影响，产流参数不应保持不变。

修正方法：
  以最早年份的土地利用为基准，
  计算当年城镇化/森林变化量，
  线性修正产流系数、基流指数、ET折减等参数。
"""
import numpy as np
import pandas as pd
from typing import Dict, Optional, List
from collections import defaultdict

class LandUseModule:
    """
    土地利用变化模块

    使用方式：
        lu = LandUseModule()
        lu.load(landuse_df)
        lu.load_soil(soil_df)
        lu.load_terrain(terrain_df)

        # 获取调整后的参数
        adj = lu.adjust_params_for_year('D05070014080000', 2000, base_params)
        sb.runoff_coeff = adj['runoff_coeff']
    """

    def __init__(self):
        # unit_id → {year: {field: value}}
        self._landuse: Dict[str, Dict[int, dict]] = defaultdict(dict)
        # unit_id → soil info
        self._soil: Dict[str, dict] = {}
        # unit_id → terrain info
        self._terrain: Dict[str, dict] = {}

        # 修正系数（可调）
        self.urban_runoff_factor = 0.008   # 城镇化每增1%，产流系数增0.008
        self.forest_et_factor = 0.003      # 森林每增1%，ET折减增0.003
        self.urban_baseflow_factor = 0.005 # 城镇化每增1%，基流指数减0.005

    # ══════════════════════════════════════
    #  数据加载
    # ══════════════════════════════════════

    def load(self, df: pd.DataFrame):
        """
        加载土地利用数据

        CSV格式：
        unit_id, year, cropland_pct, forest_pct, grassland_pct,
        urban_pct, water_pct, unused_pct, irrigated_area_km2
        """
        if df is None or len(df) == 0:
            return

        uid_col = 'unit_id'
        for c in df.columns:
            if 'uid' in c.lower() or 'id' in c.lower():
                uid_col = c
                break

        count = 0
        for _, row in df.iterrows():
            uid = str(row.get(uid_col, '')).strip()
            year = int(row.get('year', 0))
            if not uid or year <= 0:
                continue

            self._landuse[uid][year] = {
                'cropland': float(row.get('cropland_pct', 0)),
                'forest': float(row.get('forest_pct', 0)),
                'grassland': float(row.get('grassland_pct', 0)),
                'urban': float(row.get('urban_pct', 0)),
                'water': float(row.get('water_pct', 0)),
                'unused': float(row.get('unused_pct', 0)),
                'irrigated_area': float(row.get('irrigated_area_km2', 0)),
            }
            count += 1

        print(f"    加载 {count} 条土地利用记录 "
              f"({len(self._landuse)} 个单元)")

    def load_soil(self, df: pd.DataFrame):
        """加载土壤参数"""
        if df is None or len(df) == 0:
            return
        count = 0
        for _, row in df.iterrows():
            uid = str(row.get('unit_id', '')).strip()
            if not uid:
                continue
            self._soil[uid] = {
                'soil_type': str(row.get('soil_type', '')),
                'sand': float(row.get('sand_pct', 40)),
                'clay': float(row.get('clay_pct', 20)),
                'depth': float(row.get('soil_depth_mm', 1000)),
                'fc': float(row.get('field_capacity', 0.30)),
                'wp': float(row.get('wilting_point', 0.12)),
                'permeability': str(row.get('permeability', 'medium')),
            }
            count += 1
        print(f"    加载 {count} 个单元的土壤参数")

    def load_terrain(self, df: pd.DataFrame):
        """加载地形参数"""
        if df is None or len(df) == 0:
            return
        count = 0
        for _, row in df.iterrows():
            uid = str(row.get('unit_id', '')).strip()
            if not uid:
                continue
            self._terrain[uid] = {
                'elev': float(row.get('mean_elev', 500)),
                'slope': float(row.get('mean_slope', 5)),
                'density': float(row.get('drainage_density', 0.5)),
            }
            count += 1
        print(f"    加载 {count} 个单元的地形参数")

    # ══════════════════════════════════════
    #  土地利用查询（含年际插值）
    # ══════════════════════════════════════

    def get_landuse(self, unit_id: str, year: int) -> dict:
        """
        获取某单元某年的土地利用（自动线性插值）

        如果该单元无数据，返回默认值
        """
        lu_data = self._landuse.get(unit_id, {})
        if not lu_data:
            return {
                'cropland': 40, 'forest': 20, 'grassland': 15,
                'urban': 10, 'water': 5, 'unused': 10,
                'irrigated_area': 0,
            }

        years = sorted(lu_data.keys())

        # 边界处理
        if year <= years[0]:
            return dict(lu_data[years[0]])
        if year >= years[-1]:
            return dict(lu_data[years[-1]])

        # 线性插值
        y1 = max(y for y in years if y <= year)
        y2 = min(y for y in years if y > year)
        w = (year - y1) / max(1, y2 - y1)

        result = {}
        for key in lu_data[y1]:
            v1 = lu_data[y1][key]
            v2 = lu_data[y2][key]
            result[key] = v1 + (v2 - v1) * w
        return result

    # ══════════════════════════════════════
    #  初始参数估算（基于物理特征）
    # ══════════════════════════════════════

    def estimate_initial_params(self, unit_id: str) -> dict:
        """
        根据土壤和地形估算初始水文参数

        用于hydro_params.csv缺失时的初始值
        """
        soil = self._soil.get(unit_id, {})
        terrain = self._terrain.get(unit_id, {})

        # param_b: 土壤蓄水容量
        depth = soil.get('depth', 1000)
        fc = soil.get('fc', 0.30)
        wp = soil.get('wp', 0.12)
        avail_water = depth * (fc - wp)
        param_b = max(50, min(500, avail_water * 1.2))

        # param_c: 地下水补给比例
        perm = soil.get('permeability', 'medium')
        param_c = {'high': 0.6, 'medium': 0.45, 'low': 0.3}.get(perm, 0.45)

        # runoff_coeff: 产流系数
        slope = terrain.get('slope', 5)
        clay = soil.get('clay', 20)
        rc = 0.25 + 0.005 * slope + 0.002 * clay
        runoff_coeff = max(0.1, min(0.6, rc))

        # baseflow_index: 基流指数
        bfi = {'high': 0.45, 'medium': 0.30, 'low': 0.15}.get(perm, 0.30)

        # 高海拔判断
        elev = terrain.get('elev', 500)
        is_high_altitude = elev > 3000

        return {
            'param_a': 0.95,
            'param_b': param_b,
            'param_c': param_c,
            'param_d': 0.15,
            'runoff_coeff': runoff_coeff,
            'baseflow_index': bfi,
            'et_reduction': 0.30,
            'is_high_altitude': is_high_altitude,
        }

    # ══════════════════════════════════════
    #  参数动态修正
    # ══════════════════════════════════════

    def adjust_params_for_year(self, unit_id: str, year: int,
                                base_params: dict) -> dict:
        """
        根据某年的土地利用调整产流参数

        Parameters
        ----------
        unit_id : 单元UID
        year : 当前年份
        base_params : 基准参数（率定值或初始估算值）

        Returns
        -------
        调整后的参数字典
        """
        # 如果该单元没有土地利用数据，直接返回基准参数
        if unit_id not in self._landuse:
            return dict(base_params)

        lu = self.get_landuse(unit_id, year)
        result = dict(base_params)

        # 获取基准年份的土地利用
        lu_data = self._landuse[unit_id]
        base_year = min(lu_data.keys())
        base_lu = lu_data[base_year]
        base_urban = base_lu.get('urban', 10)
        base_forest = base_lu.get('forest', 20)

        # 变化量
        d_urban = lu.get('urban', 10) - base_urban
        d_forest = lu.get('forest', 20) - base_forest

        # 产流系数修正（城镇化→不透水面→产流增大）
        base_rc = base_params.get('runoff_coeff', 0.35)
        result['runoff_coeff'] = max(0.1, min(0.8,
            base_rc + self.urban_runoff_factor * d_urban))

        # 基流指数修正（城镇化→入渗减少→基流减少）
        base_bfi = base_params.get('baseflow_index', 0.30)
        result['baseflow_index'] = max(0.05, min(0.6,
            base_bfi - self.urban_baseflow_factor * d_urban))

        # ET折减修正（森林增加→蒸腾增加）
        base_et = base_params.get('et_reduction', 0.30)
        result['et_reduction'] = max(0.1, min(0.6,
            base_et + self.forest_et_factor * d_forest))

        # ABCD param_c 修正（城镇化→地下水补给减少）
        base_c = base_params.get('param_c', 0.5)
        result['param_c'] = max(0.1, min(0.9,
            base_c - 0.003 * d_urban))

        return result
