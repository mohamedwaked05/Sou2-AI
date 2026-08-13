#!/bin/sh
set -eu

test_database="${POSTGRES_TEST_DB:-sou2ai_test}"
runtime_user="${POSTGRES_RUNTIME_USER:-sou2ai_runtime_login}"
runtime_password="${POSTGRES_RUNTIME_PASSWORD:-sou2ai_runtime_local}"
operator_user="${POSTGRES_LIFECYCLE_OPERATOR_USER:-sou2ai_lifecycle_operator_login}"
operator_password="${POSTGRES_LIFECYCLE_OPERATOR_PASSWORD:-sou2ai_lifecycle_operator_local}"

psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=test_database="$test_database" \
  --set=bootstrap_user="$POSTGRES_USER" \
  --set=runtime_user="$runtime_user" \
  --set=runtime_password="$runtime_password" \
  --set=operator_user="$operator_user" \
  --set=operator_password="$operator_password" <<'SQL'
SELECT format('CREATE DATABASE %I', :'test_database')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'test_database')\gexec

SELECT 'CREATE ROLE sou2ai_migrator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sou2ai_migrator')\gexec
SELECT 'CREATE ROLE sou2ai_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sou2ai_runtime')\gexec
SELECT 'CREATE ROLE sou2ai_lifecycle_operator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sou2ai_lifecycle_operator')\gexec
ALTER ROLE sou2ai_migrator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE sou2ai_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE sou2ai_lifecycle_operator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;

SELECT format(
    'CREATE ROLE %I LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
    :'runtime_user', :'runtime_password'
)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'runtime_user')\gexec
SELECT format(
    'ALTER ROLE %I LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
    :'runtime_user', :'runtime_password'
)\gexec
SELECT format(
    'CREATE ROLE %I LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
    :'operator_user', :'operator_password'
)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'operator_user')\gexec
SELECT format(
    'ALTER ROLE %I LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
    :'operator_user', :'operator_password'
)\gexec

SELECT format('GRANT sou2ai_migrator TO %I WITH ADMIN OPTION', :'bootstrap_user')\gexec
SELECT format('GRANT sou2ai_runtime TO %I', :'runtime_user')\gexec
SELECT format('GRANT sou2ai_lifecycle_operator TO %I', :'operator_user')\gexec
SQL

for database in "$POSTGRES_DB" "$test_database"; do
  psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$database" <<'SQL'
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO sou2ai_migrator;
GRANT USAGE ON SCHEMA public TO sou2ai_runtime, sou2ai_lifecycle_operator;
SQL
done
