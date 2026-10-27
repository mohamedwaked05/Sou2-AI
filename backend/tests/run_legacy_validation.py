"""Run existing backend suites on identity-checked, network-isolated test services."""

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NONCE = uuid.uuid4().hex[:12]
EVIDENCE = ROOT / ".tmp" / ("legacy-validation-" + NONCE)
PREFIX = "legacy-tests-" + NONCE
EVIDENCE.mkdir(parents=True, exist_ok=False)


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


image = json.loads(docker("inspect", "sou2ai-development-api"))[0]["Image"]
pg_image = docker(
    "image", "inspect", "pgvector/pgvector:0.8.0-pg17", "--format", "{{.Id}}"
)
bootstrap = EVIDENCE / "init-test-db.sh"
bootstrap.write_bytes(
    (ROOT / "docker/postgres/init-test-db.sh").read_bytes().replace(b"\r\n", b"\n")
)
created = []


def verify(identifier, expected_image, network):
    state = json.loads(docker("inspect", identifier))[0]
    assert state["Id"] == identifier and state["Image"] == expected_image
    assert state["Config"]["Labels"]["sou2ai.completion.fixture"] == NONCE
    assert (
        state["HostConfig"]["NetworkMode"] == network
        and not state["HostConfig"]["PortBindings"]
    )
    return state


try:
    pg = docker(
        "run",
        "-d",
        "--pull",
        "never",
        "--name",
        "sou2ai-completion-" + NONCE + "-pg",
        "--label",
        "sou2ai.completion.fixture=" + NONCE,
        "--network",
        "none",
        "--memory",
        "1g",
        "--tmpfs",
        "/var/lib/postgresql/data:rw,size=512m",
        "-e",
        "PGPORT=5433",
        "-e",
        "POSTGRES_USER=sou2ai",
        "-e",
        "POSTGRES_DB=sou2ai_test",
        "-e",
        "POSTGRES_TEST_DB=sou2ai_test",
        "-e",
        "POSTGRES_HOST_AUTH_METHOD=trust",
        "--mount",
        f"type=bind,source={bootstrap},target=/docker-entrypoint-initdb.d/10-init-test-db.sh,readonly",
        pg_image,
        "postgres",
        "-p",
        "5433",
    )
    created.append((pg, pg_image, "none"))
    verify(*created[-1])
    for _ in range(80):
        probe = subprocess.run(
            [
                "docker",
                "exec",
                pg,
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                "sou2ai",
                "-d",
                "sou2ai_test",
            ],
            capture_output=True,
        )
        if probe.returncode == 0:
            roles = subprocess.run(
                [
                    "docker",
                    "exec",
                    pg,
                    "psql",
                    "-U",
                    "sou2ai",
                    "-d",
                    "sou2ai_test",
                    "-Atc",
                    "SELECT count(*) FROM pg_roles WHERE rolname IN "
                    "('sou2ai_runtime_login','sou2ai_lifecycle_operator_login')",
                ],
                capture_output=True,
            )
            if roles.returncode == 0 and roles.stdout.strip() == b"2":
                break
        time.sleep(0.25)
    else:
        raise RuntimeError("Disposable PostgreSQL initialization failed")
    identity = docker(
        "exec",
        pg,
        "psql",
        "-U",
        "sou2ai",
        "-d",
        "sou2ai_test",
        "-Atc",
        "SELECT current_database(),current_user,"
        "(SELECT count(*) FROM pg_database WHERE datname='sou2ai_dev'),"
        "(SELECT count(*) FROM pg_tables WHERE schemaname='public')",
    )
    assert identity == "sou2ai_test|sou2ai|0|0"
    verify(pg, pg_image, "none")
    docker("exec", pg, "createdb", "-U", "sou2ai", "fake_store")
    seeded = subprocess.run(
        ["docker", "exec", "-i", "-e", "POSTGRES_DB=fake_store", pg, "sh"],
        input=(ROOT / "docker/fake-store/init-fake-store.sh")
        .read_bytes()
        .replace(b"\r\n", b"\n"),
        capture_output=True,
        timeout=40,
    )
    assert seeded.returncode == 0, "Disposable fake-store initialization failed"
    redis_image = docker("image", "inspect", "redis:7-alpine", "--format", "{{.Id}}")
    redis = docker(
        "run",
        "-d",
        "--pull",
        "never",
        "--name",
        "sou2ai-completion-" + NONCE + "-redis",
        "--label",
        "sou2ai.completion.fixture=" + NONCE,
        "--network",
        "container:" + pg,
        "--read-only",
        "--tmpfs",
        "/data:rw,size=64m",
        redis_image,
        "redis-server",
        "--bind",
        "127.0.0.1",
        "--appendonly",
        "no",
        "--save",
        "",
    )
    created.append((redis, redis_image, "container:" + pg))
    verify(*created[-1])
    for _ in range(40):
        ping = subprocess.run(
            ["docker", "exec", redis, "redis-cli", "PING"], capture_output=True
        )
        if ping.returncode == 0 and ping.stdout.strip() == b"PONG":
            break
        time.sleep(0.25)
    else:
        raise RuntimeError("Disposable Redis readiness failed")
    args = [
        "create",
        "--pull",
        "never",
        "--name",
        "sou2ai-completion-" + NONCE + "-runner",
        "--label",
        "sou2ai.completion.fixture=" + NONCE,
        "--network",
        "container:" + pg,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=512m",
        "--tmpfs",
        "/validation/experiments/owner_chat_language_eval/artifacts:rw,size=64m",
        "--memory",
        "2g",
        "--workdir",
        "/validation/backend",
    ]
    for item in (
        "backend/app",
        "backend/tests",
        "backend/alembic",
        "backend/alembic.ini",
        "backend/pyproject.toml",
        "backend/evaluations",
        "compose.yaml",
        "frontend/vite.config.ts",
        "docker",
    ):
        args += [
            "--mount",
            f"type=bind,source={ROOT / item},target=/validation/{item},readonly",
        ]
    for item in subprocess.check_output(
        ["git", "ls-files", "experiments"], text=True
    ).splitlines():
        if item.endswith((".py", ".json", ".jsonl", ".md")):
            args += [
                "--mount",
                f"type=bind,source={ROOT / item},target=/validation/{item},readonly",
            ]
    args += ["--mount", f"type=bind,source={EVIDENCE},target=/evidence"]
    env = {
        "ENVIRONMENT": "testing",
        "ADMISSION_MODE": "open",
        "OWNER_CHAT_PROVIDER": "mock",
        "SOU2AI_DISPOSABLE_RUN": NONCE,
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "SOURCE_MAPPING_PROVIDER": "mock",
        "CONVERSATION_SUMMARY_PROVIDER": "mock",
        "CUSTOMER_CHAT_PROVIDER": "mock",
        "GEMINI_API_KEY": "synthetic-no-network-test-key",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TRUSTED_HOSTS": "testserver,localhost,127.0.0.1",
        "PYTHONPATH": "/validation/backend:/validation/backend/tests/offline",
        "TEST_POSTGRESQL_DATABASE_URL": "postgresql+psycopg://sou2ai_runtime_login@127.0.0.1:5432/sou2ai_test",
        "TEST_MIGRATION_POSTGRESQL_DATABASE_URL": "postgresql+psycopg://sou2ai@127.0.0.1:5432/sou2ai_test",
        "TEST_LIFECYCLE_OPERATOR_POSTGRESQL_DATABASE_URL": "postgresql+psycopg://sou2ai_lifecycle_operator_login@127.0.0.1:5432/sou2ai_test",
        "POSTGRESQL_DATABASE_URL": "postgresql+psycopg://sou2ai_runtime_login@127.0.0.1:5432/sou2ai_test",
        "FAKE_STORE_DATABASE_URL": "postgresql+psycopg://sou2ai_store_reader@127.0.0.1:5432/fake_store",
    }
    env = {key: value.replace(":5432/", ":5433/") for key, value in env.items()}
    for key, value in env.items():
        args += ["-e", key + "=" + value]
    targets = sys.argv[1:] or [
        str(p.relative_to(ROOT / "backend")).replace("\\", "/")
        for p in sorted((ROOT / "backend/tests").glob("test_*.py"))
    ]
    runner = docker(
        *args,
        image,
        "python",
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "--tb=short",
        "--junitxml=/evidence/" + PREFIX + ".xml",
        *targets,
    )
    created.append((runner, image, "container:" + pg))
    verify(*created[-1])
    with (ROOT / ".tmp" / (PREFIX + ".log")).open("wb") as log:
        subprocess.run(
            ["docker", "start", "-a", runner],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    state = verify(*created[-1])
    print(
        json.dumps(
            {
                "exit_code": state["State"]["ExitCode"],
                "isolated_test_database": True,
                "report": str(EVIDENCE / (PREFIX + ".xml")),
                "log": str(ROOT / ".tmp" / (PREFIX + ".log")),
            }
        )
    )
    result = state["State"]["ExitCode"]
finally:
    for resource in reversed(created):
        verify(*resource)
        docker("rm", "--force", resource[0])
    assert not docker(
        "ps", "-aq", "--filter", "label=sou2ai.completion.fixture=" + NONCE
    )
raise SystemExit(result)
