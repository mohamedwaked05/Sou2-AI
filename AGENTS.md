# AGENTS.md  Sou2AI Development Rules

## Core Working Rule

Work only on the task requested in the current prompt. Do not continue into a
future milestone or add features merely because they may be useful later.

When the requested task is complete, stop and provide a concise summary.

## Project References

Read `PROJECT.md` for project goals, users, roadmap, and technical stack.

Read `ARCHITECTURE.md` only when the current task affects system architecture,
RAG, models, tools, memory, database design, language handling, or deployment.

Do not reread or summarize those files when they are unrelated to the current
task.

Project documents describe planned work, not authorization to implement it.
Follow the current user request when defining scope.

## Session Startup and Token Usage

At the beginning of a session:

1. Read this file and the current user request.
2. Inspect only files directly relevant to that request.
3. State concisely what will change.

Do not scan or summarize the entire repository unless necessary. Do not read
virtual environments, caches, generated files, uploaded runtime data, or
dependency folders unless the task requires them. Avoid repeated architecture
explanations and unnecessary context use.

Use targeted searches before broad listings. Read configuration, tests, and
documentation only when they affect the requested change. Do not repeat facts
already available in the user request or these rules.

## Step-by-Step Development

For each task:

1. Inspect relevant files.
2. Identify the smallest required change.
3. Implement only that change.
4. Run relevant validation or tests.
5. Report the result briefly and stop.

Do not rush ahead or implement the next milestone without a new explicit
request.

For a small task, begin after the targeted inspection. For a larger task, use a
short plan of no more than five items. Ask a question only when missing
information materially prevents correct work.

## Dependency Compatibility

Assume selected dependencies are compatible unless the user asks for
verification. Do not browse package registries before every implementation.

Verify compatibility only when introducing a dependency, upgrading a major
version, or diagnosing an installation problem. Do not add future milestone
dependencies early.

Do not change the current Python version unless explicitly requested. Use
Windows PowerShell commands in documentation and prefer `python -m pip` when
interpreter selection matters.

Before adding a dependency, confirm it is required for the current milestone,
add it to `pyproject.toml`, and explain its purpose briefly. Do not change
versions unless an installation error clearly requires it.

## Architecture Boundaries

- `app/api/` handles HTTP routes; `app/api/v1/` contains version 1 APIs.
- `app/core/` holds configuration, logging, and shared exceptions.
- `app/database/` manages persistence infrastructure.
- `app/rag/` manages ingestion and retrieval.
- `app/agent/` decides which capabilities to use.
- `app/tools/` exposes controlled operations.
- `app/memory/` stores and retrieves conversation or semantic memory.
- `app/services/` contains application logic.
- `app/schemas/` contains Pydantic request and response schemas.
- `app/utils/` contains small shared utilities.

Keep routes focused on HTTP concerns. Do not combine API, database, RAG, agent,
or memory responsibilities in one module, and do not put database queries in
routes.

Do not create fake implementations of future capabilities. When those
capabilities are introduced, preserve clear separation between ingestion,
retrieval, persistence, tools, and response generation.

Keep configuration and shared infrastructure reusable, but do not add empty
business abstractions solely for future convenience.

## Coding Style

Use production-ready, beginner-readable code with clear names, type hints, and
focused modules and functions. Prefer simple code over unnecessary abstractions,
duplicate logic, or fake placeholder implementations.

Preserve existing naming and structure unless there is a strong reason to
change it. Use Ruff for formatting and linting. Do not introduce unnecessary
dependencies.

Add comments only when the reason is not obvious. Avoid generic base classes,
repositories, and helpers unless they solve a current requirement.

## File Safety

Inspect files before modifying them. Preserve unrelated user work; do not
overwrite files blindly, delete files without explanation, rename major
directories without approval, or manually modify generated/dependency files.

Never expose secrets from `.env`. Never commit credentials, private customer
data, virtual environments, caches, generated runtime data, or uploaded files.
Keep `.env.example` safe for source control.

Resolve exact paths before destructive actions. Prefer recoverable operations
when practical and report material deletions. Do not access or disclose the
contents of secret files.

Do not modify unrelated files to make a requested change appear cleaner.

## Preserve Existing Architecture

Before creating a new file, class, function, or dependency:

1. Inspect whether an appropriate implementation already exists.
2. Extend existing code when reasonable.
3. Do not create duplicate modules or parallel implementations.
4. Prefer improving existing architecture over introducing new abstractions.
5. If a refactor would affect multiple modules, explain it first instead of performing it automatically.

## Testing

Run only tests relevant to the current change, plus Ruff checks where relevant.
Report commands and results honestly. Do not claim success without validation
or repair unrelated failures unless requested.

Run the smallest useful test first. If validation cannot run because of an
environment or dependency failure, report the exact failure and do not pretend
the feature is verified.

## Commands and Git

Before running installation, deletion, system-configuration, database, or other
destructive commands, explain the action briefly. Safe inspection commands may
run directly.

Do not force-push, delete remote branches, overwrite unrelated remote history,
or commit secrets. Do not initialize, commit, or push Git changes unless the
user explicitly requests it.

Inspect status and staged changes before committing. Fetch and compare remote
history before pushing when a remote may already contain commits. Stop rather
than resolving unrelated or conflicting remote history destructively.

## Communication Style

Keep responses concise and practical. State what you understood and the small
area being changed. On completion, report files changed, implementation,
validation, and unresolved issues without long introductions or unrequested
tutorials.

Use links to relevant local files when handing off code changes. Lead with the
outcome rather than a long description of the process.

## Definition of Done

A task is complete when the requested change is implemented, relevant checks
have run, unrelated behavior is preserved, no unrequested milestone started,
and the result is summarized concisely before stopping.
