# GreatSage development

The user approved the v0.1 implementation plan on 2026-09-04. Continue implementation without repeating requirements approval. See docs/v0.1-plan.md and docs/requirements.md.

- Keep this repository independent of its parent workspace and other projects.
- Runtime recordings, transcripts, memories, logs, user Skills and credentials must stay outside version control. Development uses .runtime/.
- Never print API keys. Read existing Windows user environment variables when process environment lacks them.
- Maintain requirements, architecture decisions, validation results and roadmap with behavior changes.
- Record actual test results. Do not label untested latency or source capture as verified.
- v0.1 excludes timed reminders, executing Skill scripts and external computer-control tools.
- Use meaningful commits and push milestones to the user-authorized public GitHub repository.
