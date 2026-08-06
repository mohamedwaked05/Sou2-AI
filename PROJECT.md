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

The current repository contains the initial FastAPI backend foundation.

It does not yet contain:

* Database connectivity
* pgvector integration
* Ollama connectivity
* Embeddings
* RAG
* Agent tools
* Memory
* React functionality
* WhatsApp integration

## Development roadmap

1. Backend foundation
2. Local Ollama connectivity
3. Arabic and Franco-Arabic model evaluation
4. PostgreSQL and pgvector setup
5. Document ingestion
6. Chunking and embeddings
7. Vector retrieval
8. Complete RAG question-answering flow
9. React chat and document upload interface
10. Structured business data
11. Agent tool calling
12. Memory and conversation history
13. External messaging integrations
14. Production and cloud deployment options

## Core reliability principles

* Never fabricate business information.
* Retrieve business knowledge before answering.
* Use structured database tools for changing business facts.
* Provide sources for RAG answers.
* Ask for missing information.
* Confirm destructive actions.
* Keep the LLM replaceable.
* Develop one milestone at a time.
