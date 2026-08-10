# Project name

Sou2AI

## Vision

Sou2AI is a local-first AI operations assistant for small businesses.

It begins as a RAG-powered business knowledge assistant and will later grow into an agent capable of interacting with structured business data and safely performing business operations.

## Problem

Small businesses often manage product details, prices, policies, orders, customer information, and communications across disconnected documents and messaging applications.

Sou2AI aims to provide one natural-language interface for finding information and, in later phases, taking controlled actions.

## Initial target users

* Small shop owners
* Instagram and WhatsApp sellers
* Home businesses
* Retail stores
* Small service businesses
* Lebanese and Arabic-speaking business owners

## Supported languages

* English
* Arabic
* Lebanese Arabic
* Franco-Arabic or Arabizi
* Mixed Arabic and English
* Mixed Franco-Arabic and English

## Local-first principle

During development, the application runs locally without paid AI APIs.

Planned local AI stack:

* Ollama
* Qwen2.5 7B
* BGE-M3

## Technical stack

Backend:

* Python
* FastAPI
* Pydantic
* PostgreSQL
* pgvector

AI:

* Ollama
* Qwen2.5 7B
* BGE-M3
* Custom RAG pipeline
* Agent tools in later milestones

Frontend:

* React
* Vite

Testing and quality:

* pytest
* Ruff

## Current status

Milestone 2, the PostgreSQL platform foundation, is complete. The repository now
contains Docker-based development and test databases, SQLAlchemy sessions,
Alembic migrations, foundational platform models, weekly schedules, guarded
business activation, a minimal tool-call audit table, and database/API tests.

The current repository does not yet contain:

* pgvector integration
* Ollama connectivity
* Embeddings
* RAG
* Agent tools
* Memory
* React functionality
* WhatsApp integration

The next milestone is Milestone 3: local Ollama connectivity.

## Development roadmap

### Milestone 1: Backend foundation — Complete

* FastAPI application structure
* Versioned API routing
* Configuration foundation
* Health endpoint
* Testing and formatting foundation

### Milestone 2: PostgreSQL platform foundation — Complete

* Docker Compose creates separate `sou2ai_dev` and `sou2ai_test` databases.
* SQLAlchemy and Psycopg provide scoped synchronous database sessions.
* Alembic owns native enums, foundational platform tables, constraints, indexes,
  and database triggers.
* Users, business profiles, single-owner MVP memberships, weekly opening hours,
  and privacy-minimal tool-call audit metadata are represented.
* Profile completion is derived from current data; activation is guarded in
  PostgreSQL so direct SQL cannot activate an incomplete business.
* Database health, schedule normalization, retention, migration, model, and API
  behavior are covered by automated tests using `sou2ai_test`.

This milestone does not add authentication or business-creation endpoints,
pgvector, RAG, AI tools/adapters, operational business data, inventory, billing,
or a platform-admin dashboard.

### Milestone 3: Local Ollama connectivity

* Connect the FastAPI backend to the local Ollama HTTP API
* Verify that Qwen2.5 7B is installed and reachable
* Add a minimal model status check
* Add a simple generation endpoint
* Mock Ollama in automated tests

This milestone does not add RAG, memory, agent tools, or business-answering logic.

### Milestone 4: Arabic and Franco-Arabic model evaluation

* Define a small repeatable evaluation dataset
* Test English, Arabic, Lebanese Arabic, Franco-Arabic, and mixed-language prompts
* Record response quality and limitations
* Decide whether Qwen2.5 7B remains the default local model

### Milestone 5: pgvector and knowledge storage

* Enable the pgvector PostgreSQL extension
* Add document and chunk database models
* Create migrations for knowledge storage
* Keep structured business facts separate from vectorized knowledge

### Milestone 6: Document ingestion and chunking

* Accept supported documents
* Extract and normalize text
* Split text into traceable chunks
* Store document and chunk metadata
* Preserve source information for citations

### Milestone 7: Embeddings and vector retrieval

* Connect BGE-M3 locally
* Generate and store embeddings
* Implement similarity search
* Test retrieval independently from the language model

### Milestone 8: Complete RAG question-answering flow

* Retrieve relevant chunks before generating an answer
* Build prompts from retrieved context
* Return answers with sources
* Refuse or ask for clarification when the available knowledge is insufficient
* Evaluate retrieval and answer quality

### Milestone 9: React interface

* Add chat functionality
* Add document upload
* Display answer sources
* Show clear loading and error states

### Milestone 10: Controlled operational integrations

* Connect to each business's source system through a controlled API, read-only
  database integration, or Sou2AI-managed operational system.
* Add scoped read operations without copying live products, inventory, orders,
  customers, appointments, or sales into the platform database unnecessarily.
* Keep operational integrations separate from unstructured RAG knowledge.

### Milestone 11: Agent tool calling

* Introduce a controlled agent loop
* Expose approved business operations as explicit tools
* Validate tool inputs and outputs
* Require confirmation for destructive or sensitive actions

### Milestone 12: Memory and conversation history

* Store conversation history
* Define short-term and long-term memory boundaries
* Prevent memory from overriding trusted business data

### Milestone 13: External messaging integrations

* Add integrations such as WhatsApp only after the core assistant is reliable
* Apply authentication, authorization, and rate limiting
* Preserve source and action traceability

### Milestone 14: Production and cloud deployment options

* Define deployment configurations
* Protect secrets and business data
* Add monitoring, backups, and recovery procedures
* Keep local development supported

## Milestone rules

* Complete and verify one milestone before starting the next.
* Keep each milestone limited to its stated scope.
* Update this file when milestone order or scope changes.
* Do not treat planned functionality as implemented.
* New ideas are allowed, but they must be discussed and placed in the appropriate milestone before implementation.

## Core reliability principles

* Never fabricate business information.
* Retrieve business knowledge before answering.
* Use structured database tools for changing business facts.
* Provide sources for RAG answers.
* Ask for missing information.
* Confirm destructive actions.
* Keep the LLM replaceable.
* Develop one milestone at a time.
