"""Run candidate validation on fresh, identity-checked network-none resources."""

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def main():
    nonce = uuid.uuid4().hex[:12]
    database = f"sou2ai_disposable_{nonce}"
    image = docker(
        "image",
        "inspect",
        "sou2ai-local-backend:catalogue-20261009",
        "--format",
        "{{.Id}}",
    )
    pg_image = docker(
        "image", "inspect", "pgvector/pgvector:0.8.0-pg17", "--format", "{{.Id}}"
    )
    created = []

    def verify(identifier, expected_image, network):
        state = json.loads(docker("inspect", identifier))[0]
        assert state["Id"] == identifier and state["Image"] == expected_image
        assert state["Config"]["Labels"]["sou2ai.quality.fixture"] == nonce
        assert state["HostConfig"]["NetworkMode"] == network
        assert not state["HostConfig"]["PortBindings"]
        for mount in state["Mounts"]:
            assert mount["Type"] == "bind" and not mount["RW"]
            assert Path(mount["Source"]).resolve().is_relative_to(ROOT)
        return state

    try:
        target = docker(
            "run",
            "-d",
            "--pull",
            "never",
            "--name",
            f"sou2ai-quality-{nonce}-pg",
            "--label",
            f"sou2ai.quality.fixture={nonce}",
            "--network",
            "none",
            "--memory",
            "700m",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,size=256m",
            "-e",
            "POSTGRES_USER=reservation_test_admin",
            "-e",
            f"POSTGRES_DB={database}",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            pg_image,
        )
        created.append((target, pg_image, "none"))
        verify(*created[-1])
        for _ in range(40):
            probe = subprocess.run(
                [
                    "docker",
                    "exec",
                    target,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "reservation_test_admin",
                    "-d",
                    database,
                ],
                capture_output=True,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.3)
        else:
            raise RuntimeError("Disposable PostgreSQL startup failed")
        assert (
            docker(
                "exec",
                target,
                "psql",
                "-h",
                "127.0.0.1",
                "-U",
                "reservation_test_admin",
                "-d",
                database,
                "-Atc",
                (
                    "SELECT current_database(),current_user,(SELECT count(*) "
                    "FROM pg_database WHERE datname IN "
                    "('sou2ai_dev','sou2ai_test'))"
                ),
            )
            == f"{database}|reservation_test_admin|0"
        )
        args = [
            "create",
            "--pull",
            "never",
            "--name",
            f"sou2ai-quality-{nonce}-runner",
            "--label",
            f"sou2ai.quality.fixture={nonce}",
            "--network",
            f"container:{target}",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=256m",
            "--memory",
            "1000m",
            "--workdir",
            "/validation",
        ]
        for path in ("app", "tests", "alembic", "pyproject.toml"):
            args += [
                "--mount",
                f"type=bind,source={ROOT / path},target=/validation/{path},readonly",
            ]
        for key, value in {
            "SOU2AI_DISPOSABLE_RUN": nonce,
            "ENVIRONMENT": "testing",
            "ADMISSION_MODE": "open",
            "OWNER_CHAT_PROVIDER": "mock",
            "SOURCE_MAPPING_PROVIDER": "mock",
            "CONVERSATION_SUMMARY_PROVIDER": "mock",
            "CUSTOMER_CHAT_PROVIDER": "mock",
            "TRUSTED_HOSTS": "testserver,localhost,127.0.0.1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": "/validation:/validation/tests/offline",
            "TOOL_CALL_AUDIT_HMAC_SECRET": "synthetic-quality-test-audit-key-123456789",
            "POSTGRESQL_DATABASE_URL": f"postgresql+psycopg://sou2ai_runtime_login@127.0.0.1:5432/{database}",
        }.items():
            args += ["-e", f"{key}={value}"]

        def invoke(targets, stage):
            invocation = list(args)
            invocation[invocation.index("--name") + 1] += "-" + stage
            runner = docker(
                *invocation,
                image,
                "python",
                "-B",
                "-m",
                "pytest",
                "-c",
                "/dev/null",
                "-p",
                "no:cacheprovider",
                "--confcutdir=tests/quality_isolated",
                *targets,
                "-q",
                "--tb=short",
            )
            created.append((runner, image, f"container:{target}"))
            verify(*created[-1])
            subprocess.run(["docker", "start", "-a", runner], check=False)
            return int(verify(*created[-1])["State"]["ExitCode"])

        if sys.argv[1:] == ["--clone"]:
            if invoke(
                ["tests/quality_isolated/test_quality_clone.py::test_seed_revision18"],
                "seed",
            ):
                return 1
            verify(target, pg_image, "none")
            docker(
                "exec",
                target,
                "pg_dump",
                "-h",
                "127.0.0.1",
                "-U",
                "reservation_test_admin",
                "-d",
                database,
                "-Fc",
                "-f",
                "/tmp/quality-revision18.dump",
            )
            restored = f"sou2ai_restored_{nonce}"
            docker(
                "exec",
                target,
                "createdb",
                "-h",
                "127.0.0.1",
                "-U",
                "reservation_test_admin",
                "-T",
                "template0",
                restored,
            )
            docker(
                "exec",
                target,
                "pg_restore",
                "-h",
                "127.0.0.1",
                "-U",
                "reservation_test_admin",
                "-d",
                restored,
                "--exit-on-error",
                "/tmp/quality-revision18.dump",
            )
            return invoke(
                [
                    "tests/quality_isolated/test_quality_clone.py::test_clone_upgrade_preserves_all_existing_rows_and_security"
                ],
                "clone",
            )
        return invoke(
            sys.argv[1:] or ["tests/quality_isolated/test_quality_native.py"], "tests"
        )
    finally:
        for resource in reversed(created):
            verify(*resource)
            docker("rm", "--force", resource[0])
        assert not docker(
            "ps", "-aq", "--filter", f"label=sou2ai.quality.fixture={nonce}"
        )
        print("Verified disposable cleanup. Live resources were not used.")


if __name__ == "__main__":
    raise SystemExit(main())
