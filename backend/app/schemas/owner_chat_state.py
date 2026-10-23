"""Shared language-plan and conversation-context contracts, without source authority."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ConversationContextKind = Literal["discovery", "clarification"]
PendingReply = Literal[
    "selection", "confirmation", "unresolved", "unrelated", "cancel", "replace"
]
EVIDENCED_TRANSITIONS = frozenset({"unrelated", "cancel", "replace"})


def allowed_pending_replies(
    operation: str, decision: str, semantic_operation: str
) -> tuple[PendingReply, ...]:
    """The schema and backend share the same legal transition/action combinations."""
    replies: list[PendingReply] = [
        "selection",
        "confirmation",
        "unresolved",
        "unrelated",
    ]
    if operation == "product_search":
        replies.append("cancel")
    elif operation == "preference":
        if decision == "clear_preference":
            replies = ["replace"]
        elif decision == "set_preference":
            replies = ["selection", "confirmation", "unresolved", "replace"]
        elif decision == "final" and semantic_operation == "conversation":
            replies.append("cancel")
    return tuple(replies)


def pending_evidence_error(reply: str | None, quote: str | None) -> str | None:
    """Structural evidence validation; current-message grounding is checked later."""
    if reply in EVIDENCED_TRANSITIONS:
        if not isinstance(quote, str) or not 1 <= len(quote.strip()) <= 255:
            return "missing_transition_evidence"
    elif quote is not None:
        return "unexpected_transition_evidence"
    return None


class ProductPresentation(BaseModel):
    """Validated language intent carried unchanged into catalogue presentation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    intent: Literal["discovery", "details", "selection", "ambiguous_followup"]
    reference: Literal["current", "offered", "selected"] = "current"
    identifiers: tuple[Literal["id", "sku"], ...] = Field(default=(), max_length=2)
    group_by: Literal["products", "brands"] = "products"

    @model_validator(mode="after")
    def validate_reference(self) -> ProductPresentation:
        if len(set(self.identifiers)) != len(self.identifiers):
            raise ValueError("Identifier requests must be unique.")
        if (
            self.intent in {"selection", "ambiguous_followup"}
            and self.reference != "offered"
        ):
            raise ValueError("Candidate references require the offered set.")
        if self.intent == "discovery" and self.reference == "selected":
            raise ValueError("Discovery cannot select a prior product.")
        return self


class OfferedProduct(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    names: tuple[str, ...]
    sku: str | None = None


class CatalogueFollowupContext(BaseModel):
    """Bounded language context; source authority stays in the backend."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    state_kind: Literal["discovery", "clarification", "invalid_or_expired"]
    candidates: tuple[OfferedProduct, ...] = Field(default=(), max_length=50)
    truncated: bool = False
    semantic_query: str | None = Field(default=None, max_length=255)
    filter_requests: tuple[str, ...] = Field(default=(), max_length=8)
