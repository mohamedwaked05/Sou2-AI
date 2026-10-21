"""No host ports, external networks, deployment secrets or existing resources."""

import json
import subprocess
import time
import uuid
from pathlib import Path


def command(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def main(test_path="tests/operational_isolated/test_sqlserver_operations.py"):
    root = Path(__file__).resolve().parents[3]
    nonce = uuid.uuid4().hex
    sql_image = command(
        "image",
        "inspect",
        "mcr.microsoft.com/mssql/server:2025-latest",
        "--format",
        "{{.Id}}",
    )
    runner_image = command(
        "image",
        "inspect",
        "sou2ai-local-backend:catalogue-20261009",
        "--format",
        "{{.Id}}",
    )
    ids = []
    expected = {}

    def verify(identifier):
        info = json.loads(command("inspect", identifier))[0]
        kind, image, network = expected[identifier]
        assert (
            info["Id"] == identifier
            and info["Name"] == f"/sou2ai-operations-{nonce}-{kind}"
        )
        assert (
            info["Image"] == image
            and info["Config"]["Labels"]["sou2ai.operations.fixture"] == nonce
        )
        assert (
            info["HostConfig"]["NetworkMode"] == network
            and not info["HostConfig"]["PortBindings"]
        )
        for mount in info["Mounts"]:
            assert (
                mount["Type"] == "bind"
                and not mount["RW"]
                and Path(mount["Source"]).resolve().is_relative_to(root)
            )
        return info

    try:
        sql = command(
            "create",
            "--pull",
            "never",
            "-i",
            "--name",
            f"sou2ai-operations-{nonce}-sql",
            "--label",
            f"sou2ai.operations.fixture={nonce}",
            "--network",
            "none",
            "--memory",
            "2300m",
            "--cpus",
            "2",
            "--tmpfs",
            "/var/opt/mssql:rw,size=1g,uid=10001,gid=0",
            "-e",
            "ACCEPT_EULA=Y",
            "-e",
            "MSSQL_PID=Developer",
            "-e",
            "MSSQL_MEMORY_LIMIT_MB=1536",
            "--entrypoint",
            "bash",
            sql_image,
            "-c",
            (
                "read -r MSSQL_SA_PASSWORD; export MSSQL_SA_PASSWORD; exec "
                "/opt/mssql/bin/sqlservr"
            ),
        )
        ids.append(sql)
        expected[sql] = ("sql", sql_image, "none")
        verify(sql)
        process = subprocess.Popen(
            ["docker", "start", "-ai", sql],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process.stdin.write(b"SyntheticOnly-NotASecret-49!\n")
        process.stdin.flush()
        ready = False
        for _ in range(60):
            probe = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    sql,
                    "bash",
                    "-c",
                    (
                        "read -r SQLCMDPASSWORD; export SQLCMDPASSWORD; exec "
                        "/opt/mssql-tools18/bin/sqlcmd -S tcp:127.0.0.1,1433 -U sa -C"
                        " -l 2 -t 2 -Q 'SET NOCOUNT ON; SELECT 1'"
                    ),
                ],
                input=b"SyntheticOnly-NotASecret-49!\n",
                capture_output=True,
                timeout=6,
            )
            if probe.returncode == 0:
                ready = True
                break
            time.sleep(0.5)
        assert ready, "Disposable SQL Server did not become ready"
        verify(sql)
        args = [
            "create",
            "--pull",
            "never",
            "--name",
            f"sou2ai-operations-{nonce}-runner",
            "--label",
            f"sou2ai.operations.fixture={nonce}",
            "--network",
            f"container:{sql}",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=128m",
            "--memory",
            "600m",
            "--workdir",
            "/validation",
            "-e",
            f"SOU2AI_SQLSERVER_FIXTURE={nonce}",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        ]
        for relative in ("app", "tests", "alembic"):
            args += [
                "--mount",
                f"type=bind,source={root / ('backend') / relative},target=/validation/{
                    relative
                },readonly",
            ]
        runner = command(
            *args,
            runner_image,
            "python",
            "-B",
            "-m",
            "pytest",
            "-c",
            "/dev/null",
            "-p",
            "no:cacheprovider",
            "--confcutdir=tests/operational_isolated",
            test_path,
            "-q",
            "--tb=short",
        )
        ids.append(runner)
        expected[runner] = ("runner", runner_image, f"container:{sql}")
        verify(runner)
        subprocess.run(["docker", "start", "-a", runner], check=False)
        result = int(verify(runner)["State"]["ExitCode"])
        return result
    finally:
        for identifier in reversed(ids):
            state = verify(identifier)
            if state["State"]["Running"]:
                # Force removal is limited to these identity-verified disposable
                # tmpfs resources. Nothing is mounted writable from the host.
                command("rm", "--force", identifier)
            else:
                command("rm", identifier)
        assert not command(
            "ps", "-aq", "--filter", f"label=sou2ai.operations.fixture={nonce}"
        )
        print(
            "Verified disposable SQL Server/runner cleanup; no host volumes or ports."
        )


if __name__ == "__main__":
    raise SystemExit(main())
