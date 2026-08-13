"""Load the fixed fixture and comparable multilingual JSONL scenarios."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from app.agent.owner_chat_provider import (
    OwnerChatRequest,
    ProviderBusinessProfile,
    ProviderKnowledge,
    ProviderMessage,
    ProviderWorkingDay,
    ProviderWorkingShift,
)
from pydantic import ValidationError

from experiments.owner_chat_language_eval.models import (
    DATASET_VERSION,
    BusinessFixture,
    EvaluationScenario,
    LanguageGroup,
    ScenarioType,
)

PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_DATASET_PATH = PACKAGE_ROOT / "data" / "scenarios.jsonl"
DEFAULT_FIXTURE_PATH = PACKAGE_ROOT / "data" / "business_fixture.json"

SCENARIO_ORDER = (
    ScenarioType.WORKING_HOURS,
    ScenarioType.KNOWN_POLICY,
    ScenarioType.CONTEXT_FOLLOW_UP,
    ScenarioType.LIVE_INVENTORY,
    ScenarioType.LIVE_SALES_REVENUE,
    ScenarioType.AMBIGUOUS_CLARIFICATION,
    ScenarioType.REUSABLE_FACT,
    ScenarioType.PROMPT_OVERRIDE,
    ScenarioType.CROSS_BUSINESS_PRIVATE_DATA,
    ScenarioType.PROFILE_CONTRADICTION,
)
LANGUAGE_CODES = {
    LanguageGroup.ENGLISH: "en",
    LanguageGroup.ARABIC: "ar",
    LanguageGroup.LEBANESE_ARABIC: "lb",
    LanguageGroup.FRANCO_ARABIC: "fr",
    LanguageGroup.MIXED_LANGUAGE: "mx",
}


def dataset_fingerprint(path: Path = DEFAULT_DATASET_PATH) -> str:
    """Return a stable SHA-256 fingerprint of the exact dataset bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_fixture(path: Path = DEFAULT_FIXTURE_PATH) -> BusinessFixture:
    """Load and validate the fixed fictional business fixture."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return BusinessFixture.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"Invalid evaluation fixture: {path}") from exc


def load_dataset(path: Path = DEFAULT_DATASET_PATH) -> list[EvaluationScenario]:
    """Load and enforce the exact 50-scenario comparison matrix."""
    scenarios: list[EvaluationScenario] = []
    try:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                scenarios.append(EvaluationScenario.model_validate_json(line))
            except ValidationError as exc:
                raise ValueError(f"Invalid scenario at {path}:{line_number}") from exc
    except OSError as exc:
        raise ValueError(f"Unable to read evaluation dataset: {path}") from exc

    if len(scenarios) != 50:
        raise ValueError("Milestone 9 dataset must contain exactly 50 scenarios.")
    identifiers = [scenario.id for scenario in scenarios]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Milestone 9 scenario IDs must be unique.")

    language_counts = Counter(scenario.language for scenario in scenarios)
    if any(language_counts[language] != 10 for language in LanguageGroup):
        raise ValueError("Each language group must contain exactly 10 scenarios.")
    required_types = set(SCENARIO_ORDER)
    for language in LanguageGroup:
        language_scenarios = [
            scenario for scenario in scenarios if scenario.language is language
        ]
        if {
            scenario.scenario_type for scenario in language_scenarios
        } != required_types:
            raise ValueError(
                f"Language group {language.value} must contain all scenario types."
            )
        for scenario in language_scenarios:
            expected_index = SCENARIO_ORDER.index(scenario.scenario_type) + 1
            expected_prefix = f"m9-{LANGUAGE_CODES[language]}-{expected_index:02d}-"
            if not scenario.id.startswith(expected_prefix):
                raise ValueError(
                    f"Scenario ID {scenario.id} does not match its matrix position."
                )
            if scenario.dataset_version != DATASET_VERSION:
                raise ValueError("Scenario dataset version is unsupported.")
    return scenarios


def select_scenarios(
    scenarios: list[EvaluationScenario], scenario_ids: list[str] | None
) -> list[EvaluationScenario]:
    """Filter in dataset order and reject unknown or repeated selections."""
    if not scenario_ids:
        return list(scenarios)
    if len(set(scenario_ids)) != len(scenario_ids):
        raise ValueError("Scenario IDs for a selective rerun must be unique.")
    known = {scenario.id for scenario in scenarios}
    unknown = sorted(set(scenario_ids) - known)
    if unknown:
        raise ValueError(f"Unknown scenario IDs: {', '.join(unknown)}")
    selected = set(scenario_ids)
    return [scenario for scenario in scenarios if scenario.id in selected]


def build_provider_request(
    scenario: EvaluationScenario, fixture: BusinessFixture
) -> OwnerChatRequest:
    """Map evaluation data into the existing production provider contract."""
    profile = fixture.profile
    return OwnerChatRequest(
        profile=ProviderBusinessProfile(
            name=profile.name,
            description=profile.description,
            category=profile.category,
            governorate=profile.governorate,
            district=profile.district,
            city=profile.city,
            address_line=profile.address_line,
            timezone=profile.timezone,
            working_hours=tuple(
                ProviderWorkingDay(
                    weekday=day.weekday,
                    is_open=day.is_open,
                    shifts=tuple(
                        ProviderWorkingShift(start=shift.start, end=shift.end)
                        for shift in day.shifts
                    ),
                )
                for day in profile.working_hours
            ),
        ),
        knowledge=tuple(
            ProviderKnowledge(
                subject_key=fact.subject_key,
                content=fact.content,
                category=fact.category,
                expires_at=fact.expires_at,
            )
            for fact in fixture.knowledge
        ),
        messages=tuple(
            ProviderMessage(role=message.role, content=message.content)
            for message in scenario.messages
        ),
        requested_at=fixture.requested_at,
        max_output_tokens=fixture.max_output_tokens,
    )
