"""Tenant-specific, opt-in development answers inside the normal owner turn.

Catalogue and Sales still use the approved, audited tool executor. Demo values
never enter source tables or capability approval history.
"""

import calendar
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.owner_chat_provider import OwnerChatResult
from app.core.config import Settings
from app.database.models import (
    ChatGenerationState,
    OperationalDataSourceStatus,
    OwnerChatMessage,
    ToolCallLog,
    ToolCallStatus,
    User,
)
from app.integrations.discovery import SourceMappingError
from app.integrations.hilaleyih_test import (
    BUSINESS_ID,
    SOURCE_ID,
    HilaleyihHistoricalReader,
    demand_proxy,
    estimated_price,
)
from app.integrations.profiles import ConnectionProfileRegistry
from app.schemas.source_mapping import CatalogueResult
from app.services.businesses import load_full_access_business
from app.services.source_mapping import connector_for
from app.tools.operational import OperationalToolExecutor, ToolExecutionError
from app.utils.argument_hashing import hash_tool_arguments


def enabled(settings: Settings, business_id: uuid.UUID) -> bool:
    return (
        settings.hilaleyih_test_mode
        and settings.environment.casefold() == "development"
        and str(business_id) == BUSINESS_ID
    )


def month_bounds(message: str) -> tuple[date, date] | None:
    for month in range(1, 13):
        match = re.search(
            rf"\b(?:{calendar.month_name[month]}|{calendar.month_abbr[month]})"
            r"\s+(20\d{2})\b",
            message,
            re.I,
        )
        if match:
            year = int(match[1])
            return date(year, month, 1), (
                date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            )
    return None


def product_query(message: str) -> str:
    message = re.sub(
        r"\b(?:in|at)\s+(?:the\s+)?\w+\s+branch\b", "", message, flags=re.I
    )
    noise = set(
        "what how much many is are does do the this that product it its of for "
        "cost costs price prices pricing stock inventory bottle bottles "
        "barcode identifier sku code check in at our please me give have we "
        "available availability".split()
    )
    words = re.findall(r"\d+(?:\.\d+)?|[^\W\d_]+", message.casefold())
    return " ".join(
        "bottle" if word == "bottles" else word for word in words if word not in noise
    )


def product_identifier(message: str) -> str | None:
    explicit = re.search(
        r"\b(?:product\s+)?(?:barcode|identifier|sku|code)\s*[:#]?\s*"
        r"([a-z0-9][a-z0-9._-]{0,127})",
        message,
        re.I,
    )
    if explicit:
        return explicit[1].rstrip("?.")
    return message.strip() if re.fullmatch(r"\d{6,}", message.strip()) else None


def exact_product(result: CatalogueResult, query: str) -> CatalogueResult:
    """Prefer a unique complete source name/identity, never a guessed variant."""
    normalized = product_query(query).replace(" ", "")
    exact = tuple(
        product
        for product in result.items
        if query.strip() in (product.external_product_id, product.sku)
        or any(
            product_query(name).replace(" ", "") == normalized for name in product.names
        )
    )
    if not result.truncated and exact:
        return result.model_copy(
            update={
                "items": exact,
                "status": "resolved" if len(exact) == 1 else "ambiguous",
            }
        )
    return result


def render_inventory(name: str, evidence: Mapping[str, Any]) -> str:
    prefix = f"Hilaleyih Market — {name}: "
    if evidence.get("balance") is not None:
        return (
            f"{prefix}historical source balance {evidence['balance']} "
            f"{evidence['unit']} from {evidence['balance_records']} matched "
            "StockInventory records (company hila23, matching product/unit, "
            "unique source warehouse keys). Source balance date is unknown. "
            "Confidence: low. Not verified current stock."
        )
    quantity = demand_proxy(
        Decimal(str(evidence["demand_quantity"])), evidence["calendar_days"] or 0
    )
    if quantity is None or not evidence["usable_lines"] or not evidence["unit"]:
        return (
            f"{prefix}no evidence-based numerical estimate is possible. "
            f"{evidence['balance_reason']} Product-specific usable base-unit "
            f"sales records: {evidence['usable_lines']}; unusable quantity/unit "
            f"records: {evidence['unusable_lines']}. Confidence: insufficient. "
            "Missing evidence does not mean zero stock."
        )
    records = ", ".join(evidence["records"])
    return (
        f"{prefix}{quantity} {evidence['unit']} DEMO stock proxy. "
        f"{evidence['balance_reason']} Source: SalesFooter joined to Paid SI "
        "SalesHeader, company hila23, using the complete document identity. "
        f"Historical period: {evidence['first_date']} through "
        f"{evidence['last_date']} inclusive (Asia/Beirut), "
        f"{evidence['calendar_days']} calendar days. "
        f"{evidence['usable_lines']} usable product lines supply "
        f"{evidence['demand_quantity']} {evidence['unit']} positive demand "
        f"(paid quantity plus free quantity, verified same unit and pack factor 1). "
        f"Excluded negative documents/lines: {evidence['negative_lines']} "
        "(classification unverified); unusable unit/quantity lines: "
        f"{evidence['unusable_lines']}; orphan lines: {evidence['orphan_lines']}. "
        "Replenishment assumption: restock once to seven days of average "
        "historical demand, rounded up to one source unit; no safety stock. "
        f"Calculation: ceil({evidence['demand_quantity']} / "
        f"{evidence['calendar_days']} × 7) = {quantity} {evidence['unit']}. "
        "This is a modeled replenishment target, not an observed on-hand balance. "
        f"Example source record keys (type/invoice/line/company/partition): {records}. "
        "Confidence: low; historical demand and an assumed replenishment target "
        "cannot establish current stock. One store only; "
        "not verified current inventory."
    )


def render_financial(rows: Sequence[Mapping[str, Any]], start: date, end: date) -> str:
    prefix = (
        f"Provisional historical calculations for {start} through {end} "
        "(end exclusive, Asia/Beirut), hila23 / Paid SI. "
    )
    if not rows:
        return prefix + "0 documents; no recorded amounts in this historical interval."
    parts = []
    for row in rows:
        settlement = (
            "Settlement discrepancies cannot be assessed: SQL Server denies "
            "payment/remaining-balance reads. This does not mean they reconcile. "
            if row["settlement_discrepancies"] is None
            else "Settlement discrepancies (>0.01): "
            f"{row['settlement_discrepancies']}; "
            "signed paid + remaining minus calculated amount difference: "
            f"{row['settlement_difference']}. "
        )
        parts.append(
            f"Source currency code {row['currency_code']!s}: "
            f"{row['documents']:,} documents; gross {row['gross']}; "
            f"discounts {row['discounts']}; taxes {row['taxes']}; "
            f"signed gross − discounts + taxes = {row['signed_total']}. "
            f"Included negative documents: {row['negative_documents']} "
            f"(signed amount {row['negative_total']}); their refund/credit/adjustment "
            f"classification is unknown. {settlement}"
            f"Incomplete amount records: {row['incomplete_documents']} "
            "(SQL sums omit NULL components; totals may be incomplete)."
        )
    return (
        prefix
        + "\n".join(parts)
        + (
            "\nAmounts are kept separate by source currency code, without conversion. "
            "These are provisional historical calculations, not audited or approved "
            "revenue. Discount amounts are subtracted once; discount percentages are "
            "not applied again. Negative documents are retained, not verified refunds."
        )
    )


def answer(
    session: Session,
    business_id: uuid.UUID,
    user: User,
    owner: OwnerChatMessage,
    settings: Settings,
    profiles: ConnectionProfileRegistry | None,
) -> OwnerChatResult | None:
    if not enabled(settings, business_id) or profiles is None:
        return None
    text = owner.content.casefold()
    identifier = product_identifier(owner.content)
    branch_request = bool(re.search(r"\bjbeil\b|\bbranch(?:es)?\b", text))
    operation = (
        "pricing"
        if re.search(r"\b(price|prices|pricing|cost)\b", text)
        else "inventory"
        if re.search(r"\b(stock|inventory)\b", text)
        else "financial"
        if re.search(r"\brevenue\b", text)
        else "catalogue"
        if re.search(r"how many products.*catalogue", text)
        else "sales"
        if re.search(r"\bpaid\s+si\b.*\b(?:documents|sales)\b", text)
        else "product"
        if identifier
        else "inventory"
        if branch_request
        else None
    )
    previous = session.scalar(
        select(OwnerChatMessage).where(
            OwnerChatMessage.conversation_id == owner.conversation_id,
            OwnerChatMessage.sequence_number == owner.sequence_number - 2,
            OwnerChatMessage.generation_state == ChatGenerationState.COMPLETED,
        )
    )
    context = previous.operational_clarification if previous else None
    if not (
        isinstance(context, dict)
        and context.get("operation") == "hilaleyih_test"
        and context.get("source_id") == SOURCE_ID
        and context.get("user_id") == str(user.id)
        and context.get("expires_at", "") > datetime.now(UTC).isoformat()
    ):
        context = None
    if (
        operation is None
        and context
        and (
            text.strip().isdigit()
            or owner.content.strip() in context.get("candidates", [])
            or (
                "candidate_names" not in context
                and (context.get("candidates") or context.get("product_id"))
                and product_query(text)
                and len(text.split()) <= 8
            )
            or (
                product_query(text)
                and set(product_query(text).split())
                <= {
                    token
                    for names in context.get("candidate_names", {}).values()
                    for name in names
                    for token in product_query(name).split()
                }
            )
        )
    ):
        operation = context["capability"]
    if operation is None and re.search(r"\d+(?:\.\d+)?\s*(?:ml|l|kg|g)\b", text):
        operation = "product"
    if identifier and context and operation == "product":
        operation = context["capability"]
    if operation is None:
        return None
    load_full_access_business(session, user, business_id)
    executor = OperationalToolExecutor(session, profiles, settings)
    source = executor._active_source(business_id)
    if (
        source is None
        or str(source.id) != SOURCE_ID
        or source.connection_profile_key != "hila_catalogue_dev"
        or source.status != OperationalDataSourceStatus.ACTIVE
    ):
        return None
    secret = settings.tool_call_audit_hmac_secret
    if secret is None or not secret.get_secret_value().strip():
        raise ToolExecutionError("audit_unavailable")
    started = time.monotonic()
    audit = ToolCallLog(
        business_id=business_id,
        user_id=user.id,
        tool_name=f"hilaleyih_test_{operation}",
        args_hash=hash_tool_arguments({"message_id": str(owner.id)}, secret),
        status=ToolCallStatus.SUCCESS,
    )

    def lookup(arguments: dict[str, Any]) -> CatalogueResult:
        result = executor.execute(
            user=user,
            business_id=business_id,
            tool_name="product_search",
            arguments=arguments,
        ).output
        if not isinstance(result, CatalogueResult):
            raise SourceMappingError("hilaleyih_catalogue_required")
        return result

    try:
        if branch_request:
            reply = (
                "Hilaleyih Market is one store and has no branches. "
                "There is no Jbeil branch or branch-specific stock. "
                "I can check the selected product for Hilaleyih Market only."
            )
            if context:
                owner.operational_clarification = dict(context, capability="inventory")
        elif operation == "catalogue":
            result = lookup({"browse": True, "limit": 1})
            total = result.matched_records
            if total is None and result.coverage:
                total = result.coverage.valid_records
            excluded = result.coverage.excluded_records if result.coverage else 0
            reply = (
                (
                    f"Our approved catalogue contains {total:,} valid products. "
                    f"Excluded source records: {excluded}. "
                    "Catalogue identities do not establish current stock "
                    "or checkout prices."
                )
                if total is not None
                else "The catalogue count requires fresh validation."
            )
        elif operation in {"financial", "sales"}:
            bounds = month_bounds(owner.content)
            if bounds is None:
                reply = "Which historical month and year should I calculate?"
            elif operation == "sales":
                from zoneinfo import ZoneInfo

                start, end = bounds
                zone = ZoneInfo("Asia/Beirut")
                result = executor.execute(
                    user=user,
                    business_id=business_id,
                    tool_name="operational_report",
                    arguments={
                        "capability": "sales",
                        "start": datetime.combine(
                            start, datetime.min.time(), zone
                        ).isoformat(),
                        "end": datetime.combine(
                            end, datetime.min.time(), zone
                        ).isoformat(),
                    },
                ).output
                if result.status != "ok":
                    raise SourceMappingError("sales_approval_required")
                count = sum(item.transaction_count for item in result.totals)
                reply = (
                    f"{count:,} Paid SI sales documents were recorded from {start} "
                    f"through {end} (end exclusive, Asia/Beirut). Includes negative "
                    "documents and headers without lines; this is a historical "
                    "document count, not product quantities or verified refunds."
                )
            else:
                reader = HilaleyihHistoricalReader(
                    connector_for(profiles, source), settings
                )
                reply = render_financial(reader.financial_summary(*bounds), *bounds)
        else:
            scope = executor.catalogue_scope(source)
            if context and (
                context.get("mapping_version") != scope.mapping_version
                or context.get("schema_fingerprint") != scope.schema_fingerprint
                or context.get("source_fingerprint") != scope.source_fingerprint
            ):
                context = None
            query = identifier or product_query(owner.content)
            choices = context.get("candidates", []) if context else []
            offered_names = context.get("candidate_names", {}) if context else {}
            selected_id = None
            invalid_number = False
            if text.strip() in choices:
                selected_id = text.strip()
            elif re.fullmatch(r"\d{1,3}", text.strip()) and choices:
                index = int(text.strip()) - 1
                if 0 <= index < len(choices):
                    selected_id = choices[index]
                else:
                    invalid_number = True
            elif query and offered_names:
                matches = [
                    key
                    for key, names in offered_names.items()
                    if any(
                        product_query(name).replace(" ", "")
                        == product_query(query).replace(" ", "")
                        for name in names
                    )
                ]
                if len(matches) == 1:
                    selected_id = matches[0]
            if not selected_id and context and context.get("product_id"):
                current = lookup(
                    {
                        "external_product_id": context["product_id"],
                        "mapping_version": scope.mapping_version,
                    }
                )
                if current.status == "resolved" and (
                    not query
                    or set(product_query(query).split())
                    <= set(product_query(" ".join(current.items[0].names)).split())
                ):
                    selected_id = current.items[0].external_product_id
            if invalid_number:
                owner.operational_clarification = context
                reply = "Choose a product number from the offered variants."
            elif not query and not selected_id and not choices:
                reply = "Which catalogue product and variant do you mean?"
            else:
                if selected_id:
                    result = lookup(
                        {
                            "external_product_id": selected_id,
                            "mapping_version": scope.mapping_version,
                        }
                    )
                elif query:
                    result = lookup({"query": query, "limit": 50})
                    if choices and not context.get("product_id") and not identifier:
                        narrowed = tuple(
                            p for p in result.items if p.external_product_id in choices
                        )
                        result = result.model_copy(
                            update={
                                "items": narrowed,
                                "status": "not_found"
                                if not narrowed
                                else "resolved"
                                if len(narrowed) == 1
                                else "ambiguous",
                            }
                        )
                    result = exact_product(result, query)
                else:
                    items = []
                    for choice in choices:
                        checked = lookup(
                            {
                                "external_product_id": choice,
                                "mapping_version": scope.mapping_version,
                            }
                        )
                        items.extend(checked.items)
                    result = CatalogueResult(
                        items=tuple(items),
                        truncated=False,
                        mapping_version=scope.mapping_version,
                        status="not_found"
                        if not items
                        else "resolved"
                        if len(items) == 1
                        else "ambiguous",
                    )
                state = {
                    "operation": "hilaleyih_test",
                    "capability": operation,
                    "source_id": SOURCE_ID,
                    "user_id": str(user.id),
                    "expires_at": (
                        datetime.now(UTC) + timedelta(minutes=30)
                    ).isoformat(),
                    "mapping_version": scope.mapping_version,
                    "schema_fingerprint": scope.schema_fingerprint,
                    "source_fingerprint": scope.source_fingerprint,
                    "candidates": [p.external_product_id for p in result.items],
                    "candidate_names": {
                        p.external_product_id: list(p.names) for p in result.items
                    },
                }
                if result.status == "not_found":
                    if choices and not identifier and not context.get("product_id"):
                        owner.operational_clarification = context
                    reply = (
                        "This barcode/identifier was not found in the authorized "
                        "valid catalogue. Please check it; I cannot invent a product."
                        if identifier
                        else "No matching valid catalogue product was found. "
                        "Please give an offered name, number, or barcode."
                    )
                elif result.status == "ambiguous":
                    owner.operational_clarification = state
                    reply = (
                        "Which catalogue variant do you mean?\n"
                        + "\n".join(
                            f"{i}. {' / '.join(p.names)} "
                            f"(identifier {p.external_product_id})"
                            for i, p in enumerate(result.items, 1)
                        )
                        + (
                            "\nMore matches exist; specify the size or barcode."
                            if result.truncated
                            else ""
                        )
                    )
                else:
                    product = result.items[0]
                    state["product_id"] = product.external_product_id
                    owner.operational_clarification = state
                    name = " / ".join(product.names)
                    if operation == "inventory":
                        evidence = HilaleyihHistoricalReader(
                            connector_for(profiles, source), settings
                        ).inventory(product.external_product_id)
                        reply = render_inventory(name, evidence)
                    elif operation == "product":
                        reply = (
                            f"Catalogue product: {name}; "
                            f"identifier {product.external_product_id}."
                        )
                    else:
                        row = HilaleyihHistoricalReader(
                            connector_for(profiles, source), settings
                        ).price(product.external_product_id)
                        amount = row["Selling1"] if row else None
                        if (
                            amount is not None
                            and Decimal(str(amount)).is_finite()
                            and Decimal(str(amount)) >= 0
                        ):
                            reply = (
                                f"Hilaleyih test mode ? {name}: historical Selling1 "
                                f"source amount {amount}; "
                                f"source currency code {row['ItemCurr']}; "
                                f"source unit {row['Unit'] or 'missing'}. "
                                "Tier precedence, currency pairing, "
                                "unit/pack basis, tax, "
                                "discounts and effective date are unverified. "
                                "This is not a verified current checkout price."
                            )
                        else:
                            estimate = estimated_price(product.external_product_id)
                            reply = (
                                f"Hilaleyih test mode ? {name}: DEMO price estimate "
                                f"{estimate:.2f} synthetic demo currency units "
                                "per test item. Deterministic simulated "
                                "amount; not a verified current checkout price."
                            )
    except (SourceMappingError, ToolExecutionError) as error:
        audit.status = ToolCallStatus.ERROR
        audit.error_code = "source_unavailable"
        reply = (
            "The Hilaleyih test read could not run under its current source scope "
            "or approval. A fresh authorized validation/read is required "
            f"({error.code})."
        )
    if (
        context
        and owner.operational_clarification is None
        and operation
        in {
            "catalogue",
            "sales",
            "financial",
        }
    ):
        owner.operational_clarification = context
    audit.latency_ms = int((time.monotonic() - started) * 1000)
    session.add(audit)
    return OwnerChatResult(reply=reply)
