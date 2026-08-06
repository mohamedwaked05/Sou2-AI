# AGENTS.md  Sou2AI Development Rules

## Project

Sou2AI is a local-first AI assistant for small businesses.

Planned stack:

- FastAPI
- Python
- PostgreSQL
- pgvector
- Ollama
- Qwen2.5 7B
- BGE-M3
- React

Supported languages:

- English
- Arabic
- Lebanese Arabic
- Franco-Arabic
- Mixed Arabic and English
- Mixed Franco-Arabic and English

Development must be incremental.

Do not build future milestones before they are explicitly requested.

## Core Working Rule

Work only on the task requested in the current prompt.

Do not continue automatically into the next milestone.

Do not add features only because they may be useful later.

When the requested task is complete, stop and provide a concise summary.

## Session Startup and Token Usage

At the beginning of a new session:

1. Read this `AGENTS.md`.
2. Read the current user request.
3. Inspect only files directly relevant to the request.
4. Do not scan or summarize the entire repository unless necessary.
5. Do not repeat the complete project architecture.
6. Do not restate information already available in this file.
7. Do not produce a long introduction.
8. Begin with a concise statement of what will change.

Prefer targeted file inspection over broad repository exploration.

Do not read virtual environments, caches, generated files, uploaded runtime data, or dependency folders unless the task requires them.

Avoid unnecessary token usage while maintaining correctness.

## Step-by-Step Development

The user wants development performed one step at a time.

For every task:

1. Inspect the relevant files.
2. Identify the smallest required change.
3. Implement only that change.
4. Run the relevant validation or test.
5. Explain the result briefly.
6. Stop.

Do not provide several future steps at once.

Do not rush ahead.

Do not implement the next milestone without a new explicit request.

## Dependency Compatibility

Assume dependencies already selected for the project are compatible unless the user explicitly asks to verify them.

Do not browse documentation or package registries before every implementation.

Only verify compatibility when:

- introducing a new dependency
- upgrading a major version
- diagnosing an installation problem

Avoid unnecessary web lookups.

## Architecture Boundaries

Maintain these responsibilities:

- `app/api/`  HTTP routes and request handling
- `app/api/v1/`  version 1 API routes and router
- `app/core/`  configuration, logging, and shared exceptions
- `app/database/`  database infrastructure
- `app/rag/`  ingestion and retrieval pipeline
- `app/agent/`  agent orchestration
- `app/tools/`  controlled agent tools
- `app/memory/`  structured and semantic memory
- `app/services/`  application business logic
- `app/schemas/`  Pydantic request and response schemas
- `app/utils/`  small shared utilities

Do not place business logic directly inside API route files.

Do not place database queries directly inside route files.

Do not combine API, RAG, database, agent, and memory responsibilities in one module.

## Coding Style

Use production-ready but beginner-readable code.

Required practices:

- Use clear names.
- Use type hints.
- Keep functions focused.
- Keep modules focused on one responsibility.
- Prefer simple code over unnecessary abstraction.
- Avoid placeholder implementations that pretend to work.
- Avoid duplicate logic.
- Preserve existing naming and structure unless there is a strong reason to change them.
- Use Ruff for formatting and linting.
- Do not introduce unnecessary dependencies.

## AI Reliability Rules

When AI functionality is added later:

- Never fabricate prices.
- Never fabricate stock quantities.
- Never fabricate customers or orders.
- Never fabricate business policies.
- Never claim an action succeeded unless a tool confirms it.
- Retrieve business knowledge before answering.
- Clearly state when information is unavailable.
- Keep confirmed facts separate from recommendations.
- Require confirmation before destructive actions.

## RAG Rules

When the RAG milestone begins:

- Keep ingestion separate from retrieval.
- Keep embeddings separate from LLM generation.
- Store source metadata for every chunk.
- Return sources with answers.
- Use similarity thresholds.
- Do not answer from unrelated retrieved chunks.
- Do not use the LLM instead of structured database queries.
- Test English, Arabic, Lebanese Arabic, Franco-Arabic, and mixed-language queries.

Do not add LangChain or LlamaIndex unless explicitly requested.

## Local Model Rules

Planned local models:

- Chat model: `qwen2.5:7b`
- Embedding model: `bge-m3`
- Runtime: Ollama
- Ollama address: `http://localhost:11434`

Do not assume a model is installed or running.

Verify availability before using it.

Do not silently substitute another model.

Do not introduce paid APIs unless explicitly requested.

## Python Environment

Development is on Windows.

Do not change the current Python version unless explicitly requested.

Before adding a new dependency:

1. Confirm it is needed for the current milestone.
2. Add it to `pyproject.toml`.
3. Explain briefly why it is needed.
4. Do not install future milestone dependencies early.

Use Windows PowerShell commands in documentation.

Prefer:

```powershell
python -m pip
```

when interpreter selection matters.

## File Safety

Before modifying files:

* Inspect their contents.
* Preserve unrelated user work.
* Do not overwrite files blindly.
* Do not delete files without explaining why.
* Do not rename major directories without approval.
* Do not modify generated or dependency files manually.
* Do not create duplicate modules.

Never expose secrets from `.env`.

Never commit:

* `.env`
* Passwords
* API keys
* Database credentials
* Private customer data

Keep `.env.example` safe for source control.

## Testing

Run only tests relevant to the current change.

After making changes:

* Run targeted tests.
* Run Ruff checks where relevant.
* Report the command used.
* Report whether it passed.
* Report failures honestly.
* Do not claim success without validation.
* Do not repair unrelated failures unless requested.

## Commands and Git

Before running a command that:

* installs packages
* deletes files
* changes system configuration
* modifies the database
* or may be destructive

explain it briefly.

Safe inspection commands may be run directly.

Do not use force push.

Do not delete remote branches.

Do not overwrite unrelated remote history.

Do not commit secrets, virtual environments, caches, generated runtime data, or uploaded files.

## Communication Style

Keep responses concise and practical.

At the start of a task, state:

* What you understood.
* Which small area you will work on.

After completing a task, report:

* Files changed.
* What was implemented.
* Validation performed.
* Any unresolved issue.

Do not include long introductions, repeated project descriptions, or unrequested tutorials.

## Planning Rules

For a small task, inspect the relevant files and begin directly.

For a larger task, provide a short plan with no more than five items.

Ask a question only when missing information materially prevents correct implementation.

## Definition of Done

A task is complete when:

* The requested change is implemented.
* Relevant tests and checks were run.
* Existing unrelated behavior was preserved.
* No unrequested milestone was started.
* The result was summarized concisely.
* Codex stopped and waited for the next instruction.
