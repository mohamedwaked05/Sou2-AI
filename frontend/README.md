# Sou2AI frontend

Vite + React + TypeScript business interface. Set `VITE_API_BASE_URL` when the API is not at `http://localhost:8000/api/v1`.

The tenant-scoped Data Sources page lists the deployment-approved PostgreSQL demo
profile and supports safe configuration, validation, activation, health checks,
and disabling. It never asks for or displays a database URL, host, port, password,
environment value, schema, or SQL. The page reports only real connection,
mapping, health, and capability metadata returned by the backend; live store
records stay in the external source. It does not expose operational agent tools.

Run `npm install`, then `npm run dev`. Checks: `npm run format:check`, `npm run lint`, `npm run typecheck`, `npm test`, and `npm run build`.

Place the user-provided final transparent royal-blue logo at `public/sou2ai-logo.png`; no replacement logo is included here.
