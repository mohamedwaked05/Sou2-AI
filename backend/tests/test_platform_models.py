"""PostgreSQL tests for users, businesses, memberships, and relationships."""

import uuid

import pytest
from app.database.models import (
    AccountStatus,
    Business,
    BusinessMembership,
    BusinessStatus,
    MembershipPermission,
    User,
)
from sqlalchemy import Engine, delete, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def make_user(email: str = "owner@example.com") -> User:
    return User(
        email=email,
        first_name="Maya",
        last_name="Haddad",
        password_hash="not-a-real-password-hash",
    )


def test_user_uuid_defaults_and_normalization(db_session: Session) -> None:
    user = make_user("  Owner@Example.COM ")
    db_session.add(user)
    db_session.commit()

    assert isinstance(user.id, uuid.UUID)
    assert user.email == "owner@example.com"
    assert user.status is AccountStatus.ACTIVE
    assert user.created_at.tzinfo is not None
    assert user.updated_at.tzinfo is not None


def test_case_insensitive_email_uniqueness(db_session: Session) -> None:
    db_session.add(make_user("OWNER@example.com"))
    db_session.commit()
    db_session.add(make_user(" owner@EXAMPLE.com "))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_user_updated_at_changes_on_direct_update(db_session: Session) -> None:
    user = make_user()
    db_session.add(user)
    db_session.commit()
    original_updated_at = user.updated_at

    db_session.execute(
        text("UPDATE users SET first_name = 'Updated' WHERE id = :id"), {"id": user.id}
    )
    db_session.commit()
    db_session.refresh(user)
    assert user.updated_at > original_updated_at


@pytest.mark.parametrize("column", ["email", "first_name", "last_name"])
def test_whitespace_only_user_fields_are_rejected(
    db_session: Session, column: str
) -> None:
    values = {
        "id": uuid.uuid4(),
        "email": "valid@example.com",
        "first_name": "Maya",
        "last_name": "Haddad",
        "password_hash": "hash",
    }
    values[column] = "   "
    columns = ", ".join(values)
    placeholders = ", ".join(f":{key}" for key in values)
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(f"INSERT INTO users ({columns}) VALUES ({placeholders})"), values
        )


def test_required_user_field_is_enforced(db_session: Session) -> None:
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO users (id, email, first_name, password_hash) "
                "VALUES (:id, :email, :first_name, :password_hash)"
            ),
            {
                "id": uuid.uuid4(),
                "email": "required@example.com",
                "first_name": "Maya",
                "password_hash": "hash",
            },
        )


def test_business_defaults_and_membership_relationship(db_session: Session) -> None:
    user = make_user()
    first = Business(owner=user, name="  Waked   Store ")
    second = Business(owner=user, name="Second Shop")
    db_session.add_all(
        [
            user,
            first,
            second,
            BusinessMembership(user=user, business=first),
            BusinessMembership(user=user, business=second),
        ]
    )
    db_session.commit()

    assert first.name == "Waked Store"
    assert first.normalized_name == "waked store"
    assert first.status is BusinessStatus.PENDING
    assert not first.is_active
    assert first.country == "LB"
    assert first.timezone == "Asia/Beirut"
    assert len(user.memberships) == 2
    assert first.memberships[0].permission is MembershipPermission.FULL_ACCESS


def test_different_users_may_use_same_business_name(db_session: Session) -> None:
    first_user = make_user("first@example.com")
    second_user = make_user("second@example.com")
    first_business = Business(owner=first_user, name="Same Name")
    second_business = Business(owner=second_user, name="same   name")
    db_session.add_all(
        [
            BusinessMembership(user=first_user, business=first_business),
            BusinessMembership(user=second_user, business=second_business),
        ]
    )
    db_session.commit()


def test_one_user_cannot_use_equivalent_business_names(db_session: Session) -> None:
    user = make_user()
    first = Business(owner=user, name="Waked Store")
    second = Business(owner=user, name="waked   store")
    db_session.add_all(
        [
            BusinessMembership(user=user, business=first),
            BusinessMembership(user=user, business=second),
        ]
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_renaming_business_to_owner_equivalent_name_is_rejected(
    db_session: Session,
) -> None:
    user = make_user()
    first = Business(owner=user, name="First Store")
    second = Business(owner=user, name="Second Store")
    db_session.add_all(
        [
            BusinessMembership(user=user, business=first),
            BusinessMembership(user=user, business=second),
        ]
    )
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.execute(
            text("UPDATE businesses SET name = ' first   store ' WHERE id = :id"),
            {"id": second.id},
        )
        db_session.commit()


def test_business_can_have_multiple_distinct_memberships(db_session: Session) -> None:
    owner = make_user("owner@example.com")
    other = make_user("other@example.com")
    business = Business(owner=owner, name="Shared Access Shape")
    db_session.add_all(
        [
            BusinessMembership(user=owner, business=business),
            BusinessMembership(user=other, business=business),
        ]
    )
    db_session.commit()
    assert len(business.memberships) == 2


def test_duplicate_membership_is_rejected(db_session: Session) -> None:
    user = make_user()
    business = Business(owner=user, name="Duplicate Membership")
    db_session.add_all(
        [
            BusinessMembership(user=user, business=business),
            BusinessMembership(user=user, business=business),
        ]
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_membership_restricts_user_and_business_deletion(db_session: Session) -> None:
    user = make_user()
    business = Business(owner=user, name="Protected")
    db_session.add(BusinessMembership(user=user, business=business))
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.execute(delete(User).where(User.id == user.id))
        db_session.commit()
    db_session.rollback()

    with pytest.raises(IntegrityError):
        db_session.execute(delete(Business).where(Business.id == business.id))
        db_session.commit()


def test_business_query_indexes_exist(database_engine: Engine) -> None:
    expected = {
        "businesses": {"uq_businesses_owner_name"},
        "business_memberships": {
            "uq_memberships_user_business",
            "ix_memberships_business",
        },
        "business_opening_days": {"uq_opening_days_business_day"},
        "business_opening_shifts": {
            "uq_opening_shifts_day_interval",
        },
    }
    inspector = inspect(database_engine)
    for table_name, names in expected.items():
        actual = {item["name"] for item in inspector.get_indexes(table_name)}
        assert names <= actual
