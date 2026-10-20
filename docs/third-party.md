# 语音依赖与分发说明

本页记录 2026-10-21 引入的语音依赖，不是项目全部依赖清单。GreatSage 自身采用 MIT；第三方组件分别适用其原许可证。冻结后端随附本页和下表组件的发行元数据，不改变上游归属或许可。

| 组件与本次版本 | 许可 | 来源 |
| --- | --- | --- |
| Pipecat 1.13.0 | BSD-2-Clause | [官方源码](https://github.com/pipecat-ai/pipecat) |
| Deepgram SDK 7.14.0 | MIT | [官方源码](https://github.com/deepgram/deepgram-python-sdk) |
| Python-SoXR 1.0.0 | LGPL-2.1-or-later | [对应源码](https://github.com/dofuuz/python-soxr/tree/v1.0.0)、[源码发行包](https://pypi.org/project/soxr/1.0.0/#files) |
| sentencex 1.0.32 | MIT | [官方源码](https://github.com/wikimedia/sentencex) |
| loudness 0.2.0 | MIT | [官方源码](https://github.com/iver56/loudness) |

## SoXR 的许可与替换

Python-SoXR 的版权归 Myungchul Keum；libsoxr 的版权及 PFFFT／FFTPACK 归属以随包原文为准。本次使用未修改的上游 wheel。构建保留 `soxr-1.0.0.dist-info/licenses/` 中的四个文件：`LICENSE.txt`、`COPYING.LGPL`、`LICENSE-libsoxr.txt`、`LICENSE-PFFFT.txt`。分发时保留完整版权、条件和免责声明，而非仅保留许可证名称。

PyInstaller 目录包将 SoXR 原生模块作为独立 `.pyd` 放在后端 `_internal/soxr/` 下；其 Python 包装代码通过模块导入调用它。应用不校验或锁定该扩展的内容。关闭 GreatSage 后，可用相同 Python ABI、Windows x64 架构和兼容接口构建的扩展替换；也可从 GreatSage 源码重新打包。实际文件名随 Python 版本变化，发布验收应核对该文件及许可证目录确实存在。

`.pyd` 由 Python 动态加载，并不表示内部 libsoxr 也是独立 DLL。[上游构建配置](https://github.com/dofuuz/python-soxr/blob/v1.0.0/CMakeLists.txt)默认把 libsoxr 编入扩展；对应 libsoxr 源码为 [a66f3eeeeb62a32403ff143b756eed92b1ec6b62](https://github.com/dofuuz/soxr/tree/a66f3eeeeb62a32403ff143b756eed92b1ec6b62)。重建时保留该子模块及上游构建文件，参见 [BUILDING.md](https://github.com/dofuuz/python-soxr/blob/v1.0.0/BUILDING.md)。发行版本变化时同步更新源码链接、元数据和替换说明。

上述说明提供组件识别、对应源码和替换入口；许可证条件仍以随包原文及 [LGPL 2.1 正文](https://opensource.org/license/lgpl-2-1)为准，不以应用采用 MIT 或扩展可替换代替具体分发要求。

## 离线包诊断

从源码运行 `python -m greatsage --check-voice-runtime`，或在后端目录运行 `greatsage-backend.exe --check-voice-runtime`。命令输出版本、合成静音管线与 SDK 懒加载检查的 JSON 和退出码；不打开音源、不读取用户设置、不创建运行数据库、不连接 Deepgram。成功只说明本机依赖可装载及管线可启动／清理，不代表真实识别、网络连接或音频设备已经验收。
