# InferOptimize 软件交付说明

版本 `20261123-910c-delivery-v1` 适用于 vLLM-Ascend 吞吐优化。交付范围包括受控 A/A 与 A/B、独立确认、逐请求质量与时延约束下的 goodput、profiling 采集和证据复算。[版本源码](https://github.com/Zerone-CS/Silver/tree/b0632d84bedf5cc6975955351ab42e6b9b06d353) · [910C 站点手册](https://github.com/Zerone-CS/Silver/blob/b0632d84bedf5cc6975955351ab42e6b9b06d353/references/delivery.md)

## 获取与校验

[下载已验收的软件包](https://github.com/Zerone-CS/Silver/raw/refs/heads/releases/20261123-910c-delivery-v1/vllm-ascend-throughput-skills.tar.gz)。软件包 SHA-256 为 `5650c0a6595118b9479bca1de2a411f269a8c0b5267d46b61d7da94373b6335d`。本分支文件的摘要列在 [SHA256SUMS](SHA256SUMS)。

源码包来自构建提交 `d1048a9a8399f9b4f90ab219c2a6ba6ae8481cd2`。Silver 的公开快照 `b0632d84bedf` 与该构建提交具有同一 Git 文件树，包含全部 919 个文件。[发布清单](release.json) · [安装校验记录](verify-installed.json)

## 已有验收结果

| 项目 | 验收记录 |
|---|---|
| Linux aarch64，Python 3.9 | 在 910C 主机上执行软件测试，329 项全部通过。[测试日志](full-tests-linux.log) |
| macOS | 328 项通过，1 项按 Python 版本条件跳过。 |
| 离线复算 | 12,418 份历史原件，11 项裁决与原记录一致。其中 7 项采纳、3 项回退、1 项暂缓。[逐项结果](offline-verification-linux.json) |

## 910C 性能验收进度

本版本的正式优化 Round、仓储负载长尾复测、三个留出负载族的经验库盲测均待实机验收。历史复算检验了分析工具重现旧裁决的能力。新版的吞吐收益、长尾成因和经验库节省的 NPU 小时仍需要现场实验给出。
