# 复算流式请求终态长尾

对已归档的正式或诊断 benchmark 运行，可生成逐请求分段和长尾名单。

```sh
python3 -B diagnose/scripts/stream_tail.py --run /archive/run-B --long-tail-s 1.0 > /archive/run-B-stream-tail.json
```

报告按 E2E 从大到小排序。长尾名单按最后非空输出至请求结束是否达到给定秒数生成。客户端通过同一个单调时钟记录最后非空 SSE 事件、包含结束原因的事件、最终用量、`[DONE]` 和请求结束。一个事件带多个标记时允许相同时间戳。标记时间是客户端读到完整 SSE 事件的时刻，其间还可能含服务端、传输和客户端读取延迟。正式吞吐和 goodput 判据仍使用完整批次窗口和请求终态。

报告区分已完整采集、部分标记缺失及历史原件三种覆盖。历史原件可复算最后非空输出至请求结束的总空窗，各终态子段留空。零非空输出以及超时等情况保留请求结局和所有可用时间戳。

## 关联现场证据

在[单引擎探针](../adapters/vllm_023_single_engine/README.md)完成导出后，使用原始诊断运行和同一 capture 的证据目录。

```sh
python3 -B diagnose/scripts/stream_tail.py --run /archive/diagnostic/benchmark \
  --engine-evidence /archive/diagnostic/step-evidence > /archive/diagnostic/tail-report.json
```

工具重放留存 SSE，核对响应 ID，再关联引擎准入、首次调度、首末输出提交和引擎结束事件。旧引擎日志没有时标时对应字段为空。可选的 `--api-evidence` 接受现场 API 探针的单时钟 JSON，应和原始探针输出一起归档。

```json
{
  "schema_version": 1,
  "run_id": "<run id>",
  "manifest_sha256": "<benchmark manifest SHA-256>",
  "clock": "api-local-monotonic-ns",
  "events": [{
    "response_id": "<SSE response id>",
    "last_output_written_ns": 10000,
    "done_written_ns": 20000
  }]
}
```

引擎、API 和客户端按请求身份对齐，各自保留时钟域。跨时钟阶段需要额外校时证据。成功匹配的响应由原件支持，缺少响应 ID 或探针事件的请求保留空值。
