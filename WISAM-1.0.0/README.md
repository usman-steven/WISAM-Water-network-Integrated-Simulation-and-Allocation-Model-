# WISAM

**Water network Integrated Simulation and Allocation Model**  
水网综合模拟与配置模型 · **Version 1.0.0**

WISAM是月尺度水文—水网—水资源配置模型，集成产汇流、水库与湖泊调蓄、跨区域调水、地下水供水、分行业需水及生态用水过程，支持多情景模拟和水量核算。

**作者：**王丽川（Wang Lichuan）  
**单位：**中国水利水电科学研究院（China Institute of Water Resources and Hydropower Research）  
**许可证：**[MIT](LICENSE)

[English](README.en.md)

## 安装

Python ≥3.11，推荐Python 3.12。在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

macOS/Linux使用`.venv/bin/python`。

## 快速开始

运行内置合成水网示例：

```powershell
.\.venv\Scripts\python.exe examples/synthetic_demo.py
```

示例包含两个产流单元、两个需水节点、地下水供水和一条调水工程，模拟24个月；输入和结果分别保存在`examples/data/`和`examples/output/`。

查看模型情景：

```powershell
.\.venv\Scripts\python.exe run_demo.py --list-scenarios
```

使用案例数据运行：

```powershell
$env:WISAM_OUTPUT_DIR = '.\output\case01'
.\.venv\Scripts\python.exe run_demo.py --data-dir '.\data' --start-year 2024 --end-year 2024 --output '.\output\case01\results.xlsx'
```

`WISAM_OUTPUT_DIR`指定生成文件和水文缓存的根目录，应在启动Python前设置。各案例应使用相匹配的输入和缓存。输入格式见[数据接口](docs/DATA_INTERFACE.md)。

## 文档

- [模型概述](docs/MODEL_OVERVIEW.md)
- [数据接口](docs/DATA_INTERFACE.md)
- [运行环境](docs/DEPENDENCIES.md)
- [建模与使用说明](docs/KNOWN_LIMITATIONS.md)
- [示例说明](examples/README.md)
- [数据与示例](DATA_POLICY.md)
- [版本记录](CHANGELOG.md)

## 项目结构

| 目录 | 功能 |
|---|---|
| `api/` | 模型编程接口 |
| `core/` | 节点、连接与网络 |
| `hydro/` | 水文过程 |
| `water/` | 工程运行与用水配置 |
| `data_io/` | 输入与网络构建 |
| `engine/` | 月步长模拟 |
| `engineering/`、`coupling/` | 工程组织与单元耦合 |
| `management/`、`scenario/` | 供水管理、结果与情景 |
| `examples/`、`docs/` | 示例与文档 |

## 校验与引用

执行`python tools/verify_package.py`可核对发布文件的SHA-256校验值。软件运行检查记录位于`verification/`。

使用本软件时，请根据[CITATION.cff](CITATION.cff)引用WISAM，并注明版本号。

