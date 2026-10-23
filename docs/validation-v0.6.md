# v0.6 记忆与实体交付验证

更新日期：2026-10-24；Windows 11、Python 3.11.7。范围见[v0.6 方案](v0.6-plan.md)，模块提交见[开发日志](development-log.md)。本文件区分已实现、合成验证、真实服务单例和尚未验收的内容。

## 功能与定向检查

| 范围 | 已有证据 |
| --- | --- |
| 设置与迁移默认值 | `tests/test_settings.py`：45 passed；默认关闭、mostly，非法值不落盘 |
| 存储与旧记忆兼容 | structured_memory、memory、memory_v02 首批 43 passed；追加来源修正／清空事务回滚 2 passed，重叠集合不累加为全仓成绩 |
| API 与元数据边界 | `tests/test_server.py`：11 passed；候选→实体→接受→删除、身份验证、策略不批准旧候选、监控不包含私人正文 |
| 真实 LangMem 离线路径 | 源码 `--check-memory-runtime`：通过，1 个合成工具提案／1 次假模型调用；没有外部请求，不代表模型质量 |
| 运行与模型协议 | 新 LangMem/runtime 加原 runtime/v02 初轮 71 passed；最终暂停与身份守卫后新模块 27 passed；内联 schema 后适配模块 10 passed。集合有重叠，不累加为全仓成绩 |
| 预算与真实来源标签 | 完整结构存储 29 passed；最后 GLOB 来源匹配调整后相关 2 passed。含设备编号、延后恢复与来源修改 |
| 前端与仓库 | `npm run check` 与已追踪文件／凭据排除检查通过；前端包含新增 structured-memory.js |

Python 后端代码固定为 **`9f57833`**，最终界面修复为 **`2e67a10`**；随后只补交接文档。后端版本的 Windows CI [39986742477](https://github.com/MuzeAnisichael/GreatSage/actions/runs/39986742477) **375 passed、3 skipped**，86.57 秒；后端黑盒、前端语法与仓库检查也通过。首个提前推送的界面切片在运行模块尚未提交时 CI 失败，完整运行切片后的上述检查才是有效依据；最终界面检查见下。

## 最终冻结检查

PyInstaller 首次冻结 `9f57833` 后，包内检查发现实体详情关闭后可能被刷新重开；该候选包没有发布。`2e67a10` 修复后重新收集静态 UI 并打包，Python 代码不变，已有后端结果可复用。日志保存在 `.runtime/v06-final-pyinstaller.log`、`.runtime/v06-final-desktop-build.log` 及 `v06-ui-refresh-*` 文件。冻结阶段 Hugging Face 日志试图访问外部缓存目录被拒，回退控制台；没有为构建下载模型。

| 对象 | 结果 |
| --- | --- |
| 记忆运行依赖 | `--check-memory-runtime` 通过，311 ms；真实 LangMem 0.0.30／trustcall 0.0.39，一次合成提案，不联网 |
| 语音运行依赖 | `--check-voice-runtime` 通过，3997 ms；Pipecat、Deepgram 懒加载及静音管线清理正常，没有设备／识别验收 |
| 冻结后端 | 14/14 通过，合成模型 4 次／工具 1 次，覆盖审批、重启、产物与审计；`.runtime/backend-smoke/20261023T163021Z-0081f4/result.json` |
| 退出 | shutdown／EOF 均成功，469／468 ms，数据库完整；`.runtime/shutdown-smoke/96e9417a88aad939/result.json` |

最终包内界面 **56/56 通过**，`packaged=true`，报告 `.runtime/desktop-smoke/2026-10-23T16-43-19-548Z/result.json`。包含三档设置保存、候选接受／拒绝、冲突导航、字面证据、实体详情与修正跳转、来源删除、请求元数据，以及可控迟到响应下关闭弹窗不再重开。`memory-v06.png` 已检查，无溢出。手动提取按钮的后台状态部分使用界面模拟；真实模型只由上面的独立单例验证，不把两者混为真实桌面语音验收。

首次失败报告为 `.runtime/desktop-smoke/2026-10-23T16-33-18-446Z/result.json`；它复现实体弹窗刷新竞态，不计通过。修复没有削弱原断言，加入确定性竞态回归后再检查最终包。UI 收集文件与源码内容指纹一致。

## 发布产物

从 [v0.6.0-alpha.1 预发布页](https://github.com/MuzeAnisichael/GreatSage/releases/tag/v0.6.0-alpha.1) 下载；随包提供 `SHA256SUMS.txt`。保留全部切片经 [PR #9](https://github.com/MuzeAnisichael/GreatSage/pull/9) 合入 main，标签指向该合并提交。ZIP 仅将构建名补为 x64，没有重新压缩。NSIS 的 Authenticode 状态为 **NotSigned**。

| 文件 | 字节 | SHA256 |
| --- | ---: | --- |
| `GreatSage-0.6.0-alpha.1-win-x64.zip` | 289,557,554 | `9cf252f20597a776efc8dff298f47eb3c37a62d479351ed209edb0f70094a969` |
| `GreatSage-0.6.0-alpha.1-win-x64-Setup.exe` | 215,862,758 | `29619e4bc91a4f2ec525c35aeb3beda52e5aad6059816ca12fb5c36ead896151` |

## 真实模型单例（2026-10-24）

仅使用虚构用户原文“我正在开发星河项目，偏好用中文交流。”，上下文预算 16384、输出上限 1536；未读取用户会话、未改变用户模型配置。按既有授权使用本机保存的 OpenRouter 凭据和已安装 Ollama 模型。

- OpenRouter `openai/gpt-4.1-mini`：**通过**，分别提取项目事实和语言偏好，两个逐字引用通过存储校验，以 mostly 策略落为两条候选，生成两个实体。一次请求约 **4656 ms**；服务返回费用 **0.0005728 USD**。这是单例功能检查，不是性能或一般准确率结论。
- 原默认 `google/gemini-2.5-flash-lite`：单次返回 `rate_limited`，未对该模型重复请求，不计通过。
- Ollama 服务原本未启动；临时启动现有程序，不下载模型。已装 `qwen3:8b` 多次表现为缺失／不合 schema 的结果；内联证据 schema 后最终返回两个原生调用，但参数额外包在 `object` 层，**严格校验拒绝**，没有写入候选或推进进度。因此本版可配置 Ollama 且协议模拟通过，但该安装模型在此样本的提取兼容性未通过。没有通过放宽证据／字段检查掩盖失败。结束后卸载测试模型并关闭本轮临时服务。

报告：`.runtime/memory-live-4c2a153a/result.json`（最终）；早期错误见 `.runtime/memory-live-cb19c1b7/`、`6d9774f7`、`5558e416`、`c90bc1f4` 对应目录。所有原始报告和调试输出留在忽略目录，不上传运行数据。日常提取对确定性错误暂停，需用户调整配置或显式重试。

## 明确的能力边界

- 逐字引用和来源指纹只验证出处，不证明模型结论正确。自动／必要确认档位依赖模型判断，可能漏掉不确定或敏感内容。
- 实体按相同名称与类型分组，不做别名归并、同名消歧。冲突检查按精确实体／属性键，无法普遍发现旧纯文本或语义改写矛盾。
- 删除／拒绝记忆会阻止同一旧来源版本再次自动提取任何新事实；新来源与明确手动添加仍可记忆。保留原文及其他摘要仍可进入历史检索，要彻底移除需删除来源。
- 启用后当前聊天模型处理用户原文；云端可能收费。不支持工具结构化输出的模型会失败并保留进度，不自动更换服务。
- 真实自然语音、Deepgram、设备切换、人工安装／升级／卸载、干净机器和整天运行继续保留 v0.5 未测项。安装器仍是未签名实验性 alpha；包内检查不替代安装流程验收。
- 产品多 Agent／Deep Agents 属于 v0.7，开发时使用协作代理不等于产品具备该能力。
