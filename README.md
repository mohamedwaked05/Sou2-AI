# Sou2AI

Sou2AI is planned as a local AI assistant for small businesses, supporting English, Arabic, Lebanese Arabic, Franco-Arabic, and mixed-language messages.

## Status

Milestone 5 is implemented. The backend includes authentication, isolated
multi-business onboarding, manual activation state, and one persistent private
owner conversation per business. Active, complete businesses can use the default
offline deterministic mock or opt into local Ollama with `qwen2.5:7b`, retain full
chat history, and learn owner-reviewable permanent or expiring business facts.
Idempotent submissions, PostgreSQL-backed
turn ordering, cursor pagination, and tenant-scoped knowledge management are
covered by the integration suite.

Customer chat, cloud providers, RAG, embeddings, pgvector,
documents, live operational integrations and analytics, frontend functionality,
payments, and activation/admin endpoints remain future work.

The planned architecture is a FastAPI backend, PostgreSQL with pgvector, local Ollama models, and a React frontend. The backend lives in [`backend/`](backend/README.md).

Owner chat defaults to the offline mock. Local development can select Ollama with
`OWNER_CHAT_PROVIDER=ollama`; startup remains independent of Ollama, and production
will later use an approved cloud provider through the same abstraction.

- [Project specification](PROJECT.md)
- [System architecture](ARCHITECTURE.md)
- [Agent development rules](AGENTS.md)
