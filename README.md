# Sou2AI

Sou2AI is planned as a local AI assistant for small businesses, supporting English, Arabic, Lebanese Arabic, Franco-Arabic, and mixed-language messages.

## Status

Milestone 5 is implemented. The backend includes authentication, isolated
multi-business onboarding, manual activation state, and one persistent private
owner conversation per business. Active, complete businesses can use the offline
deterministic mock provider, retain full chat history, and learn owner-reviewable
permanent or expiring business facts. Idempotent submissions, PostgreSQL-backed
turn ordering, cursor pagination, and tenant-scoped knowledge management are
covered by the integration suite.

Customer chat, cloud providers, Ollama connectivity, RAG, embeddings, pgvector,
documents, live operational integrations and analytics, frontend functionality,
payments, and activation/admin endpoints remain future work.

The planned architecture is a FastAPI backend, PostgreSQL with pgvector, local Ollama models, and a React frontend. The backend lives in [`backend/`](backend/README.md).

- [Project specification](PROJECT.md)
- [System architecture](ARCHITECTURE.md)
- [Agent development rules](AGENTS.md)
