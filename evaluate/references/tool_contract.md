# 工具数据接口 v1

保留原始 run，所有输出使用新路径。修复、重算或格式适配时另存新报告，记录输入 hash、工具版本及替代关系。下列字段由现有工具读取或生成。外部质量、资源、源码或机制证据另附来源。

## 采集输入和原件

- **请求集。** JSONL 每行一个对象，包含唯一的非空字符串 `request_id`、字符串 `prompt_text` 和正整数配额 `output_tokens`。本版每请求一条 user 消息、`n=1`。闭环要求 `arrival_offset_ms` 缺失或 null，开环要求每行有非负有限偏移（毫秒）。`--max-requests` 按文件顺序取前 N 条非空记录。
- **元数据。** `prepare_metadata.py` 从非空 JSON 对象生成 `config_sha256`、`environment_sha256` 和 `metric_contract_sha256`，同时记录输入文件路径与字节 hash。`benchmark.py` 要求三个小写十六进制 SHA-256、非空 `scenario`、`run_purpose`（performance/diagnostic）、布尔 `profiler_enabled` 和 `effective_config_verified`，以及 `quality_status`（pass/fail/not_evaluated）。质量与生效状态由调用方提供。
- **运行原件。** `manifest.json`、`requests.jsonl`、`workload.json`（实际选中的请求数组）、`metadata.json`（传入的原件）是校验所需的四个文件。`raw/` 保存各请求的请求体、SSE 和拼接输出，供复核。只有 `manifest.started.json` 的目录表示未完成，保留现场，用新目录重试。

最终 manifest 的必需字段：

| 字段 | 约定 |
|---|---|
| `schema_version`, `run_id`, `scenario` | 版本整数 1，非空运行 ID 和场景名 |
| `measurement_mode`, `clock`, `start_ts`, `end_ts` | `finite_batch`、`client_perf_counter`、同一时钟的有限秒数，`0 ≤ start_ts < end_ts` |
| `quality_status`, `effective_config_verified`, `profiler_enabled`, `run_purpose` | 与 metadata 中的类型和值一致 |
| `workload_sha256`, `config_sha256`, `environment_sha256`, `metric_contract_sha256` | 对应 JSON 内容的规范 hash，UTF-8、键排序、紧凑分隔、非 ASCII 不转义 |
| `benchmark_sha256`, `metadata_sha256` | 采集器执行源码集合的组合指纹与 metadata 原件的字节 hash；旧采集器保留其原有单文件指纹，不混用版本 |
| `client_measurement_policy`, `client_measurement_policy_sha256` | 完整策略对象及其规范 hash，包括实际请求、并发、workers、超时、采样及计数策略。同一份参数化计量契约可用于不同场景，各场景另存实际客户端策略 |

新版 `benchmark_source_hashes` 保存 `benchmark.py` 和实际导入的 `implementation_identity.py` 字节 hash，`benchmark_hash_algorithm=sha256-canonical-source-map-v1`；对该映射按上述规范 JSON 散列得到 `benchmark_sha256`。

每条 `requests.jsonl` 的必需字段：

| 字段 | 约定 |
|---|---|
| `run_id`, `request_id`, `requested_max_tokens` | 对应 manifest 和 workload。全部请求 ID 及配额须逐一对应 |
| `planned_send_ts`, `send_start_ts`, `end_ts` | 均在窗口内，依次不减，成功和失败均有记录 |
| `response_open_ts`, `first_output_ts`, `last_output_ts`, `output_event_ts` | 前三项可为 null。最后一项为非空载荷事件时间数组，首末元素与首末输出时间对应。无事件时数组为空、首末为 null |
| `first_channel_event`, `server_content_tokens` | 新采集器记录两个输出通道各自首次非空事件在 `output_event_ts` 中的零基下标，缺失通道无下标。可见内容 token 数取自服务端最终 usage 的显式扩展字段，缺失记为 null |
| `actual_prompt_tokens`, `actual_output_tokens`, `token_count_source` | 实测非负整数或 null，计数来源为 `server_usage` 或 null |
| `final_status`, `finish_reason`, `stream_done_received`, `final_usage_received`, `error` | 终态 success/error/timeout/cancelled，两项 received 为布尔值。失败保留错误原因 |

采集成功要求 HTTP 200、SSE 正常结束（`[DONE]`）、`stop` 或 `length`、最终 usage 含 prompt/completion 两个计数，且有效载荷与输出计数相符。role-only、空块、usage 帧均不算首输出。零输出可成功且没有 TTFT 样本。断流即使已有 usage 也不计成功。

## 校验与比较结果

**单次运行。** `validate_run.py` 根据四件原件核对指纹、请求 ID/配额、时间及终态，输出 `schema_version, valid, errors, warnings, manifest, source_hashes, metrics, metric_definitions`。语义校验失败时 `valid=false, metrics={}`。有效报告仍可能含真实失败、未核验的配置或未评估质量。主要指标如下，算法见 [计量与判读](measurement_contract.md)。

- 计数与吞吐：`n_requests, n_success, n_failed, failure_rate, wall_s, effective_output_tokens, output_token_throughput, request_throughput`。
- 成功请求时延：`ttft_p50_s, ttft_p99_s, ttft_sample_count, e2e_p50_s, e2e_p99_s, e2e_sample_count`。没有相应样本时分位数为 null。
- 在途：`observed_mean_in_flight, in_flight_integral_request_s, summed_request_residence_s`。

**配对比较。** `compare_trials.py` 读取预定的 [比较契约](../assets/comparison_contract.example.json) 和 `trial.json`。后者含 `schema_version: 1` 以及 `pairs` 数组，每项为 `scenario, baseline, candidate`，两条路径相对 trial 文件，指向独立的校验报告。工具拒绝复用同一物理文件、有效 run ID 或相同请求记录。各对和跨重复的身份要求见 [比较与交付](measurement_contract.md#比较与交付)。

- 契约含 `min_pairs, confidence, bootstrap_samples, seed, scenarios` 及 `schema_version: 1`。每个场景声明 `primary` 和 `guardrails`，纯保护场景可用 `primary: null`，至少一个场景有主指标。主指标含 `metric, direction, min_relative_improvement`，方向为 higher/lower。保护项可设 `min_value/max_value`，相对退化同时传 `direction, max_relative_regression`。失败率上限需显式声明，基线为零时使用绝对界限。
- 对每个指标和场景，工具按独立配对 run 重采样，估计配对相对效应的等权均值和百分位 bootstrap 区间。各主指标区间下界须严格超过约定收益，每项保护条件均须通过。每个符合资格的候选观测都检查约定上/下限。小样本区间、逐指标置信水平及预设重复数均不能保证未来全部场景表现。
- 读取 `input_valid, recommendation, reasons, errors, mechanism_status`。有效输入另含 `method, scenarios, pairs, validated_inputs` 及两份输入的 hash。诊断 run、开启 profiler、质量未通过或配置未核验均不能晋级。有合格观测违反保护条件或主指标区间表明回退时建议 `revert`，全部合格且逐项通过时建议 `adopt`，其他情况 `defer`。输入错误可产生 `input_valid=false, recommendation=defer` 的错误报告。工具保留 `mechanism_status=unresolved`。

**输出抽检。** `compare_outputs.py` 读取两个独立文件，均为非空 `{prompt_id: output_string}` 对象。逐 ID 比较字符串，报告缺少、多出、不同的 ID、两份字节 hash 及 `passed`。重复键、读取失败、同一文件（含硬链接）属于输入错误。抽检结果的 `quality_status` 始终为 `not_evaluated`。

## 源码或构建产物对照

这是 v1 的可选扩展。`prepare_metadata.py --implementation FILE` 接收非空 JSON 实现清单，记录指定项目的源码/产物路径与 hash，生成 `implementation_sha256`。metadata 和 manifest 保留清单内容、来源及 `implementation_verified`。`--implementation-evidence FILE` 接收 `implementation_sha256, verified, scope, evidence`，最后一项为证据文件路径数组，工具封存其 hash。证据须关联被测实例、实际加载路径及生效版本，未核验时不可晋级。质量报告也须包含相符的 `implementation_sha256`，随运行保存质量归属。

比较契约增加 `implementation_change: {baseline_sha256, candidate_sha256}`，两值必须不同且与所有对应运行匹配。此模式固定两侧服务配置，以及原有负载、环境、计量和采集器身份；同侧跨全部场景和重复固定实现。缺失、混合或未声明的实现字段会被拒绝。完全没有扩展字段的旧参数对照保持原规则。

镜像、驱动/CANN、第三方依赖、工具链和权重继续保留在固定环境中。实现清单只容纳预声明的项目源码及对应构建产物，保留完整 diff。工具核对身份连接和证据完整性，补丁范围及生效证据的真实性由执行者核验。

## 退出码

| 工具 | 0 | 1 | 2 |
|---|---|---|---|
| prepare_metadata | 已生成 | 未使用 | 输入或写文件错误 |
| benchmark | 请求全部成功，原件已保存 | 至少一个请求失败，完整原件仍可校验 | 输入或采集错误，检查完整性 |
| validate_run | 计量有效 | 语义校验失败，读取 errors | 命令、解析或文件错误 |
| compare_trials | 建议 adopt | 建议 defer 或 revert，读取 reasons | 命令、输入或文件错误 |
| compare_outputs | 全部样本一致 | 有差异或缺失/多出样本 | 命令、输入或文件错误 |

保留原调用的 stdout、stderr 和退出码，经管道显示时读取工具自身状态。中断或宿主故障也可能返回其他状态。采纳建议、实际配置切换和实际回退分别记录。
