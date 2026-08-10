#!/bin/sh
set -eu

test_database="${POSTGRES_TEST_DB:-sou2ai_test}"

psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=test_database="$test_database" <<'SQL'
SELECT format('CREATE DATABASE %I', :'test_database')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'test_database')\gexec
SQL
