# 合成水网示例 / Synthetic water-network example

本示例采用人工构造的输入，演示WISAM的模型配置、运行和结果提取。

在项目根目录执行：

```text
python examples/synthetic_demo.py
```

## 系统设置

- 2000—2001年，共24个月。
- 两个产流单元、两个城市需水节点、两个地下水供水节点及一条调水工程。
- 产流单元面积100和80 km²，径流系数0.45；第二年降水为第一年的45%。
- 总需水4200万m³/年；地下水年度基础可采量分别为150和240万m³。
- 调水计划30万m³/月、取水能力40万m³/月、输水损失率5%。

脚本在`examples/data/`生成9个CSV输入，并将`WISAM_OUTPUT_DIR`设为`examples/output/`，隔离示例缓存。重复运行会重新生成示例文件。

## 结果与检查

示例检查逐月供需平衡、非负水量、有限数值、地下水与调水供水，以及干年缺水增加。请使用标准Python模式运行，保留断言检查。

参考运行的两年总需水为8400.0万m³、供水4865.3万m³、缺水3534.7万m³；地下水供水656.0万m³，调水来源供水635.2万m³。显示结果按表格精度舍入。

输出包括城市年度供需、水源构成、月度水量核算、调水汇总、合成水文与需水时序，以及`verification.json`检查记录。

This example uses synthetic inputs to demonstrate model setup, execution and result extraction. Generated inputs and outputs are stored under `examples/`.

