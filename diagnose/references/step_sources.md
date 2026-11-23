# 从 profiler 导出和运行事件建立逐步证据

## 直接分析 MS Service Profiler 导出

`batch.csv`、`request.csv`、`kvcache.csv` 可以直接读取。将 `RUN` 指向完成校验的 diagnostic benchmark 原目录，`EXPORT` 指向存放这些 CSV 的目录，`TOOL_VERSION` 填现场实际使用的采集器和解析器版本。在仓库根目录执行以下命令，输出文件名应尚未使用。

```sh
python3 diagnose/scripts/step_sources.py prepare --run-dir "$RUN" --ms-dir "$EXPORT" \
    --tool-version "$TOOL_VERSION" --output sources.json
python3 scripts/step_analysis.py run --run "$RUN" --sources sources.json --output analysis.json
```

内置列名依据 MS Service Profiler 26.0.0 的表 3 至表 5。不同安装版本可能导出不同列，运行报告保留实际列的原名、原单位、样本数、极值和原始 CSV 行号。文件按操作员指定的同次采集配对，记录为 `operator_paired`。[官方字段说明](https://github.com/mindstudio-docs/26.0.0/blob/main/msserviceprofiler/docs/zh/msserviceprofiler_serving_tuning_instruct.md) · [vLLM-Ascend 采集指南](https://docs.vllm.ai/projects/ascend/en/main/developer_guide/performance_and_debug/service_profiling_guide.html)

| 原文件 | 可查看的证据 | 解读 |
| --- | --- | --- |
| `batch.csv` | `name`, `during_time(ms)`, `batch_size`, `prefill_scheduled_tokens`, `decode_scheduled_tokens`, `used_blocks` | 按 `hostname`, `dp_rank`, `prof_id`, `name` 分组。`prof_id` 是卡标识。`batchFrameworkProcessing`, `modelExec`, `specDecoding` 表达不同阶段。 |
| `request.csv` | `http_rid`, `recv_token_size`, `reply_token_size`, `execution_time(ms)`, `queue_wait_time(ms)`, `first_token_latency(ms)` | 表内的请求级观测可解释排队、长尾和长度分布。逐步归属仍需每次提交的 step 和输出位置。 |
| `kvcache.csv` | `name`, `used_blocks`, `free_blocks`, `total_blocks`, `blocks_allocated`, `blocks_freed` | 可观察分配/释放事件和占用极值。官方 `rid` 在该文档版本由 MindIE 提供，在 vLLM 环境使用前需核对实际支持。 |

这些表本身没有统一记录逻辑 step、最终输出位置以及 benchmark 的业务请求标识。单独读取时，可得到客户端 `N`、`T`、输出吞吐量及上述原生观察。全窗口逐步分解保持未知。

## 使用现场已有的逐步事件

vLLM-Ascend 0.23 的单引擎独占测试可直接参考[昇腾现场采集适配器](../adapters/vllm_023_single_engine/README.md)。对于从引擎日志转换出的 CSV，可在 mapping 的 `evidence_files` 中登记底层日志的 `path` 与 `sha256`，每次读账和结轮导出都会复验这些原件。

如果引擎已输出逐步事件，可在同次采集加入 `--mapping mapping.json`。下面是**站点示例列名**。替换右侧列名和文件路径，保持左侧含义。每张表的一个数据行对应一个真实的事件、标识关联或计数器记录。文件相对路径从 `mapping.json` 所在目录解析。

```json
{
  "scope": "full_run",
  "step_definition": "每个 DP replica 完成一次 scheduler iteration，含零输出步骤",
  "owner_ranks": {"0": "0"},
  "tables": [
    {"kind": "inventory", "path": "steps.csv", "columns": {
      "capture_id": "capture", "replica": "dp", "owner_rank": "rank", "step_id": "iteration", "duration_ms": "elapsed_ms"}},
    {"kind": "outputs", "path": "commits.csv", "columns": {
      "capture_id": "capture", "replica": "dp", "owner_rank": "rank", "step_id": "iteration", "backend_id": "worker_id", "output_start": "first_position", "output_count": "committed_count"}},
    {"kind": "request_map", "path": "ids.csv", "columns": {
      "run_id": "run_id", "manifest_sha256": "manifest_sha256", "request_id": "request_id", "response_id": "response_id", "sse_sha256": "sse_sha256"}},
    {"kind": "bridge", "path": "engine_ids.csv", "columns": {
      "capture_id": "capture", "backend_id": "worker_id", "response_id": "api_id"}},
    {"kind": "work", "path": "request_work.csv", "columns": {
      "capture_id": "capture", "replica": "dp", "owner_rank": "rank", "step_id": "iteration", "backend_id": "worker_id", "prefix_hit_tokens": "prefix_hits", "num_preemptions": "preemptions"}},
    {"kind": "seal", "path": "window_counters.csv", "columns": {
      "capture_id": "capture", "replica": "dp", "expected_steps": "completed_iterations_in_window"}}
  ]
}
```

```sh
python3 scripts/step_analysis.py request-map --run "$RUN" --output ids.csv
python3 diagnose/scripts/step_sources.py prepare --run-dir "$RUN" --ms-dir "$EXPORT" \
    --mapping mapping.json --tool-version "$TOOL_VERSION" --output site-sources.json
python3 scripts/step_analysis.py run --run "$RUN" --sources site-sources.json --output site-analysis.json
```

- `request-map` 从留存的成功响应 SSE 重放，生成业务 `request_id` 和实际 API `response_id` 的对应关系，将 `ids.csv` 放在映射指定位置。如果现场事件只有引擎内部 ID，应在请求入队边界记录 `backend_id` 与 `response_id`。若现场字段直接携带同一业务请求 ID 或 API 响应 ID，可直接映射到相应字段，并省去多余的桥接表。
- `inventory` 每个 `(replica, step_id)` 有一条可信 owner 的事件。记录全部逻辑步，包括没有生成输出的步；含多次调度的 capture 需要稳定且不复用的 step 标识。`owner_ranks` 为每个独立 replica 选一个记录者，过滤 TP/PP 的镜像行。`duration_us` 和 `duration_ms` 二选一，支持缺测。多卡、多个阶段的时长可以重叠，总吞吐使用 benchmark 原始墙钟。
- `outputs` 在最终 token 确定并交给请求输出队列时记录，使用请求内从 0 开始的半开区间 `[output_start, output_start + output_count)`；一次可提交多个 token。记录的 token 口径必须与 benchmark 的完成响应 `usage.completion_tokens` 一致，包括该运行实际计入的特殊 token。一次无输出的 step 只写 inventory。工作项、草拟候选、SSE chunk 数、文本长度、batch size 或请求总长度均不能提供这一区间。
- `work` 可选，每个 `(replica, step_id, request_id)` 仅一条工作记录。支持 `prefill_tokens`, `decode_tokens`, `draft_tokens`, `accepted_draft_tokens`, `recomputed_tokens`, `prefix_hit_tokens`, `num_preemptions`。使用该请求该步实际增量，缺测字段留空。KV block 水位和队列侧的累计量保留在原生观察中。
- `seal` 从独立的引擎整窗计数器或完成汇总读取每个 replica 的总步数。采集范围须覆盖特定 benchmark 完整执行窗口，包括零输出的步。由 inventory 行数生成的文件无法证明是否漏采。抽样窗口使用 `scope: partial`。ID、列或封口缺失时，检查报告中的 `coverage` 与 `unresolved`。
- 每张现场表都应有原始的 `run_id` 或 `capture_id`。只接受当前 benchmark 的 `manifest.json.run_id` 或 `metadata.json.profile_capture_id`，其他窗口行会跳过。提供 `manifest_sha256` 时，属于同次运行的行必须匹配。多类事件混在一张表时，可用 `"where": {"event": "output_commit"}` 筛选。对于已物理分片且身份已知的文件，`constants` 仅支持 `replica` 与 `owner_rank`。
- `prepare` 冻结源文件绝对路径、SHA-256、生产者版本和运行 manifest 的 SHA-256。报告可从 `sources` 与每条 `source.path` / `source.line` 追溯，含跨行 CSV 引号时行号指记录结束的物理行。哈希可以复核输入字节，字段及窗口含义仍需根据采集器版本和站点事件定义确认。
