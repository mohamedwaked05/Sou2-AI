"""Command-line interface for the Milestone 9 language evaluation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.agent.owner_chat_provider import OllamaOwnerChatProvider
from app.core.config import Settings

from experiments.owner_chat_language_eval.dataset import (
    dataset_fingerprint,
    load_dataset,
    load_fixture,
    select_scenarios,
)
from experiments.owner_chat_language_eval.scoring import (
    build_scoring_template,
    load_json_document,
    render_report,
    validate_completed_scoring,
)
from experiments.owner_chat_language_eval.workflow import (
    DEFAULT_BASELINE_PATH,
    execute_evaluation,
    persist_run_document,
    write_json_exclusive,
    write_text_exclusive,
)

PACKAGE_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
DEFAULT_SCORING_PATH = PACKAGE_ROOT / "results" / "manual_scoring.json"
DEFAULT_REPORT_PATH = PACKAGE_ROOT / "results" / "report.md"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate the existing Sou2AI Ollama owner-chat provider."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "validate-dataset", help="Validate the fixed fixture and 50-scenario matrix."
    )

    run = subparsers.add_parser("run", help="Run a baseline or selected scenarios.")
    run.add_argument(
        "--scenario-id",
        action="append",
        dest="scenario_ids",
        help="Stable scenario ID to rerun; repeat for multiple IDs.",
    )
    run.add_argument("--output", type=Path)

    prepare = subparsers.add_parser(
        "prepare-scoring", help="Create a human-editable scoring artifact."
    )
    prepare.add_argument("--run", type=Path, default=DEFAULT_BASELINE_PATH)
    prepare.add_argument("--output", type=Path, default=DEFAULT_SCORING_PATH)

    validate_scoring = subparsers.add_parser(
        "validate-scoring", help="Require complete rubric and critical review."
    )
    validate_scoring.add_argument("--input", type=Path, required=True)

    report = subparsers.add_parser(
        "report", help="Generate Markdown from fully validated human scoring."
    )
    report.add_argument("--input", type=Path, required=True)
    report.add_argument("--output", type=Path, default=DEFAULT_REPORT_PATH)
    report.add_argument(
        "--rerun",
        type=Path,
        action="append",
        default=[],
        help="Optional completed selective-rerun artifact; repeat as needed.",
    )
    return parser


def _run_command(arguments: argparse.Namespace) -> int:
    scenarios = load_dataset()
    fixture = load_fixture()
    selected = select_scenarios(scenarios, arguments.scenario_ids)
    run_kind = "selective_rerun" if arguments.scenario_ids else "baseline"

    if arguments.output is not None and arguments.output.exists():
        raise ValueError(f"Refusing to overwrite existing result: {arguments.output}")
    if (
        run_kind == "baseline"
        and arguments.output is None
        and DEFAULT_BASELINE_PATH.exists()
    ):
        raise ValueError(
            f"Completed baseline already exists and will not be replaced: "
            f"{DEFAULT_BASELINE_PATH}"
        )

    settings = Settings(_env_file=REPOSITORY_ROOT / "backend" / ".env")
    provider = OllamaOwnerChatProvider(
        base_url=settings.ollama_base_url,
        model=settings.ollama_chat_model,
        timeout_seconds=settings.ollama_request_timeout_seconds,
    )
    document = execute_evaluation(
        provider=provider,
        scenarios=selected,
        fixture=fixture,
        run_kind=run_kind,
    )
    output_path = persist_run_document(document, requested_path=arguments.output)
    print(f"{document['status']} evaluation artifact: {output_path}")
    return 0 if document["status"] == "complete" else 2


def main(argv: list[str] | None = None) -> int:
    """Run one explicit evaluation workflow command."""
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "validate-dataset":
            scenarios = load_dataset()
            load_fixture()
            print(
                f"valid dataset: {len(scenarios)} scenarios, "
                f"sha256={dataset_fingerprint()}"
            )
            return 0
        if arguments.command == "run":
            return _run_command(arguments)
        if arguments.command == "prepare-scoring":
            run_document = load_json_document(arguments.run)
            template = build_scoring_template(run_document)
            output_path = write_json_exclusive(arguments.output, template)
            print(f"scoring template: {output_path}")
            return 0
        if arguments.command == "validate-scoring":
            scoring = load_json_document(arguments.input)
            validate_completed_scoring(scoring)
            print("scoring is complete and valid")
            return 0
        if arguments.command == "report":
            scoring = load_json_document(arguments.input)
            reruns = [load_json_document(path) for path in arguments.rerun]
            report = render_report(scoring, selective_reruns=reruns)
            output_path = write_text_exclusive(
                arguments.output, report, artifact_name="report"
            )
            print(f"final report: {output_path}")
            return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    parser.error("Unknown command.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
