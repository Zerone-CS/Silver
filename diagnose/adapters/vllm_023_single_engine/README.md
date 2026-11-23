# vLLM-Ascend 0.23 单引擎逐步采集

这个适配器把客户端成功流里的业务请求、引擎内部请求与每一步最终提交的输出位置接起来。适用于镜像 `quay.io/ascend/vllm-ascend:v0.23.0-a3`，默认同步或异步调度器，TP、DP、PP 均为 1，流式 chat 和 `n=1`。每次门控只接受一批全部成功、完全独占服务的请求。为其他版本安装前，应根据实际调度器和输出路径重新实现及验收该探针。[源文件指纹](installed-sources.sha256)

## 安装到专用诊断实例

在 Linux Docker 宿主机，从当前套件目录设置 `SUITE=$(pwd)`。为本实例设置一个新的绝对路径 `CASE`，并创建可由引擎写入的挂载目录。为采集指定一个空闲设备，并沿用已验证的驱动挂载、模型、网络、精度及服务参数。

```sh
SITE="$SUITE/diagnose/adapters/vllm_023_single_engine"
mkdir -p "$CASE"
mkdir "$CASE/probe" "$CASE/profiles"
```

给这个实例的 `docker run` 增加三个挂载和两个环境变量。在容器内执行后续启动命令，这样可保留镜像自带的 CANN Python 搜索目录。

```sh
-v "$SITE:/site:ro" -v "$CASE/probe:/probe" -v "$CASE/profiles:/profiles" \
-e SITE_LEDGER_DIR=/probe -e PYTHONDONTWRITEBYTECODE=1
```

```sh
cd /vllm-workspace/vllm
sha256sum -c /site/installed-sources.sha256 || exit 1
export PYTHONPATH="/site${PYTHONPATH:+:$PYTHONPATH}"
exec vllm serve /model "$@" \
    --scheduler-cls site_scheduler.SiteScheduler --enable-logging-iteration-details \
    --profiler-config '{"profiler":"torch","torch_profiler_dir":"/profiles","ignore_frontend":true,"torch_profiler_with_stack":false}'
```

这里的 `"$@"` 是本实例已有的 `vllm serve` 参数。等待服务就绪，完成预热，确认没有未完成请求。保留服务容器从启动开始的完整 Docker 日志，原生迭代序列的校验以首个迭代为 0。

## 测量、导出和复验

在宿主机准备实际的固定 `$CASE/workload.jsonl` 和本次 `$CASE/diagnostic.metadata.json`，后者须带 `run_purpose=diagnostic`、`profiler_enabled=true` 和唯一的 `profile_capture_id`。该标识可使用 1 至 120 个字母、数字、下划线或连字符。将单 rank 的 `$CASE/spec.json` 按采集手册准备，`diagnostic_run` 填 `benchmark`，`traces` 只含 owner rank `0`。这里的 `$CONTAINER`、`$BASE_URL`、`$MODEL` 都指向这个专用实例。运行采集的 shell 须同时能访问 Docker 和宿主机的导出目录。[采集 spec 和 trace 验收](../../references/profile.md)

```sh
CAPTURE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["profile_capture_id"])' \
    "$CASE/diagnostic.metadata.json")
python3 -B "$SITE/site_export.py" open --root "$CASE/probe" --container "$CONTAINER" \
    --capture-id "$CAPTURE" || exit 1

status=0
python3 -B "$SUITE/diagnose/scripts/profile.py" capture --spec "$CASE/spec.json" \
    --out "$CASE/profile-bundle" --timeout 1200 -- \
    python3 -B "$SUITE/diagnose/scripts/vllm_profile.py" --spec "$CASE/spec.json" \
    --base-url "$BASE_URL" --export-root "$CASE/profiles" --http-timeout 300 \
    --benchmark-timeout 180 --export-timeout 300 -- \
    --model "$MODEL" --dataset "$CASE/workload.jsonl" --metadata "$CASE/diagnostic.metadata.json" \
    --workers 1 --concurrency 1 || status=$?

python3 -B "$SITE/site_export.py" close --root "$CASE/probe" --container "$CONTAINER" \
    --capture-id "$CAPTURE" || exit 1
[ "$status" -eq 0 ] || exit "$status"
python3 -B "$SUITE/diagnose/scripts/profile.py" verify --bundle "$CASE/profile-bundle" || exit 1
python3 -B "$SITE/site_export.py" prepare --root "$CASE/probe" --run "$CASE/benchmark" \
    --suite "$SUITE" --output "$CASE/step-evidence"
```

`--run` 必须是本次**原始 benchmark 目录**，包含 `raw/*.sse`、原始请求、输出和四份运行记录。profile bundle 中的 `diagnostic-run/` 只保存四份运行记录。保留原始 benchmark、probe、profile bundle 与 step-evidence；要在另一台机器复算，将这些原件复制到当地，再执行 `prepare` 写入一个全新目录，使冻结路径与当地一致。失败尝试产生的目录可供排查，每次重试使用新输出路径。

## 证据口径

- `engine.jsonl` 记录准入时的外部/内部 ID 对、正调度批次、真实完成顺序以及 `EngineCoreOutputs.outputs[].new_token_ids` 提交的请求内输出区间。每次开始调度返回的对象与实际完成回调按对象身份配对，跨越门控的步会报错。导出器核查请求准入、调度、完成、提交的行序，以及每条 commit 的请求是否参与同一步。
- `before.log`、`after.log` 记录开关两端的 Docker 日志，`open.json`、`close.json` 固定同一容器和 capture ID。原生 `Iteration(i)` 须从 0 连续，门控的增量与探针的正调度数、完成数一致。这里的 K 统计 `total_num_scheduled_tokens > 0` 的已完成批次，包含窗口内零产出步。调度零 token 的清理循环排除在 K 外，DP dummy 迭代会报错。
- 套件重放原始 SSE，检查当批全部业务请求的 `response_id` 与准入桥一一对应，并将最终成功请求的 `usage.completion_tokens` 与提交区间逐位置对账。最后一个完成的正调度批次若有真实输出，它的完成先于对应客户端响应的结束。若异步调度留下零输出尾步，报告标记 `partial`，保留 N、T 和已观测的位置关系，全窗口 K、R、Y_bar 保持空值。
- `inventory.csv`、`bridges.csv`、`commits.csv` 保留 engine JSONL 物理行号，`seal.csv` 保留原生日志的索引边界与两个 SHA-256。`sources.json` 同时固定上述五份原件与派生 CSV 的 SHA-256。`report.json` 给出 N、T、K、R、Y_bar 和 N/T。`provenance.json` 给出窗口判据及输出文件指纹。
- 单步设备时长和 KV 抢占、重算、缓存命中等工作量需要另外的 profiler 事件。探针和逐迭代 INFO 日志用于诊断，正式吞吐收益用关闭探针和 profiler 的同 case 配对运行验收。
- 引擎原件还记录本地单调时钟和可观测的请求结束事件。用[流式时间分解](../../references/stream_tail.md)通过 SSE 响应 ID 关联准入、首次调度、输出提交和结束，再查看各客户端终态阶段。
