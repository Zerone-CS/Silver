---
name: evaluate
description: 测量 vLLM-Ascend 的有效输出吞吐，校验已有运行，比较服务配置或源码实现的质量、时延和吞吐效果。用于建立基线或验证候选。
---

# 测基线与验效果

| 任务 | 从哪里开始 | 交付 |
|---|---|---|
| 测量（`measurement`） | 核对目标实例、负载和计数口径。优先校验已有 run，需要采集时沿用已授权的服务范围与预算 | 原始 run、校验报告、有效吞吐和时延、基线可信度及缺口 |
| 验证（`validation`） | 核对预定主指标与保护条件、两侧实际部署及变更。按场景检查独立配对运行 | 全部重复和逐场景结论、质量/SLO、收益与不确定性、采纳/回退/补证建议 |

[计量与判读](references/measurement_contract.md) 定义测什么、怎样判断。[工具用法](references/tool_usage.md) 提供从采集到比较的命令。接收其他工具的产物或排查无效输入时查 [v1 数据接口](references/tool_contract.md)。

独立请求直接交付结果。完整优化任务按 [任务接口](../references/suite_contract.md) 报告原件引用、实际用量和未决项，由协调者继续决策并更新基线。可补的关键证据写入 `next_actions`，注明哪项判断依赖它。
