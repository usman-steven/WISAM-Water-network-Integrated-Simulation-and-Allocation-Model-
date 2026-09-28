"""
data_io/templates.py - 生成CSV模板（简化版）

主要用于提示用户数据格式。
实际使用虚拟数据生成器 tools/generate_virtual_data.py
"""
import os
import pandas as pd
import numpy as np

def generate_all_templates(data_dir="data", units_df=None):
    """生成所有CSV空模板"""
    for sub in ['climate', 'demand', 'infra', 'stations', 'ecology', 'physical']:
        os.makedirs(os.path.join(data_dir, sub), exist_ok=True)

    print("  模板文件：")
    print(f"    {data_dir}/units.csv")
    print(f"    {data_dir}/topology.csv")
    print(f"    {data_dir}/climate/precip.csv")
    print(f"    {data_dir}/climate/temp_mean.csv")
    print(f"    {data_dir}/climate/pet.csv")
    print(f"    {data_dir}/demand/annual_demand.csv")
    print(f"    {data_dir}/return_ratios.csv")
    print(f"    {data_dir}/gw_params.csv")
    print(f"    {data_dir}/infra/reservoirs.csv")
    print(f"    {data_dir}/ecology/eco_control.csv")
    print(f"\n  请运行 tools/generate_virtual_data.py 生成虚拟数据")
