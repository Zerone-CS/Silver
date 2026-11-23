# 安装、注册与验证

以下命令从实际套件根目录运行。evaluate 的五个脚本使用 Python 3.9+ 标准库，测量操作见 [工具手册](../evaluate/references/tool_usage.md)。

```bash
python3 scripts/verify_suite.py
python3 scripts/run_tests.py --scope evaluate
```

核验器检查清单中的文件 hash、入口、契约、链接和代码语法。工作目录根部的个人报告（HTML、PDF、PNG、DOCX、XLSX）、reports/ 与编辑器配置不属于分发包。

逐条检查命令退出码；资源身份不匹配时先核对套件版本与文件。

## 注册入口

将 `AGENT_SKILLS_DIR` 设为当前 agent 的真实发现目录，然后执行。

```bash
python3 scripts/register_skills.py --skills-dir "$AGENT_SKILLS_DIR"
```

注册器生成主入口和四个模块的薄索引，指向实际 SKILL.md。名称冲突或已编辑索引会在写入前拒绝。套件搬迁后重新注册并核对 loader 实际加载路径；当前会话可能仍持有旧内容。迁移旧 measure/profile/validate 索引时先确认归属并备份，注册器保留这些旧目录。

## 知识运行时

按需准备 knowledge 依赖。内置离线轮子匹配 Linux aarch64 / CPython 3.9，其他环境使用兼容解释器和依赖。知识命令及来源限制见 [工作流](../knowledge/references/workflow.md)。

```bash
python3 scripts/setup_runtime.py
.runtime/bin/python knowledge/scripts/kb.py --help
.runtime/bin/python scripts/run_tests.py --scope suite
.runtime/bin/python scripts/run_tests.py --scope knowledge
```

安装器根据 manifest 核验资源并离线安装到根 `.runtime`，宿主 Python 需能创建匹配的 venv。`--rebuild` 隔离重建并保留旧环境。搬迁或复制后核对解释器和实际 import 路径，需要时重建。

`--scope suite` 检查结构、注册及 CLI，需 YAML 等知识依赖，但不调用外部模型。`--scope knowledge` 包含知识模块与 KB 内部测试，`--scope all` 运行全部登记测试。测试验证离线行为，现场收益仍需实际对照。
