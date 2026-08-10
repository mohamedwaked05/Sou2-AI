# Sou2AI

Sou2AI is planned as a local AI assistant for small businesses, supporting English, Arabic, Lebanese Arabic, Franco-Arabic, and mixed-language messages.

## Status

Milestone 2 is complete: the backend has its FastAPI foundation, Dockerized
PostgreSQL development/test databases, SQLAlchemy models and sessions, Alembic
migrations, business profile/schedule safeguards, minimal tool-call audit
infrastructure, and database integration tests. AI, RAG, tool execution,
operational integrations, document ingestion, and frontend functionality remain
future work.

The planned architecture is a FastAPI backend, PostgreSQL with pgvector, local Ollama models, and a React frontend. The backend lives in [`backend/`](backend/README.md).

- [Project specification](PROJECT.md)
- [System architecture](ARCHITECTURE.md)
- [Agent development rules](AGENTS.md)
