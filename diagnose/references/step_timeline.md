# 将引擎 step 与执行时间线关联

已有封存的诊断 bundle 时，用[时间线工具](../scripts/step_timeline.py)检查慢步上依次出现了哪些调度、runner、设备、通信和等待活动。工具复验 bundle、benchmark 运行 ID、采集工具版本和原始事件 SHA-256。可直接使用随 [profile](profile.md) 产生的 Chrome trace JSON 或 gzip 原件。

```bash
python3 -B diagnose/scripts/step_timeline.py \
  --bundle /data/campaign/round-01/bundle-A \
  --mapping /data/campaign/round-01/timeline-fields.json \
  --run-id 实际诊断运行ID
```

标准输出为 JSON。也可从 Python 调用 `analyze(bundle_dir, mapping_path, run_id, manifest_sha256=benchmark_manifest_sha256, step_keys=logical_keys)`。传入的 manifest SHA 属于 `diagnostic-run/manifest.json`；`logical_keys` 是由账本给出的 `(replica, step_id)` 字符串对。这样只有同一次诊断且与账本相符的步骤会进入对比。直接运行 CLI 时将列出时间线中可关联的步骤。

## 字段与关联依据

下面的映射对应一个**示例导出**。先抽取当前工具版本的实际事件，核对逻辑 step 字段、replica 拓扑和 stage 的符号来源，再保存现场映射。数字 step ID 将转成字符串，保留其余标识原值；每个 replica 内的逻辑 step ID 应在该 run 中唯一。rank 表示并行 worker，replica 表示一次逻辑推理的引擎副本；例如同一 TP 组的两个 rank 可映射到同一个 replica。

```json
{
  "schema_version": 1,
  "tool": {"name": "torch_npu.profiler", "version": "fixture-1"},
  "replica_by_rank": {"0": "engine-0", "1": "engine-0"},
  "step_field": "args.logical_step",
  "correlation": {"producer_field": "args.submission_id", "consumer_field": "args.launch_id"},
  "rules": [
    {"stage": "scheduler", "field": "name", "pattern": "schedule"},
    {"stage": "runner", "field": "name", "pattern": "execute_model"},
    {"stage": "device", "field": "cat", "pattern": "kernel"},
    {"stage": "communication", "field": "cat", "pattern": "collective"},
    {"stage": "wait", "field": "cat", "pattern": "synchronization"}
  ]
}
```

- `tool` 必须与封存 spec 中的工具名和版本一致。事件携带 replica 时，可用 `"replica_field": "args.replica"` 替换 `replica_by_rank`。映射使用当前版本真实的字段。
- `rules` 对字段的整个字符串做正则匹配。一个事件匹配多个不同 stage 时会标为歧义。同步 `B/E` 事件按同一 rank、pid、tid 的嵌套栈配对，来源同时记录开始和结束事件编号。完整 `X` 事件直接取其区间。仅统计与验收窗口有非空交集的区间，跨界部分裁剪并标注 `clipped`。
- 首选事件自身的逻辑键。导出提供主机入队至设备执行的显式关联 ID 时，可加 `correlation`，关联域限定在同一 rank。只有该值指向唯一逻辑步且其余可见字段相容时才分配。保留设备事件和主机产生该 ID 的原始事件索引，供打开原件复查。存在多个互不相干的 ID 命名空间时，应在导出中保留可真正关联的完整复合键。
- 只有可信命令产生的一组新原件或具备共同运行标记的封存导入可以归属到本轮。历史导入若仅由操作员手工配对，会返回 `unavailable`。当原件没有 step ID 或可验证的关联字段时，报告缺项；下次诊断可在调度和执行路径增加共同标记。

## 时间单位与归因边界

单条 span 的时长和同一 rank/pid/tid 上的区间并集可以直接读取。跨主机、设备或 rank 比较时间位置时，在映射中加 `"clock_alignment": {"path": "clock.json", "sha256": "该文件的实际字节哈希"}`。此项表示现场已完成校时，并将它的证据与本次运行绑定。示例回执如下。

```json
{
  "run_id": "实际诊断运行ID",
  "clock": "对应封存spec的时钟名",
  "ranks": ["0", "1"],
  "method": "现场同步或离线校时方法、误差范围与覆盖的主机/设备通道",
  "evidence": {"path": "clock-calibration.log", "sha256": "校时原件的实际字节哈希"}
}
```

`ranks` 的顺序和成员须匹配采集策略。相对路径从各自引用文件的目录解析。保存转换方法、残余偏差和有效时间段供复查。工具检查文件身份和作用域，时钟同步精度由现场证据支持。尚无此回执时，保留逐 lane 并集及原始事件，跨通道或跨 rank 的并集与最后完成顺序保持未知。

报告按 `sources` 列出输入文件和 SHA-256。每个步骤的 `longest_spans` 给出窗口内时长最大的五条原始活动，`event_index` 从零开始。时间字段使用十进制微秒字符串，避免绝对时间戳失去精度。

| 字段 | 含义 |
|---|---|
| `sum_span_us` | 各观测 span 裁剪后的时长之和，保留并发及嵌套的重复时间 |
| `wall_union_us` | 至少有一个归属 span 活动的墙钟时间 |
| `overlap_wall_us` | 至少两个 span 同时活动的墙钟时间，三重重叠也只计一次 |
| `observed_extent_us` | 首个归属 span 开始至最后一个归属 span 结束的时间，可含空隙 |
| `coverage` | 已选/已关联 span 数、遗漏步骤、未见 stage 以及歧义和缺失原因 |
| `longest_steps` | 在有共同时间轴时，按观测跨度列出的前十步，包含最后活动的 rank 与原件位置 |

例如，若某 rank 的通信 span 最晚结束，可回到该步的 runner 和设备事件核对它进入通信的时刻，检查是否有其他 rank 的等待与这段时间重叠。这里给出的是有原件支持的时间关系。确认某个等待原因还需要依赖标记、源码或受控实验。一次诊断 trace 的观察窗口可能只覆盖请求的一部分，其区间不能直接替代正式 A/B 的整批墙钟时间。
