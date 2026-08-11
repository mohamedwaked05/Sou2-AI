"""Milestone 4 business management, onboarding, and tenant-isolation tests."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.core.security import create_access_token, hash_password, utc_now
from app.database.models import (
    Business,
    BusinessMembership,
    MembershipPermission,
    User,
)
from app.services.businesses import create_business
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker


def create_user(session: Session, email: str) -> User:
    user = User(
        email=email,
        first_name="Business",
        last_name="Owner",
        password_hash=hash_password("Strong1!Pass"),
        email_verified_at=utc_now(),
    )
    session.add(user)
    session.commit()
    return user


def headers(user: User) -> dict[str, str]:
    token, _ = create_access_token(user.id, get_settings())
    return {"Authorization": f"Bearer {token}"}


def create_draft(
    client: TestClient, user: User, name: str = "Waked Market"
) -> dict[str, object]:
    response = client.post(
        "/api/v1/businesses", json={"name": name}, headers=headers(user)
    )
    assert response.status_code == 201, response.text
    return response.json()


def valid_hours() -> list[dict[str, object]]:
    weekdays = [
        "MONDAY",
        "TUESDAY",
        "WEDNESDAY",
        "THURSDAY",
        "FRIDAY",
        "SATURDAY",
        "SUNDAY",
    ]
    return [
        {
            "weekday": weekday,
            "is_closed": weekday in {"SATURDAY", "SUNDAY"},
            "shifts": (
                []
                if weekday in {"SATURDAY", "SUNDAY"}
                else [{"start": "09:00", "end": "17:00"}]
            ),
        }
        for weekday in weekdays
    ]


def complete_profile(client: TestClient, user: User, business_id: str) -> object:
    return client.patch(
        f"/api/v1/businesses/{business_id}",
        headers=headers(user),
        json={
            "description": "A neighborhood grocery and household goods market.",
            "category": "GROCERY_SUPERMARKET",
            "governorate": "Mount Lebanon",
            "district": "Metn",
            "city": "Antelias",
            "address_line": "Main road, building 12",
            "working_hours": valid_hours(),
        },
    )


def test_creation_requires_authentication(api_client: TestClient) -> None:
    response = api_client.post("/api/v1/businesses", json={"name": "Private Shop"})
    assert response.status_code == 401


def test_creator_membership_and_pending_draft_defaults(
    api_client: TestClient, db_session: Session
) -> None:
    user = create_user(db_session, "creator@example.com")
    payload = create_draft(api_client, user, "  Waked   Market  ")

    assert payload["name"] == "Waked Market"
    assert payload["status"] == "PENDING"
    assert payload["is_active"] is False
    assert payload["profile_complete"] is False
    assert payload["first_incomplete_section"] == "business_details"
    assert payload["onboarding_submitted_at"] is None
    membership = db_session.scalar(
        select(BusinessMembership).where(
            BusinessMembership.business_id == uuid.UUID(payload["id"])
        )
    )
    assert membership is not None
    assert membership.user_id == user.id
    assert membership.permission is MembershipPermission.FULL_ACCESS


@pytest.mark.parametrize(
    "protected",
    [
        "is_active",
        "status",
        "profile_complete",
        "owner_user_id",
        "memberships",
        "created_at",
    ],
)
def test_creation_rejects_protected_fields(
    api_client: TestClient, db_session: Session, protected: str
) -> None:
    user = create_user(db_session, f"{protected}@example.com")
    response = api_client.post(
        "/api/v1/businesses",
        json={"name": "Protected Fields", protected: True},
        headers=headers(user),
    )
    assert response.status_code == 422


def test_creation_rolls_back_if_membership_insert_fails(
    api_client: TestClient, db_session: Session
) -> None:
    user = create_user(db_session, "atomic@example.com")
    db_session.execute(
        text(
            """
            CREATE FUNCTION test_reject_membership() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'test rejection' USING ERRCODE = '23514'; END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER test_reject_membership
            BEFORE INSERT ON business_memberships
            FOR EACH ROW EXECUTE FUNCTION test_reject_membership();
            """
        )
    )
    db_session.commit()
    try:
        response = api_client.post(
            "/api/v1/businesses",
            json={"name": "Atomic Draft"},
            headers=headers(user),
        )
        assert response.status_code == 409
        assert db_session.scalar(select(func.count()).select_from(Business)) == 0
        assert (
            db_session.scalar(select(func.count()).select_from(BusinessMembership)) == 0
        )
    finally:
        db_session.execute(
            text(
                "DROP TRIGGER IF EXISTS test_reject_membership "
                "ON business_memberships; "
                "DROP FUNCTION IF EXISTS test_reject_membership()"
            )
        )
        db_session.commit()


def test_owner_name_uniqueness_and_similar_names(
    api_client: TestClient, db_session: Session
) -> None:
    owner = create_user(db_session, "names@example.com")
    other = create_user(db_session, "other-names@example.com")
    create_draft(api_client, owner, "Waked Market")
    assert create_draft(api_client, owner, "Waked-Market")["name"] == "Waked-Market"
    assert create_draft(api_client, owner, "Waked Mini Market")["name"] == (
        "Waked Mini Market"
    )
    assert create_draft(api_client, other, "waked market")["name"] == "waked market"

    duplicate = api_client.post(
        "/api/v1/businesses",
        json={"name": "  WAKED   MARKET "},
        headers=headers(owner),
    )
    assert duplicate.status_code == 409
    assert "constraint" not in duplicate.text.lower()


def test_concurrent_duplicate_creation_has_one_winner_and_no_orphan(
    database_engine: Engine, db_session: Session
) -> None:
    owner = create_user(db_session, "race@example.com")
    owner_id = owner.id
    barrier = Barrier(2)
    factory = sessionmaker(bind=database_engine, expire_on_commit=False)

    def attempt() -> bool:
        with factory() as session:
            thread_user = session.get(User, owner_id)
            assert thread_user is not None
            barrier.wait()
            try:
                create_business(session, thread_user, "Race   Market")
            except ApplicationError as exc:
                assert exc.status_code == 409
                return False
            return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))

    assert sorted(results) == [False, True]
    businesses = db_session.scalars(
        select(Business).where(Business.owner_user_id == owner_id)
    ).all()
    assert len(businesses) == 1
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(BusinessMembership)
            .where(BusinessMembership.business_id == businesses[0].id)
        )
        == 1
    )


def test_list_detail_and_cross_tenant_isolation(
    api_client: TestClient, db_session: Session
) -> None:
    first_user = create_user(db_session, "first-tenant@example.com")
    second_user = create_user(db_session, "second-tenant@example.com")
    first = create_draft(api_client, first_user, "First Business")
    second = create_draft(api_client, first_user, "Second Business")
    foreign = create_draft(api_client, second_user, "Foreign Business")

    first_list = api_client.get(
        "/api/v1/businesses", headers=headers(first_user)
    ).json()
    repeated = api_client.get("/api/v1/businesses", headers=headers(first_user)).json()
    assert [item["id"] for item in first_list] == [item["id"] for item in repeated]
    assert {item["id"] for item in first_list} == {first["id"], second["id"]}
    assert foreign["id"] not in {item["id"] for item in first_list}

    assert (
        api_client.get(
            f"/api/v1/businesses/{first['id']}", headers=headers(first_user)
        ).status_code
        == 200
    )
    for method, path in (
        ("get", f"/api/v1/businesses/{foreign['id']}"),
        ("patch", f"/api/v1/businesses/{foreign['id']}"),
        ("post", f"/api/v1/businesses/{foreign['id']}/onboarding/confirm"),
    ):
        response = getattr(api_client, method)(
            path,
            headers=headers(first_user),
            **({"json": {"description": None}} if method == "patch" else {}),
        )
        assert response.status_code == 404
        assert "Foreign Business" not in response.text

    unknown = api_client.get(
        f"/api/v1/businesses/{uuid.uuid4()}", headers=headers(first_user)
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "business_not_found"


def test_membership_not_creator_ownership_grants_business_access(
    api_client: TestClient, db_session: Session
) -> None:
    owner = create_user(db_session, "membership-owner@example.com")
    member = create_user(db_session, "membership-member@example.com")
    business = create_draft(api_client, owner, "Membership Scoped")
    db_session.add(
        BusinessMembership(
            user_id=member.id,
            business_id=uuid.UUID(business["id"]),
            permission=MembershipPermission.FULL_ACCESS,
        )
    )
    db_session.commit()

    detail = api_client.get(
        f"/api/v1/businesses/{business['id']}", headers=headers(member)
    )
    assert detail.status_code == 200
    listed = api_client.get("/api/v1/businesses", headers=headers(member)).json()
    assert [item["id"] for item in listed] == [business["id"]]


def test_sections_are_resumable_and_confirmation_retains_first_timestamp(
    api_client: TestClient, db_session: Session
) -> None:
    user = create_user(db_session, "onboarding@example.com")
    business = create_draft(api_client, user)
    business_id = business["id"]

    details = api_client.patch(
        f"/api/v1/businesses/{business_id}",
        headers=headers(user),
        json={
            "description": "A neighborhood grocery and household goods market.",
            "category": "GROCERY_SUPERMARKET",
        },
    )
    assert details.status_code == 200
    assert details.json()["first_incomplete_section"] == "location"
    incomplete = api_client.post(
        f"/api/v1/businesses/{business_id}/onboarding/confirm",
        headers=headers(user),
    )
    assert incomplete.status_code == 422
    assert incomplete.json()["error"]["details"]["first_incomplete_section"] == (
        "location"
    )

    completed = complete_profile(api_client, user, business_id)
    assert completed.status_code == 200, completed.text
    assert completed.json()["profile_complete"] is True
    assert completed.json()["is_active"] is False
    confirmed = api_client.post(
        f"/api/v1/businesses/{business_id}/onboarding/confirm",
        headers=headers(user),
    )
    assert confirmed.status_code == 200
    first_submitted = confirmed.json()["onboarding_submitted_at"]
    assert first_submitted is not None
    again = api_client.post(
        f"/api/v1/businesses/{business_id}/onboarding/confirm",
        headers=headers(user),
    )
    assert again.json()["onboarding_submitted_at"] == first_submitted

    cleared = api_client.patch(
        f"/api/v1/businesses/{business_id}",
        headers=headers(user),
        json={"description": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["profile_complete"] is False
    assert cleared.json()["is_active"] is False
    assert cleared.json()["onboarding_submitted_at"] == first_submitted


def test_category_and_location_rules(
    api_client: TestClient, db_session: Session
) -> None:
    user = create_user(db_session, "rules@example.com")
    business = create_draft(api_client, user)
    path = f"/api/v1/businesses/{business['id']}"

    assert (
        api_client.patch(
            path,
            headers=headers(user),
            json={"category": "OTHER"},
        ).status_code
        == 422
    )
    assert (
        api_client.patch(
            path,
            headers=headers(user),
            json={"category": "OTHER", "custom_category": "  "},
        ).status_code
        == 422
    )
    assert (
        api_client.patch(
            path,
            headers=headers(user),
            json={"category": "OTHER", "custom_category": "Pet supplies"},
        ).status_code
        == 200
    )
    assert (
        api_client.patch(
            path,
            headers=headers(user),
            json={"category": "BAKERY", "custom_category": "Pastries"},
        ).status_code
        == 422
    )
    predefined = api_client.patch(
        path,
        headers=headers(user),
        json={"category": "BAKERY"},
    )
    assert predefined.status_code == 200
    assert predefined.json()["custom_category"] is None

    for payload in (
        {"governorate": "Unknown"},
        {"district": "Unknown"},
        {"city": "Unknown"},
        {
            "governorate": "Mount Lebanon",
            "district": "Metn",
            "city": "Tripoli",
        },
    ):
        assert api_client.patch(
            path, headers=headers(user), json=payload
        ).status_code == (422)
    valid = api_client.patch(
        path,
        headers=headers(user),
        json={
            "governorate": "South",
            "district": "Saida",
            "city": "Abra",
            "address_line": "Main road, shop 4",
        },
    )
    assert valid.status_code == 200


@pytest.mark.parametrize(
    "hours",
    [
        valid_hours()[:-1],
        valid_hours()[:-1] + [valid_hours()[0]],
        [
            *valid_hours()[:-1],
            {"weekday": "FUNDAY", "is_closed": True, "shifts": []},
        ],
        [
            {**valid_hours()[0], "is_closed": True},
            *valid_hours()[1:],
        ],
        [
            {**valid_hours()[0], "shifts": []},
            *valid_hours()[1:],
        ],
        [
            {
                **valid_hours()[0],
                "shifts": [
                    {"start": "08:00", "end": "09:00"},
                    {"start": "10:00", "end": "11:00"},
                    {"start": "12:00", "end": "13:00"},
                    {"start": "14:00", "end": "15:00"},
                ],
            },
            *valid_hours()[1:],
        ],
        [
            {
                **valid_hours()[0],
                "shifts": [{"start": "17:00", "end": "09:00"}],
            },
            *valid_hours()[1:],
        ],
        [
            {
                **valid_hours()[0],
                "shifts": [
                    {"start": "09:00", "end": "13:00"},
                    {"start": "12:00", "end": "17:00"},
                ],
            },
            *valid_hours()[1:],
        ],
    ],
)
def test_invalid_complete_working_hours_roll_back(
    api_client: TestClient,
    db_session: Session,
    hours: list[dict[str, object]],
) -> None:
    user = create_user(db_session, "hours@example.com")
    business = create_draft(api_client, user)
    response = api_client.patch(
        f"/api/v1/businesses/{business['id']}",
        headers=headers(user),
        json={"working_hours": hours},
    )
    assert response.status_code == 422
    detail = api_client.get(
        f"/api/v1/businesses/{business['id']}", headers=headers(user)
    )
    assert detail.json()["working_hours"] == []


def test_three_shifts_sort_chronologically_and_adjacent_are_allowed(
    api_client: TestClient, db_session: Session
) -> None:
    user = create_user(db_session, "sorted-hours@example.com")
    business = create_draft(api_client, user)
    hours = valid_hours()
    hours[0]["shifts"] = [
        {"start": "13:00", "end": "17:00"},
        {"start": "09:00", "end": "12:00"},
        {"start": "12:00", "end": "13:00"},
    ]
    response = api_client.patch(
        f"/api/v1/businesses/{business['id']}",
        headers=headers(user),
        json={"working_hours": hours},
    )
    assert response.status_code == 200, response.text
    assert [
        item["start"] for item in response.json()["working_hours"][0]["shifts"]
    ] == [
        "09:00:00",
        "12:00:00",
        "13:00:00",
    ]


def test_duplicate_name_update_is_conflict_and_atomic(
    api_client: TestClient, db_session: Session
) -> None:
    user = create_user(db_session, "rename@example.com")
    create_draft(api_client, user, "First Market")
    second = create_draft(api_client, user, "Second Market")
    response = api_client.patch(
        f"/api/v1/businesses/{second['id']}",
        headers=headers(user),
        json={
            "name": " FIRST   MARKET ",
            "description": "This change must be rolled back with the duplicate rename.",
        },
    )
    assert response.status_code == 409
    detail = api_client.get(
        f"/api/v1/businesses/{second['id']}", headers=headers(user)
    ).json()
    assert detail["name"] == "Second Market"
    assert detail["description"] is None
