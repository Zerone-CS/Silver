---
name: diagnose
description: 根据 vLLM-Ascend 的运行记录、日志或 trace 解释吞吐现象，核对证据范围，提出可检验的原因或下一项观测。
---

# 诊断吞吐现象

从具体问题和已有运行结果出发。解析 trace 或日志时使用 `profiling`，解释现象时使用 `diagnosis`。给出观测来源、适用范围和能回答的问题。若原因仍不确定，指出哪项观测或受控改动能区分现有解释。[诊断记录](references/records.md)

独立请求直接交付分析。完整优化中，把结论或补采建议交回协调者，沿用本次实验的负载、实例和预算安排下一步。[主入口](../SKILL.md)

自动采集、导入与复验多 rank trace，使用[profiling 采集器](references/profile.md)。

流式请求出现终态长尾时，使用[流式时间分解](references/stream_tail.md)从原件区分最后非空输出、结束原因、最终用量和完成标记。带现场探针的诊断运行还可以按 SSE 响应 ID 关联服务端证据。

解释有效输出或吞吐变化时，先复算整批 N/T 和逐请求结局，再按现场证据关联调度步、KV 事件和执行时间线。使用[吞吐对账工具](references/step_analysis.md)查看可归属的 token、慢步和缺项。
