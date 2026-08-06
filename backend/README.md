# Sou2AI backend

Sou2AI is a local AI assistant planned for small businesses, with future support for English, Arabic, Lebanese Arabic, Franco-Arabic, and mixed-language conversations.

## Current milestone

This milestone provides only the FastAPI backend foundation: environment-based configuration, logging, CORS preparation, structured exception handling, and versioned API routing under `app/api/v1/`.

It does **not** include PostgreSQL, pgvector, database models, Ollama connections, Qwen, embeddings, document uploads, retrieval, RAG, memory, tools, agents, or a React frontend.

## Requirements

Use Python 3.14. The selected package versions declare Python 3.14 support.

## Setup on Windows PowerShell

From the `backend` directory:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Edit `.env` only for local configuration. Do not commit it. The default CORS origins target a future local React development server. In production, set `ALLOWED_CORS_ORIGINS` to explicit trusted origins; wildcard origins are rejected.

## Run the API

```powershell
uvicorn app.main:app --reload
```

Open Swagger UI at <http://127.0.0.1:8000/docs>.

Endpoints:

- Service metadata: <http://127.0.0.1:8000/>
- Health status: <http://127.0.0.1:8000/api/v1/health>

```json
{
  "status": "healthy",
  "service": "Sou2AI API",
  "version": "0.1.0",
  "environment": "development"
}
```

## Run tests

```powershell
python -m pytest
```

## Formatting and linting

```powershell
python -m ruff check app tests
python -m ruff format --check app tests
python -m ruff format app tests
```

## Next milestone

The next milestone can introduce database infrastructure deliberately, without implementing unrelated AI features.
