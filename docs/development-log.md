# 开发日志

每个迁移切片记录修改、验证与剩余边界；版本交付见 [CHANGELOG](../CHANGELOG.md)，未来工作见[路线图](roadmap.md)。日志与对应改动同一提交，提交标识由 Git 历史追溯。

## 2026-10-17 · 模型适配迁移

- 使用 `langchain-openai` / `langchain-ollama` 及其官方 SDK，替换自写 SSE／NDJSON 聊天协议解析；ASR、TTS、嵌入接口保持现有实现。
- 保留共享桌面 HTTP 连接池、Windows 代理、显式配置的密钥、总超时、即时关闭流、安全错误和实际费用字段。SDK 自动重试关闭，任务层继续负责重试；显式关闭继承自环境的 LangSmith 追踪，默认不上传请求正文。
- 两处受测试约束的兼容补丁：保留 OpenRouter 费用字段；补齐 Ollama 工具结果的工具名称。框架版本已固定，冻结构建配置加入模型配置文件和包元数据。
- 协议测试中的 Ollama 模拟响应补全官方协议要求的 `message.role`；工具调用 ID 接受 SDK 生成值。无效 SSE JSON（包括非 JSON 的 error 事件）统一安全报告 `invalid_json_response`；缺失完成标志仍拒绝成功。
- 验证：全量 **213 passed、5 skipped**（21.45 秒），新增 HTTP 错误不隐式重试、环境账号／密钥隔离、禁止远端追踪和 Ollama 不完整流回归。未把模拟协议测试当作真实 API、Ollama 或安装器验收。

## 2026-10-16 · 迁移基线与范围

- 以 GitHub `main` 的 `5095c04` 为基线，新建 `codex/framework-migration` 分支；开始前本地无未提交改动，与远端一致。
- 用户接受渐进迁移，确认每个模块按小切片提交、持续更新日志。范围记录在 [v0.4 方案](v0.4-plan.md)。
- 基线：Windows / Python 3.11，`pytest -q --basetemp .runtime/pytest-baseline-20261016-a --tb=short`：**209 passed、5 skipped**（17.15 秒）。这不包含真实设备验收。
- 默认系统临时目录首次运行出现写入／替换权限错误；改用仓库内已忽略的临时目录后全部恢复，没有为此修改产品代码。
- 后续首先迁移模型适配与任务编排，再接入标准工具和本地监控；语音、实体记忆和多 Agent 分阶段实施。
