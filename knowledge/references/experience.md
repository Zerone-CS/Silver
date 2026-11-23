# 查询与积累实验经验

实验经验库是存放在自选位置的 SQLite 文件。它索引已完成轮次的部署身份、观测、假设、采纳或回退等结果，以及原始证据的路径和 SHA-256。可让同一组实验共享一个库，按条件找出以往尝试。参数的含义、默认值和执行路径见[参数知识库](workflow.md)。

在套件根目录运行。以下示例假设工作目录下已有 `campaign-a/`，第 3 轮已完成。

```bash
python3 scripts/experience.py add --db ./experience.sqlite --campaign ./campaign-a --round 3
python3 scripts/experience.py search --db ./experience.sqlite --scope W5 --text prefix
```

`add` 调用 `scripts/campaign.py export`，由主控核验完成状态及证据。入库保留导出的完整 JSON、内容指纹和 campaign 路径。库中保存证据引用，原始 trace 等大文件在归档位置保留。相同 campaign ID 和轮次只存一份导出。中断后可重复执行命令。如果相同身份再次导出的内容不同，命令会报冲突，并保留首份记录。遇到冲突时核对两次导出涉及的原件、路径与哈希。

机制推断、额外适用条件和反证可以随时追加。将实测事件或新 run 的引用写入反证，方便下一次核验。下面演示注解的写法，具体内容由该轮证据决定。

```bash
python3 scripts/experience.py add --db ./experience.sqlite --campaign ./campaign-a --round 3 \
  --mechanism '共享前缀未产生复用，可能受当前缓存布局约束' \
  --scope-note '该轮的模型版本、缓存配置和 W5 请求分布'
python3 scripts/experience.py add --db ./experience.sqlite --campaign ./campaign-a --round 3 \
  --counterevidence 'campaign-b 第 2 轮在另一布局观察到命中，见该轮导出的计数器原件'
```

重复提交相同注解会自动去重。查询结果的 `round_record` 保存源记录，其中 `hypothesis` 是实验前假设，`observation` 是随实验登记的观测陈述，`evidence` 引用可以复核的实际材料。`agent_notes` 分列推断、适用条件和反证。回退或放弃的轮次也能查询，解释其原因时要查看当轮有没有完成可比测量、质量检查和部署核验。

`search` 支持组合过滤。`--campaign-id`、`--round`、`--decision adopt|revert|abandon` 精确匹配。`--scope` 在导出的适用条件中匹配子串，`--text` 在源记录及注解中匹配字面子串。默认返回最近入库的 20 条，`--limit N` 调整条数。首次入库前查询返回空列表。用历史结果提出本轮候选，实际收益仍由本轮场景的比较决定。

## 按适用条件查询

旧命令仍可返回原结轮 JSON。增加 `--cards` 后显示每个类别的简要证据，`--limit` 在此模式下按类别计算。可用 `--context` 指定当前任务的结构化条件。

```bash
python3 scripts/experience.py search --db ./experience.sqlite --context current-context.json --limit 2
```

上下文 JSON 可包含 `model`、`model_sha256`、`hardware`、`topology`、`vllm`、`vllm_ascend`、`cann`、`workload_family`、`scenario`、`workload_sha256`、`measurement_mode`、`concurrency`、`requests`、`input_p50_tokens`、`output_p50_tokens`、`max_ttft_s`、`max_e2e_s`、`min_request_score`、`quality_method`、`primary_metric` 或 `goodput_primary`。缺失条件和实测条件冲突分别显示。一次结轮比较中的各场景分别列出，原吞吐和 goodput 的实测结论保留各自的指标名和区间。

索引时从已核验的正式运行及证据中提取字段。文件缺失的旧库仍能使用其导出中保留的字段。需要补录部署或负载族时，可在首次 `add` 或 `sync` 时传入同样结构的 `--context`。这些值只填补空字段，标记为操作方声明。

```bash
python3 scripts/experience.py sync --db ./experience.sqlite --campaign ./campaign-a
python3 scripts/drive_campaign.py --campaign ./campaign-a --resource-lock ./device.lock \
  --experience-db ./experience.sqlite -- ./site-worker
```

`sync` 根据控制器的结轮事件逐轮调用正式导出。对已核验和索引的轮次可重复运行，缺原件的轮次会输出独立错误并可补齐后重试。配好经验库的 driver 会留下私有归档日志，状态结果给出最近一次归档的路径和退出码。归档错误也可以用 `sync` 补做。

## 对照经验库的效果

[有库与无库的配对研究](experience_study.md)冻结同一 Agent 型号、候选范围、历史快照和 NPU 预算，分开记录实际检索与引用。它从候选裁决、占卡回执和停机理由汇总各路径。
