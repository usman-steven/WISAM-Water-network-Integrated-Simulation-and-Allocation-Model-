# 运行环境 / Dependencies

WISAM 1.0.0要求Python ≥3.11，推荐Python 3.12。`requirements.txt`列出固定依赖版本。

| 依赖 | 版本 | 用途 |
|---|---|---|
| NumPy | 2.3.5 | 数值计算 |
| pandas | 3.0.1 | 时序与表格处理 |
| openpyxl | 3.1.5 | Excel结果导出 |

安装：`python -m pip install -r requirements.txt`。

软件运行检查使用Windows x64、Python 3.12.14，覆盖核心模块导入、命令行、合成水网模拟和Excel读写。

## 可选功能

| 依赖 | 功能 |
|---|---|
| Numba | 水文序列计算加速；缺省使用NumPy实现 |
| GeoPandas | 当单元表缺少坐标时，从`shp/unit.shp`读取质心；直接提供坐标时不需要 |
| XlsxWriter | Excel导出的备用写入器 |

按需要安装可选依赖。地理输入需要保持坐标参考系一致。

## 路径设置

建议从项目根目录执行命令。`--data-dir`指定输入目录；`WISAM_OUTPUT_DIR`指定输出和水文缓存根目录，在导入模型前设置，默认值为项目根目录下的`output/`。

