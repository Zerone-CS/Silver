# 测量经验库对 Agent 搜索的作用

以新负载族内的配对轨迹为比较单位。组间共用 Agent 型号及设置、工具接口、负载、候选菜单、正式比较契约和预算。两组都看到同样的查询接口。历史组看到冻结结果，可选择引用或忽略，对照组看到空结果。每条轨迹使用新 Agent 上下文，后续反馈来自本轨迹的测量。操作方保管分组与原始稿，只给 Agent 发送 `packet` 的内容。

预先准备每个负载的同一基线和过质量预检的负载。下面的最小规格使用了三个重复，同一结构可增加多个负载族。真实研究在封存前登记全部负载族、质量与收益门槛、筛选和确认配对方案，以及执行环境。按实际租约估算两个阶段能启动的最低剩余预算。基线占卡费等额计入各轨迹。

```json
{
  "schema_version": 1,
  "seed": 20261124,
  "agent_model": "agent-model-id",
  "agent_settings": {"reasoning": "medium"},
  "tools": ["experience.search", "fixed_candidate_menu", "screen_and_confirm"],
  "budget_npu_seconds": 2400,
  "max_candidates": 2,
  "trajectories_per_arm": 3,
  "families": [{
    "id": "warehouse-holdout",
    "workload": "cases/warehouse.jsonl",
    "brief": "cases/warehouse-baseline.json",
    "context": {"hardware": "910C", "workload_family": "warehouse", "measurement_mode": "closed_loop"},
    "candidates": ["seq8", "prefix"],
    "baseline_goodput_tps": 32.83,
    "baseline_npu_seconds": 212.72,
    "minimum_next_npu_seconds": 200,
    "minimum_confirm_npu_seconds": 1000
  }]
}
```

`brief` 是给两组的相同基线摘要，可附带正式契约指纹和负载分组。新负载不能复用历史库已登记的正式 workload 指纹。不同负载族需要不同 workload。样本数、诊断配置和功效应针对真实任务预登记。

```bash
S=/data/experiments/experience-ab
python3 scripts/experience_study.py init --study "$S" --spec study.json --library experience.sqlite
python3 scripts/experience_study.py dispatch --study "$S"
python3 scripts/experience_study.py packet --study "$S" --trajectory "$ID" > agent-packet.json
python3 scripts/experience_study.py choose --study "$S" --trajectory "$ID" --choice agent-choice.json
python3 scripts/experience_study.py observe --study "$S" --trajectory "$ID" --receipt stage-receipt.json
python3 scripts/experience_study.py report --study "$S"
python3 scripts/experience_study.py export --study "$S" --output /data/experiments/experience-ab-export
```

`dispatch` 是操作方专用的、按种子安排的负载族、配对及顺序。每条轨迹有不含组名的 ID。Agent 返回下面的 JSON，只有实际引用的历史 `record_id` 才写入 `record_ids_used`。

```json
{
  "candidate": "seq8",
  "reasoning": "并发超过已核实的序列上限",
  "prediction": "每步有效输出和达标输出速率会上升",
  "falsifier": "独立确认未过 goodput 和原吞吐约束",
  "record_ids_used": []
}
```

每次筛选或确认使用站点执行器和正式核验器。新阶段收尾后登记回执，`charge` 和 `assessment` 可相对回执所在目录。占卡原件提供单调时钟区间和释放核验，检查区间与金额相符且不和其他候选重复。候选运行 ID 从占卡原件的 `measurements` 或正式报告的 `runs` 获得。研究记录器核验声明与提交的比较报告一致，生产实验仍应将正式原件和评估器一起归档复算。

```json
{
  "schema_version": 1,
  "candidate": "seq8",
  "stage": "screen",
  "decision": "adopt",
  "goodput_tps": 61.34,
  "charge": "runs/step-1/charge.json",
  "assessment": "runs/step-1/decision.json"
}
```

筛选通过后登记新的独立确认，确认建立收益时自动结束。筛选失败后可取下一份 `packet`。Agent 可以选择 `stop`。操作方也可用 `finish --reason budget --note '…'` 登记已低于阶段启动预算的情况。基础设施故障用 `infrastructure_failure`，明确取消用 `cancelled`。两组尚未完成的配对按未完成报告。

报告分开记录历史接口可用、检索返回和 Agent 引用，按负载族汇总预算内确认比例、已确认样本的 NPU 小时、第一次确认前的累计开销、无效候选与最终 goodput。未确认的时间为截尾，最终 goodput 使用冻结基线。JSON 导出封存原始规格、基线摘要、SQLite 快照、提案、测量回执和对应内容哈希，需放在受控目录。
