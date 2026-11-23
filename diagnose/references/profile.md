# 采集并验收 profiling

`diagnose/scripts/profile.py` 接收每个预期 rank 对应的一份 Chrome Trace Event 时间线。支持纯事件数组、带 `traceEvents` 的对象和这两者的 gzip 文件。典型来源是 vLLM-Ascend 内置 PyTorch Profiler 生成的 `ASCEND_PROFILER_OUTPUT/trace_view.json`、`export_chrome_trace` 导出或 CANN `msprof` 已经导出的时间线 JSON。原始 `PROF_*`、SQLite、CSV、protobuf 和多分片数据先用实际安装版本的导出工具转为适配格式。导出命令可以加入下文的采集作业。[Ascend PyTorch Profiler 输出说明](https://github.com/Ascend/pytorch/blob/master/docs/en/developer_notes/ascend_pytorch_profiler_user_guide.md) · [vLLM-Ascend 服务 profiling 指南](https://docs.vllm.ai/projects/ascend/en/v0.21.0rc/developer_guide/performance_and_debug/service_profiling_guide.html)

## 接入 CANN msprof

使用现场 CANN 工具预先导出每 rank 的 `msprof_*.json` 时间线，并保留安装版本记录和设备与 rank 的对应证据。若采集和导出一起执行，使用下文的现场命令模式并让命令生成新的诊断运行、时间线和版本记录。导入既有原件时使用同一命令的导入模式。不同 CANN 版本的采集和导出参数以现场版本为准。[CANN 时间线导出](https://www.hiascend.com/document/detail/en/canncommercial/800/devaids/profiling/atlasprofiling_16_0021.html)

spec 的 `tool` 示例为 `{"name":"msprof","version":"8.3.RC1","version_evidence":"cann-version.info"}`。版本记录使用相对于 spec 的路径，接受安装包版本文件或现场版本查询输出，指定的版本文本必须出现且作为独立值。采集器将记录归档为 `raw/tool-version.txt` 并核验哈希。两组的策略比较使用工具名称、版本和证据方式。文件随采集路径变化时仍可对照。

按导出中的真实字段声明 `rank_field`，按实际设备和工具时间域声明 `window.clock`。一份时间线包含多个 rank 时，先用现场导出或转换步骤生成逐 rank 时间线，并保留原文件与转换记录。现场提供时钟对齐依据后，才可解释重叠窗口内的跨 rank 时序。捕获无法提供运行标记时，报告保留现场命令生成或人工配对的证据级别。

## 准备采集说明

下面的 `spec.json` 适用于双 rank、存在分布式 worker 原生 `profiler_info_0.json` 和 `profiler_info_1.json` 的导出。用现场的实际包版本、路径、事件类型和所需数量替换示例值。首次采集可以要求全体 rank 的有效事件时间轴至少重叠一定时长。例中要求重叠 1 毫秒。

```json
{
  "schema_version": 1,
  "format": "chrome-trace-v1",
  "tool": {"name": "torch_npu.profiler", "version": "2.7.1"},
  "diagnostic_run": "diagnostic-run",
  "traces": {
    "0": "worker0/ASCEND_PROFILER_OUTPUT/trace_view.json",
    "1": "worker1/ASCEND_PROFILER_OUTPUT/trace_view.json"
  },
  "rank_field": {"scope": "ascend-profiler-info", "path": "profiler_info_{rank}.json"},
  "window": {"clock": "aligned-trace-us", "mode": "trace_extent", "min_duration_us": 1000},
  "requirements": [
    {"id": "matmul", "field": "name", "pattern": "(?i)matmul", "min_count": 1}
  ]
}
```

`diagnostic_run` 和所有输入路径以 spec 所在目录为基准。诊断工作负载用 evaluate 的同一采集入口运行，元数据设置 `run_purpose=diagnostic`、`profiler_enabled=true`。复验工具读取并验证该运行的 manifest、requests、workload 和 metadata，从中提取 run、场景、环境、配置、工作负载、客户端策略和可选实现身份。包版本及 trace 时钟域名称由采集方声明。保留版本查询原件和时钟同步依据。

- 对 Kineto 中包含 `distributedInfo.rank` 的对象，将 `rank_field` 设为 `{"scope":"document","path":"distributedInfo.rank"}`。对事件携带 rank 的导出，使用 `{"scope":"event","path":"args.rank"}`，每份导出出现的该字段值必须与预期 rank 一致。设备编号或 PID 与业务 rank 的对应关系需要运行现场的证据。
- `ascend-profiler-info` 从每份原生 worker 导出旁的 `profiler_info_{rank}.json` 核对 rank 并归档。目录结构按上面的例子保留。如果该版本使用其他结构或省略单卡 rank 元数据，选择有实际 rank 字段的导出或保留现场关联标记。采集器会报告无法核实的 rank。
- 数值窗口示例为 `{"clock":"aligned-trace-us","start_us":"1000000.25","end_us":"1100000.25"}`。Chrome 事件的 `ts` 和 `dur` 均以微秒计，支持数值和十进制字符串。较大的时间戳宜用字符串保留精度。每个 rank 的非元数据事件须覆盖窗口两端。标记窗口要求各 rank 在同一已对齐的 trace 时钟上各有一个开始事件、一个结束事件，工具取各窗口的共同交集，再逐 rank 计数。校时或客户端与设备的时间换算由现场工具提供。
- `trace_extent` 用每个 rank 首个有效时间戳到最后一个事件结束的区间，再取所有 rank 的交集。验收门槛为该交集满足 `min_duration_us`，且其中每一条逐 rank 事件要求达标。该范围可包含预热和收尾事件。需要专门检验 decode 等阶段时，可以事先指定真实可观测到的唯一标记，例如 `{"clock":"aligned-trace-us","start_event":"case.begin","end_event":"case.end"}`。
- 每条 requirement 对指定字段做正则搜索，例如 `field: "args.Task Type"`；每个 rank 都须达到 `min_count`。只计完整落在左闭右开窗口内的 `X` 区间、瞬时 `i/I`、计数器 `C` 事件。需要核验通信覆盖时，应声明通信事件及识别它的字段；模型相关的事件名称、阈值由本轮计划确定。解析器以单 rank 的完整 JSON 为内存单位。
- 可以在 spec 增加 `"run_binding":{"scope":"event","path":"args.capture_id","source":"profile_capture_id"}`。采集作业在诊断 benchmark 的 metadata 写入唯一 `profile_capture_id`，同时写入每个 rank 的 trace 事件或文档字段；另一种来源是 benchmark 实际的 `run_id`。复验会逐 rank 交叉核对，成功状态为 `trace_field_verified`。历史导出缺少共同字段时，`operator_paired` 记录由导入者配对。现场新目录采集的状态为 `fresh_command_outputs`。两者的精确时间对应关系须结合现场记录解释。

## 直接采集正在运行的 vLLM-Ascend 实例

当实例已经启用适用于本版本的 Torch/NPU profiler，并且采集机能看到服务端的 profiler 输出目录时，可调用内置适配器。vLLM-Ascend 的在线接口为直接向实例发送 `POST /start_profile` 和 `POST /stop_profile`；端点会在启用 profiler 后注册。新版启动参数为 `--profiler-config`。不同版本的启动方式和输出路径以实际部署为准。[vLLM-Ascend 服务 profiling 指南](https://docs.vllm.ai/projects/ascend/en/main/developer_guide/performance_and_debug/service_profiling_guide.html)

从套件根目录执行，替换服务模型名、实例地址、实际 profiler 导出目录及与正式计分一致的 benchmark 参数。诊断 metadata 由 evaluate 生成，设置 `--profiler-enabled on --run-purpose diagnostic`。capture 的现场命令在本机运行。

```sh
case_dir=/data/profile/round-01
python3 diagnose/scripts/profile.py capture \
  --spec "$case_dir/spec.json" --out /archive/round-01/A-profile --timeout 1200 -- \
  python3 diagnose/scripts/vllm_profile.py \
  --spec "$case_dir/spec.json" --base-url http://127.0.0.1:8000/v1 \
  --export-root /srv/vllm/profiles --api-key-env OPENAI_API_KEY --benchmark-timeout 300 -- \
  --model my-served-model --dataset "$case_dir/workload.jsonl" --metadata "$case_dir/diagnostic.metadata.json" \
  --concurrency 4 --workers 4
python3 diagnose/scripts/profile.py verify --bundle /archive/round-01/A-profile
```

适配器先登记输出目录中已有的 `*_ascend_pt` worker，然后启动 profiling、执行实际的 `evaluate/scripts/benchmark.py`，最后停止 profiling。benchmark 失败或自身超时后也会尝试停止。服务拒绝启动、停止失败、缺 rank、同一 rank 对应多个新 worker 或导出未稳定时返回非零。它只从本次新 worker 复制 `ASCEND_PROFILER_OUTPUT/trace_view.json[.gz]` 及同一 worker 的 `profiler_info_{rank}.json` 到 spec 路径，保留原始导出并在 stdout 记录对应关系。源和目标使用相同压缩格式。单设备环境若只有不带 rank 的 `profiler_info.json`，可按上文的文档/事件字段方式归档，并用实际 rank 证据验证。

每个实例及其输出目录在采集期间由该作业独占。多节点部署需要逐实例安排控制及可见的原件。外层 timeout 应给内部 benchmark、停止请求、导出等待和归档留出时间；本机被强制关停后，须现场核对服务端 profiler 状态。

## 冻结策略、自定义作业与复验

下列命令从套件根目录执行。目录 `/data/profile/round-01` 示例中存放 spec 和现场作业。将路径替换为本次实际目录。

```sh
python3 diagnose/scripts/profile.py policy --spec /data/profile/round-01/spec.json > /data/profile/round-01/policy.json
python3 diagnose/scripts/profile.py capture --spec /data/profile/round-01/spec.json --out /archive/round-01/A-profile --cwd /data/profile/round-01 --timeout 600 -- python3 collect_case.py
python3 diagnose/scripts/profile.py verify --bundle /archive/round-01/A-profile
```

`collect_case.py` 代表当前部署的实际采集作业。它控制本实例的 profiler 启停、执行诊断 benchmark、等待 profiler 导出结束，并将动态命名的 worker 目录放到 spec 写明的位置。采集器直接执行传入的 argv，记录 stdout、stderr、工作目录、退出状态和时间。开始前逐一检查诊断目录及预期输出尚未创建。超时结束该作业所在的本地进程组；远端服务清理由作业及协调者按实例归属处理。

已经完成导出的运行可省略命令，直接从原件归档。使用新的输出目录，避免覆盖旧尝试。

```sh
python3 diagnose/scripts/profile.py capture --spec /data/profile/round-01/spec.json --out /archive/round-01/A-import
```

bundle 含冻结的 `spec.json`、采集 `manifest.json`、`raw/` 内逐 rank 导出、`diagnostic-run/` 内四份计量原件和派生的 `profile-report.json`。在外置原件盘上也可以直接生成 bundle。复验重新检查文件 SHA-256、benchmark 身份、rank 归属、采集窗口和事件数量。报告提供 manifest、spec 和共同策略的 SHA-256，以及每个 rank 的窗口与计数。将 manifest SHA 与 round 记录一起固定，才能发现整个 bundle 被替换。元数据引用的部署观测及服务日志应与其引用路径一同保存。

冻结的共同策略覆盖工具与声明版本、rank 集及识别字段、可选运行关联方式、窗口时钟与窗口定义、事件要求。数值窗口固定长度并允许绝对位置随运行移动。诊断 run 的采样只用于瓶颈判断，正式收益使用同 case 下关闭 profiler 的独立对照。采集异常和证据不足均以非零退出，已创建的目录保留现场数据，下一尝试使用新的目录。
