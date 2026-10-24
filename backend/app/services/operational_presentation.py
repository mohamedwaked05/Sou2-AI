"""Shared factual presentation. Language models do not rewrite source facts."""

import re
from collections.abc import Callable, Sequence
from datetime import timedelta
from zoneinfo import ZoneInfo

from app.schemas.operational import (
    BestSellingProductsResult,
    CategoryResolution,
    InventoryResult,
    LocationResolution,
    MetricCapabilityResult,
    Product,
    ProductDiscoveryResult,
    ProductResolution,
    ProductResolutionCandidate,
    RestockingRecommendationsResult,
    SalesSummary,
)
from app.schemas.operational_mapping import OperationalReport
from app.schemas.operational_presentation import (
    OperationalPresentation,
    PresentationProvenance,
    PresentationResult,
)
from app.schemas.owner_chat_state import ProductPresentation
from app.schemas.source_mapping import CatalogueProduct, CatalogueResult

SafeField = Callable[[str | None, str], str]
ProductRecord = Product | ProductResolutionCandidate | CatalogueProduct
MAX_RESPONSE_CHARS = 14_000


def build_presentation(
    result: PresentationResult,
    *,
    purpose: ProductPresentation | None = None,
    identifiers: tuple[str, ...] = (),
    language: str = "english",
    provenance: PresentationProvenance | None = None,
    source_records: ProductDiscoveryResult | CatalogueResult | None = None,
    operation: str | None = None,
) -> OperationalPresentation:
    capabilities = {
        OperationalReport: "mapped_operations",
        CatalogueResult: "products",
        InventoryResult: "inventory",
        SalesSummary: "sales",
        BestSellingProductsResult: "best_sellers",
        RestockingRecommendationsResult: "restocking",
        MetricCapabilityResult: "financial_metric",
        ProductResolution: "product_choice",
        CategoryResolution: "category_choice",
        LocationResolution: "location_choice",
    }
    metadata = getattr(result, "metadata", None)
    ambiguous = getattr(result, "status", None) == "ambiguous" or any(
        getattr(getattr(result, attr, None), "status", None) == "ambiguous"
        for attr in ("resolution", "category_resolution")
    )
    return OperationalPresentation(
        capability=capabilities[type(result)],
        operation=operation,
        presentation_kind=(
            "clarification"
            if ambiguous and not (purpose and purpose.intent == "discovery")
            else "detail"
            if purpose and purpose.intent in {"details", "selection"}
            else "report"
            if isinstance(
                result,
                (SalesSummary, BestSellingProductsResult, MetricCapabilityResult),
            )
            else "list"
        ),
        result=result,
        purpose=purpose,
        identifiers=identifiers,
        language="arabic"
        if language in {"arabic", "lebanese_arabic", "mixed"}
        else "english",
        provenance=provenance or PresentationProvenance(),
        source_records=source_records,
        unknown_fields=("stock", "price")
        if isinstance(result, CatalogueResult)
        else ("quantity_unit", "price")
        if isinstance(result, (InventoryResult, RestockingRecommendationsResult))
        else (),
        unsupported_fields=tuple(result.missing_inputs)
        if isinstance(result, MetricCapabilityResult)
        else (),
        ambiguous=ambiguous,
        pagination_available=bool(getattr(result, "continuation", None)),
        truncated=getattr(result, "truncated", False)
        or bool(metadata and metadata.is_truncated),
    )


def safe_text(value: str | None, fallback: str, safe: SafeField) -> str:
    text = " ".join(safe(value, fallback).split())
    # Source labels are text, never markdown instructions, HTML or links.
    return re.sub(r"([\\`*\[\]<>|])", r"\\\1", text)


def product_labels(
    products: Sequence[ProductRecord],
    identifiers: tuple[str, ...],
    safe: SafeField,
    *,
    arabic: bool = False,
    categories: bool = False,
) -> list[str]:
    unknown = "غير متاح" if arabic else "unavailable"
    names = []
    for product in products:
        raw = (
            product.names if isinstance(product, CatalogueProduct) else (product.name,)
        )
        unique: dict[str, str] = {}
        for name in raw:
            if name.strip():
                label = safe_text(name, "منتج" if arabic else "Product", safe)
                unique.setdefault(label.casefold(), label)
        names.append(" / ".join(unique.values()) or ("منتج" if arabic else "Product"))
    labels = []
    for product, name in zip(products, names, strict=True):
        collisions = [
            p
            for p, n in zip(products, names, strict=True)
            if n.casefold() == name.casefold()
        ]
        distinct = {p.external_product_id for p in collisions}
        sku = safe_text(product.sku, unknown, safe)
        unique_sku = (
            product.sku
            and sku != unknown
            and len(
                {
                    p.external_product_id
                    for p in collisions
                    if safe_text(p.sku, unknown, safe).casefold() == sku.casefold()
                }
            )
            == 1
        )
        show_id = "id" in identifiers or (
            len(distinct) > 1 and not ("sku" in identifiers and unique_sku)
        )
        label = name
        if show_id:
            label += " — ID: " + safe_text(product.external_product_id, unknown, safe)
        if "sku" in identifiers:
            label += " — SKU: " + sku
        raw_categories = (
            product.categories
            if isinstance(product, CatalogueProduct)
            else (getattr(product, "category", None),)
        )
        if categories:
            useful = dict.fromkeys(
                safe_text(c, "", safe)
                for c in raw_categories
                if c and not re.fullmatch(r"(?:family|category|group)\s*\d+", c, re.I)
            )
            useful = [c for c in useful if c and c.casefold() != name.casefold()]
            if useful:
                label += (
                    " — " + ("الفئة: " if arabic else "Category: ") + ", ".join(useful)
                )
        labels.append(label)
    return labels


def _bounded(
    header: str, rows: list[str], footer: list[str], truncated: bool, arabic: bool
) -> str:
    lines = [header]
    shown = 0
    # Reserve space for factual caveats and the truncation indicator.
    for index, row in enumerate(rows, 1):
        line = f"{index}. {row}"
        if sum(len(s) + 1 for s in lines) + len(line) > 12_000:
            break
        lines.append(line)
        shown += 1
    if truncated or shown < len(rows):
        footer.append(
            "النتائج محدودة؛ ضيّق البحث لعرض المزيد."
            if arabic
            else "Results are truncated; narrow the search for more matches."
        )
    return "\n".join([*lines, *footer])


def render_choices(labels: list[str], kind: str, language: str = "english") -> str:
    ar = language in {"arabic", "lebanese_arabic", "mixed"}
    headings = {
        "product": ("Which product do you mean?", "أي منتج تقصد؟"),
        "category": ("Which category do you mean?", "أي فئة تقصد؟"),
        "location": ("Which source location do you mean?", "أي موقع في المصدر تقصد؟"),
    }
    return _bounded(headings[kind][int(ar)], labels, [], False, ar)


def render_capability_gap(kind: str, language: str) -> str:
    """Facts about missing connected capabilities, independent of business identity."""
    ar = language in {"arabic", "lebanese_arabic", "mixed"}
    replies = {
        "products_only": (
            "The connected catalogue provides product details only. "
            "Stock, prices, inventory and sales are unknown.",
            "الكتالوج المتصل يوفّر تفاصيل المنتجات فقط. "
            "الكميات والأسعار والمخزون والمبيعات غير معروفة.",
        ),
        "current_price": (
            "The connected source does not provide current product prices. "
            "Historical sale prices cannot establish a current price.",
            "المصدر المتصل لا يوفّر أسعار المنتجات الحالية. "
            "أسعار المبيعات السابقة لا تؤكد السعر الحالي.",
        ),
        "product_reference": (
            "Specify a product name or identifier to search the catalogue. "
            "Stock and prices are unknown.",
            "حدّد اسم منتج أو معرّفه للبحث في الكتالوج. المخزون والأسعار غير معروفة.",
        ),
    }
    return replies[kind][int(ar)]


def render_operational(view: OperationalPresentation, safe: SafeField) -> str:
    output = view.result
    ar = view.language == "arabic"

    def text(en: str, arabic: str) -> str:
        return arabic if ar else en

    ids = view.purpose.identifiers if view.purpose else view.identifiers
    if isinstance(output, OperationalReport):
        if output.status in {"unsupported", "not_found"} and not (
            output.capability == "sales"
            and output.reason == "no_records_in_requested_period"
        ):
            return text("No approved result: ", "لا توجد نتيجة معتمدة: ") + safe_text(
                output.reason or output.status, "unavailable", safe
            )
        rows = []
        fields = (
            "branch",
            "warehouse",
            "on_hand",
            "reserved",
            "available",
            "price",
            "gross_quantity",
            "returned_quantity",
            "net_quantity",
            "transaction_count",
            "gross_revenue",
            "net_revenue",
            "refunds",
            "reorder_point",
            "target_quantity",
            "recommended_quantity",
            "tax_basis",
            "price_list",
            "observed_at",
        )
        for row in output.rows:
            labels = []
            if row.product_id is not None:
                labels.append("ID: " + safe_text(row.product_id, "unknown", safe))
            if row.unit is not None:
                labels.append(safe_text(row.unit, "unknown unit", safe))
            if row.location:
                labels.append("Location: " + safe_text(row.location, "unknown", safe))
            if row.currency:
                labels.append(safe_text(row.currency, "unknown currency", safe))
            labels.extend(
                name + ": " + safe_text(str(getattr(row, name)), "unknown", safe)
                for name in fields
                if getattr(row, name) is not None
            )
            rows.append(" | ".join(labels))
        scope = []
        if output.source_first_date and output.source_last_date:
            scope.append(
                text("Recorded source dates: ", "تواريخ السجلات في المصدر: ")
                + f"{output.source_first_date.isoformat()} — "
                + output.source_last_date.isoformat()
            )
            if output.start and output.end:
                zone = ZoneInfo(output.timezone)
                first = output.start.astimezone(zone).date()
                last = (output.end.astimezone(zone) - timedelta(microseconds=1)).date()
                if first < output.source_first_date or last > output.source_last_date:
                    scope.append(
                        text(
                            "The requested period extends beyond the recorded source "
                            "dates. This dataset cannot establish business activity "
                            "outside that range.",
                            "الفترة المطلوبة تتجاوز تواريخ السجلات المتاحة. "
                            "لا تثبت هذه البيانات نشاط المتجر خارج هذا النطاق.",
                        )
                    )
        if output.reason == "no_records_in_requested_period":
            scope.append(
                text(
                    "No matching documents are recorded in this dataset for the "
                    "requested period. This does not establish zero actual sales.",
                    "لا توجد مستندات مطابقة مسجلة في هذه البيانات للفترة المطلوبة. "
                    "هذا لا يعني أن مبيعات المتجر الفعلية كانت صفراً.",
                )
            )
        for total in output.totals:
            values = total.model_dump(exclude_none=True)
            scope.append(
                "Total | "
                + " | ".join(
                    safe_text(name, "field", safe)
                    + ": "
                    + safe_text(str(value), "unknown", safe)
                    for name, value in values.items()
                )
            )
        if output.start and output.end:
            scope.append(
                f"[{output.start.isoformat()}, {output.end.isoformat()}) | "
                + safe_text(output.timezone, "unknown timezone", safe)
            )
        if output.as_of:
            scope.append("As of: " + output.as_of.isoformat())
        if output.revenue_basis:
            scope.append("Net basis: " + output.revenue_basis)
        if output.business_meaning:
            scope.append(
                text("Reviewed meaning: ", "المعنى المعتمد: ")
                + safe_text(output.business_meaning, "unavailable", safe)
            )
        return _bounded(
            text("Approved source report: ", "تقرير المصدر المعتمد: ")
            + output.capability,
            rows,
            scope
            + (
                [
                    text(
                        "Multiple prices require an explicit scope.",
                        "الأسعار المتعددة تحتاج نطاقاً محدداً.",
                    )
                ]
                if output.status == "ambiguous"
                else []
            ),
            output.truncated,
            ar,
        )
    if isinstance(output, CatalogueResult):
        coverage_note = ""
        caveat = (
            text(
                "Stock and prices are unknown.",
                "المخزون والأسعار غير معروفة.",
            )
            + coverage_note
        )
        if not output.items:
            if output.search_semantics == "semantic":
                return text(
                    "No matching catalogue products were verified in the retrieved "
                    "candidates. "
                    "Try a product type or brand. Stock and prices are unknown.",
                    "لم يتم التحقق من منتجات مطابقة ضمن نتائج الكتالوج المسترجعة. "
                    "حدد نوع المنتج أو العلامة التجارية. المخزون والأسعار غير معروفة.",
                )
            return (
                coverage_note.strip()
                + (" " if coverage_note else "")
                + (
                    text(
                        "No matching catalogue product was found. Stock and prices "
                        "are unknown. Try a narrower product type or brand.",
                        "لم يُعثر على منتج مطابق في الكتالوج. المخزون والأسعار "
                        "غير معروفة. "
                        "عدم التطابق الحرفي لا يثبت غياب فئة؛ استخدم تصنيفاً معتمداً.",
                    )
                    if not output.truncated
                    else text(
                        "Catalogue matches exceed the display limit; narrow the "
                        "search. Stock and prices are unknown.",
                        (
                            "تتجاوز النتائج حد العرض؛ ضيّق البحث. المخزون والأسعار "
                            "غير معروفة."
                        ),
                    )
                )
            )
        discovery = view.purpose is not None and view.purpose.intent == "discovery"
        if (
            view.purpose is not None
            and view.purpose.group_by == "brands"
            and output.brand_labels
        ):
            labels = list(
                dict.fromkeys(
                    safe_text(label, "", safe) for label in output.brand_labels
                )
            )
            return (
                text("Catalogue brands: ", "علامات الكتالوج: ")
                + ", ".join(labels)
                + ".\n"
                + text(
                    "Choose a brand to see its products.", "اختر علامة لعرض منتجاتها."
                )
                + (
                    text(" This is a bounded selection.", " هذه نتائج محدودة.")
                    if output.truncated
                    else ""
                )
            )
        labels = product_labels(
            output.items, ids, safe, arabic=ar, categories=not discovery
        )
        if (
            not discovery
            and output.status == "resolved"
            and len(labels) == 1
            and len(labels[0]) <= 12_000
        ):
            return (
                text("Catalogue product: ", "منتج الكتالوج: ")
                + labels[0]
                + ".\n"
                + caveat
            )
        return _bounded(
            text("Matching catalogue products:", "منتجات الكتالوج المطابقة:")
            if discovery
            else text(
                "Which catalogue product do you mean?", "أي منتج في الكتالوج تقصد؟"
            ),
            labels,
            [
                caveat,
                text(
                    "Reply next for the next bounded page.",
                    "أرسل التالي للصفحة التالية.",
                )
                if output.continuation
                else "",
            ],
            output.truncated,
            ar,
        )
    if isinstance(output, (InventoryResult, RestockingRecommendationsResult)):
        for resolution in (output.resolution, output.category_resolution):
            if resolution is not None and resolution.status != "resolved":
                return render_operational(
                    view.model_copy(update={"result": resolution}), safe
                )
        restocking = isinstance(output, RestockingRecommendationsResult)
        inventories = [i.inventory if restocking else i for i in output.items]
        labels = product_labels(
            [i.product for i in inventories], ids, safe, arabic=ar, categories=True
        )
        rows = []
        for item, inv, label in zip(output.items, inventories, labels, strict=True):
            location = safe_text(
                inv.branch_name or inv.warehouse_name,
                text("source location", "موقع المصدر"),
                safe,
            )
            if restocking:
                row = f"{label} — {location}: " + text(
                    f"restock {item.recommended_quantity}; "
                    f"{inv.available_quantity} available; target {inv.target_stock}; "
                    f"reorder point {inv.reorder_point}",
                    f"كمية إعادة التزويد {item.recommended_quantity}؛ "
                    f"المتاح {inv.available_quantity}؛ الهدف {inv.target_stock}؛ "
                    f"حد إعادة الطلب {inv.reorder_point}",
                )
            else:
                row = f"{label} — {location}: " + text(
                    f"{inv.available_quantity} available; "
                    f"{inv.on_hand_quantity} on hand; {inv.reserved_quantity} reserved",
                    f"المتاح {inv.available_quantity}؛ الموجود {inv.on_hand_quantity}؛ "
                    f"المحجوز {inv.reserved_quantity}",
                )
            rows.append(row)
        if not rows:
            return (
                text(
                    "No matching items require restocking under the source rules.",
                    "لا توجد أصناف مطابقة تحتاج إلى إعادة تزويد حسب قواعد المصدر.",
                )
                if restocking
                else text(
                    "No matching inventory rows were returned. This does not "
                    "establish zero stock.",
                    "لا توجد سجلات مخزون مطابقة. هذا لا يعني أن المخزون صفر.",
                )
            )
        footer = [
            text(
                "Quantities are source stock units; pack/case units were "
                "not supplied. Prices are unknown.",
                "الكميات بوحدات المخزون في المصدر؛ وحدات العبوة أو الصندوق "
                "غير محددة. الأسعار غير معروفة.",
            )
        ]
        return _bounded(
            text("Restocking recommendations:", "توصيات إعادة التزويد:")
            if restocking
            else text("Current inventory:", "المخزون الحالي:"),
            rows,
            footer,
            output.metadata.is_truncated,
            ar,
        )
    if isinstance(output, (ProductResolution, CategoryResolution, LocationResolution)):
        if output.status == "not_found":
            return text(
                "No matching source record was found.",
                "لم يُعثر على سجل مطابق في المصدر.",
            )
        if isinstance(output, ProductResolution):
            records = (
                output.candidates if output.status == "ambiguous" else (output.product,)
            )
            rows = product_labels(records, ids, safe, arabic=ar)
        else:
            records = (
                output.candidates
                if output.status == "ambiguous"
                else (getattr(output, "category", None) or output.location,)
            )
            rows = [
                safe_text(i.label, text("source record", "سجل المصدر"), safe)
                for i in records
            ]
        return _bounded(
            text("Matching source record:", "السجل المطابق في المصدر:")
            if output.status == "resolved"
            else text("Which product do you mean?", "أي منتج تقصد؟")
            if isinstance(output, ProductResolution)
            else text(
                "Which source category or location do you mean?",
                "أي فئة أو موقع في المصدر تقصد؟",
            ),
            rows,
            [],
            output.metadata.is_truncated,
            ar,
        )
    if isinstance(
        output, (SalesSummary, BestSellingProductsResult, MetricCapabilityResult)
    ):
        period = output.period
        period_text = (
            f"{period.start_date} – {period.end_date} "
            + text("(end excluded)", "(تاريخ النهاية غير مشمول)")
            + f", {period.source_timezone}"
        )
        if output.branch_external_id:
            period_text += text(" — requested branch", " — الفرع المطلوب")
        if isinstance(output, MetricCapabilityResult):
            if output.status == "supported":
                return (
                    text(
                        "The requested metric is supported; "
                        "no calculated value was supplied.",
                        "المؤشر المطلوب مدعوم؛ لم تُقدّم قيمة محسوبة.",
                    )
                    + "\n"
                    + period_text
                )
            labels = {
                "cost_cogs": text("cost/COGS", "تكلفة البضاعة"),
                "expenses": text("expenses", "المصاريف"),
                "valuation_basis": text("valuation basis", "أساس التقييم"),
            }
            return (
                text(
                    "The requested metric is unsupported. Missing inputs: ",
                    "المؤشر المطلوب غير مدعوم. البيانات الناقصة: ",
                )
                + ", ".join(labels[i] for i in output.missing_inputs)
                + "\n"
                + text("Supported metrics: ", "المؤشرات المدعومة: ")
                + ", ".join(_metric(i, ar) for i in output.supported_metrics)
                + "\n"
                + period_text
            )
        if isinstance(output, BestSellingProductsResult):
            labels = product_labels(
                [i.product for i in output.items], ids, safe, arabic=ar
            )
            rows = [
                f"{label}: "
                + text(
                    f"rank {i.rank}; {i.quantity_sold} net units; "
                    f"{i.revenue} {i.currency} net revenue",
                    f"الترتيب {i.rank}؛ صافي الكمية {i.quantity_sold}؛ "
                    f"صافي الإيرادات {i.revenue} {i.currency}",
                )
                for i, label in zip(output.items, labels, strict=True)
            ]
            if not rows:
                rows = [text("No matching sales.", "لا توجد مبيعات مطابقة.")]
            return _bounded(
                text("Best sellers for ", "الأكثر مبيعاً للفترة ") + period_text + ":",
                rows,
                [],
                output.metadata.is_truncated,
                ar,
            )
        if output.metric == "revenue":
            rows = [
                text("Net revenue", "صافي الإيرادات")
                + f": {output.net_revenue} {output.currency}",
                text("Gross revenue", "إجمالي الإيرادات")
                + f": {output.gross_revenue} {output.currency}",
                text("Refunds", "المبالغ المستردة")
                + f": {output.refund_amount} {output.currency}",
            ]
        elif output.metric == "sales_count":
            rows = [
                text("Completed sales", "المبيعات المكتملة")
                + f": {output.completed_sale_count}",
                text("Sales with returns", "مبيعات مع مرتجعات")
                + f": {output.returned_sale_count}",
                text("Completed refunds", "عمليات الاسترداد المكتملة")
                + f": {output.completed_refund_count}",
            ]
        elif output.metric in {"gross_profit", "net_profit"}:
            rows = [
                _metric(output.metric, ar)
                + f": {getattr(output, output.metric)} {output.currency}"
            ]
        else:
            rows = [
                text(
                    "Inventory valuation is not supplied by this sales result.",
                    "نتيجة المبيعات لا تتضمن قيمة المخزون.",
                )
            ]
        return _bounded(
            text("Sales for ", "المبيعات للفترة ") + period_text + ":",
            rows,
            [],
            output.metadata.is_truncated,
            ar,
        )
    raise ValueError("Unsupported presentation result.")


def _metric(value: str, arabic: bool) -> str:
    labels = {
        "revenue": "الإيرادات",
        "sales_count": "عدد المبيعات",
        "gross_profit": "الربح الإجمالي",
        "net_profit": "الربح الصافي",
        "inventory_value": "قيمة المخزون",
    }
    return labels[value] if arabic else value.replace("_", " ")
