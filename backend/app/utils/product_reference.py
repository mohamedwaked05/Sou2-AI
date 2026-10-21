"""Conservative product-reference syntax, independent of brands and tenants."""

import re


def search_terms(value: str) -> tuple[str, ...]:
    # A digit followed by a separated word is one identifier/unit token (7 UP,
    # 330 ml). Words otherwise remain independent required terms.
    value = re.sub(r"(\d)\s+(?=[A-Za-z])", r"\1", value.strip())
    return tuple(dict.fromkeys(value.casefold().split()))


_REQUEST_WORDS = frozenset(
    "please show list give get me us all the a an of for from products "
    "product choose select find search matching s can could would one "
    "items item variants have we do you i want need about tell details detail "
    "barcode barcodes id ids sku skus code codes price prices stock inventory "
    "quantity quantities available availability how many what is are it its "
    "sales sale sold selling revenue revenues gross net refunds refund returned "
    "returns restock restocking reorder best sellers bestselling today yesterday "
    "tomorrow week month year last previous current date day days weeks months "
    "years since until between total totals count transaction transactions was "
    "were did does had tax discounts "
    "مبيعات مباع بيع إيرادات ايرادات مردود مرتجع مرتجعات اليوم أمس امس "
    "اسبوع أسبوع شهر سنة الماضي الحالية الحالي منه تبعه قديش كم شو "
    "اعرض اعطيني كل المنتجات منتج عندكم عندنا سعر باركود".split()
)
_REFERENTIAL = frozenset(
    "next more continue previous page those these them that this it its their "
    "yes no ok okay eh ah cancel stop thanks thank you hello hi hey تمام اي "
    "التالي المزيد كمل هؤلاء هذه هذا نعم لا شكرا مرحبا".split()
)


def reference_words(value: str) -> frozenset[str]:
    return frozenset(
        word
        for word in re.findall(r"[^\W_]+", value.casefold())
        if word not in _REQUEST_WORDS and word not in _REFERENTIAL
    )


def changes_product_reference(current: str, previous: str) -> bool:
    """Explicit changed words invalidate context; pronouns/commands do not select."""
    words = reference_words(current)
    if not words:
        return False
    old = reference_words(previous)
    # Do not reinterpret an exact identifier/ordinal-only reply as a new name.
    if all(word.isdecimal() for word in words):
        return bool(
            re.search(
                r"\b(?:id|ids|sku|skus|barcode|code|product|item)\b", current, re.I
            )
        )
    return words != old


def changed_candidate_reference(message, pending):
    words = reference_words(message)
    if any(
        words
        == reference_words(" ".join(getattr(c, "names", (getattr(c, "name", ""),))))
        for c in pending.candidates
    ):
        return False
    return changes_product_reference(message, pending.owner_request or "")


def product_reference_query(message: str) -> str:
    return " ".join(
        word
        for word in re.findall(r"[^\W_]+", message.casefold())
        if word not in _REQUEST_WORDS and word not in _REFERENTIAL
    )
