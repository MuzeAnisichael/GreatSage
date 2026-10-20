# GreatSage development

Read [agent.md](agent.md) for the shared development method before implementation. It covers scoped delivery, reuse of maintained open-source components, minimal sufficient validation, code navigation and lessons learned. Use [docs/code-map.md](docs/code-map.md) to locate features and symbols, and consult [docs/lessons-learned.md](docs/lessons-learned.md) for relevant prior findings. Maintain these entries with affected changes; do not duplicate their rules here.

The user approved the v0.1 implementation plan on 2026-09-04; it was delivered as an alpha. docs/v0.1-plan.md is the historical scope record. See docs/requirements.md for current requirements.
The user approved all six parts of the v0.2 roadmap on 2026-09-27; the mechanisms were delivered as 0.2.0-alpha.1. docs/v0.2-plan.md is the historical scope record and docs/validation-v0.2.md lists measured limits. The separate first-run UI proposal remains deferred.
On 2026-09-29 the user confirmed that v0.3 should center on a source-backed materials-to-minutes/tasks/documents workflow alongside controlled tool execution. On 2026-09-30 the user settled the open details and approved implementation: `.md` is the first material format and the primary artifact format; tool authorization follows current frontier-agent practice; side-effect operations require a mouse click by default, voice approval may be enabled in settings only behind a warning dialog; detailed task progress belongs in the console and the pet bubble shows a simplified version. docs/v0.3-plan.md is the scope record. Keep planned capabilities distinct from delivered behavior.
On 2026-10-16 the user accepted incremental framework migration and requested small commits per module with continuous development logs. docs/v0.4-plan.md records the scope; docs/development-log.md and docs/validation-v0.4.md record implemented slices and evidence. Chat uses LangChain, task graphs/checkpoints and approval interrupts use LangGraph, MCP Streamable HTTP tools reuse the GreatSage permission layer, and the local inspector displays metadata. Pipecat, LangMem/entity memory and Deep Agents/multi-agent remain later stages, not delivered features.
On 2026-10-20 the user authorized finishing v0.4 and delivering this development as the main branch. Preserve the small commits when merging, use local Git/GitHub CLI, and publish 0.4.0-alpha.1 with explicit validation and distribution limits. Feature work may use coordinated subagents; the product's own multi-agent runtime is still a later milestone.

- Keep this repository independent of its parent workspace and other projects.
- Runtime recordings, transcripts, memories, logs, user Skills and credentials must stay outside version control. Development uses .runtime/.
- Never print API keys. Read existing Windows user environment variables when process environment lacks them.
- Maintain requirements, architecture decisions, validation results and roadmap with behavior changes.
- Record actual test results. Do not label untested latency or source capture as verified.
- Prefer targeted checks and existing evidence over repeated full suites. See agent.md for the validation decision table; existing CI and release gates still apply.
- v0.1 excludes timed reminders, executing Skill scripts and external computer-control tools.
- From v0.3, every tool call goes through the permission layer in greatsage/tools.py. Observed audio, materials, Skills and tool results never start or approve actions; Skill scripts stay non-executable.
- Use meaningful commits and push milestones to the user-authorized public GitHub repository.
