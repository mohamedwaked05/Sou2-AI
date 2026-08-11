# Sou2AI

Sou2AI is planned as a local AI assistant for small businesses, supporting English, Arabic, Lebanese Arabic, Franco-Arabic, and mixed-language messages.

## Status

Milestone 3 is implemented: the backend now includes secure user registration,
email verification through a replaceable Resend adapter, password login,
rotating refresh sessions, logout, account recovery, and current-user support.
Authentication does not create or authorize a business. AI, RAG, tool execution,
operational integrations, document ingestion, and frontend functionality remain
future work.

The planned architecture is a FastAPI backend, PostgreSQL with pgvector, local Ollama models, and a React frontend. The backend lives in [`backend/`](backend/README.md).

- [Project specification](PROJECT.md)
- [System architecture](ARCHITECTURE.md)
- [Agent development rules](AGENTS.md)
