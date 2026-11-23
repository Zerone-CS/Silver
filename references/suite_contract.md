# 任务接口

[主入口](../SKILL.md)维护目标、总预算、基线、活动任务与最终决策。模块产生自己的局部结果，由协调者验收后写入状态。同一 agent 可完成全部工作；委派时传入实际 skill 路径和必要上下文。

## 能力与兼容角色

| 模块 | 历史 role | 公开契约 |
|---|---|---|
| evaluate | measurement、validation | [计量](../evaluate/references/measurement_contract.md)、[数据与比较工具](../evaluate/references/tool_contract.md) |
| diagnose | profiling、diagnosis | [观测与解释](../diagnose/references/records.md) |
| optimize | design | [设计、实施与回退](../optimize/references/experiment_contract.md) |
| knowledge | knowledge | [查询与建设记录](../knowledge/references/records.md) |

`mode` 表示工作范围，`observe` 读取或解析，`diagnose` 分析，`prepare` 准备方案，`execute` 实施已委托动作。`design` 可按 goal 委托设计、实施或回退。保留这些字段供旧任务继续使用，单个 agent 的短任务无需生成整套交接文件。

## 输入与返回

[输入模板](../assets/handoff-input.example.json)包含 task_id、parent_task_id、campaign_root、role、goal、mode、artifacts、scenario_scope、constraints、budget、service_access、expected_outputs 和 return_conditions。提供具体问题、必要原件、实例与操作范围、资源预算及完成条件。路径相对 campaign_root 或使用绝对路径，固定输入附 hash；不传凭据。

[结果模板](../assets/handoff-result.example.json)包含 task_id、role、input_ref、status、artifacts、observations、claims、uncertainties、next_actions、decision_recommendation、budget_used 和 side_effects。返回实际生成的产物、观测与解释、证据缺口、成本和已发生的变更或恢复。未知值保留未知。

status 使用 `completed`、`needs_evidence`、`blocked` 或 `failed`，分别表示委托完成、待补证、外部依赖和执行失败。完成委托后仍需分别核验数据、质量与效果。建议可以是 adopt/revert/defer，最终决定由协调者记录；回退执行结果须有恢复后身份和健康证据。

## 接续

协调者核对返回产物和现场副作用，登记一次成本，更新 [状态](campaign_state.md) 并继续下一项已授权动作。设计后接实施，实施后接评估，决策后落实采纳或恢复。缺证据时选择补采或其他可推进方向，受外部依赖阻塞时保存恢复条件。

同一实例的变更、重启、profiling 与压测由协调者排他安排。模块可在委托范围内查询固定知识快照；建设或发布按实际委托执行。固定中的契约、原件和来源版本保持可追溯，更新定义时注明兼容性。完整任务通过 [Round 控制器](round_control.md)提交状态变化。站点负责设备预留，同机驱动共享资源锁。
