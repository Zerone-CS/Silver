# 知识产物与记录字段

knowledge 交付快照、查询或建设证据。协调者维护实验状态、预算和活动快照引用。以下为交付字段，按任务选用；CLI 产物之外的判断由执行者补充。[任务接口](../../references/suite_contract.md)

| 产物 | 交付内容 |
|---|---|
| `snapshot.json` | snapshot_id、数据目录、逐文件清单及 hash、构建 manifest、查询器与入口 hash、两套部署源码身份及各自的 source_match |
| `queries/<query_id>/` | 问题、固定快照、argv、退出码与 stdout/stderr 引用和 hash、命中与选用卡片的 name/type/scope、路径/hash、选择依据、现场核验或 knowledge_gap；有相关实验时附因子或假设引用 |
| `builds/<build_id>/` | 构建范围、输入与两套源码指纹、构建器/依赖/规则/提示/schema/模型与端点身份、各阶段命令及退出码、日志及产物 hash、预算与实际用量、覆盖去向、碰撞、错误和恢复点 |
| `reviews/<review_id>/` | build 或 snapshot 身份，分别记录结构检查、源码主张核验和输入覆盖对账的范围、结果及未审项 |
| `publications/<publication_id>/` | 快照与审查引用、发布目标、旧/新引用、实际操作状态、生效证据和回退方法 |
| `feedback.jsonl` | claim_id、参数或假设、主张类型、适用条件、源码或 run/trial 引用、配置/环境/负载/契约、质量/SLO/效应与机制状态、矛盾证据及审查状态 |

两套源码的 `source_match` 各取 `exact`（内容一致）、`compatible_verified`（指定主张的相关路径已核验）、`mismatch` 或 `unknown`。后两种仍可提供候选线索。区分源码事实、文档建议、机制推断与实测观察；以 `verified / unverified / contradicted` 记录具体主张及证据。

查询未命中时交付检索范围和缺口。构建按约定范围记录 `planned / running / partial / complete / failed`；保留历史产物，修正使用新版本。结构审查、源码核验、发布和实验切换各自记录实际结果。[操作与命令](workflow.md)
