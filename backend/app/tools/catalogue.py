"""Bounded catalogue operation for an approved, engine-neutral query plan."""

from app.integrations.mapped_products import MappedProductSource
from app.schemas.source_mapping import CatalogueRequest, CatalogueResult


def execute_product_search(
    source: MappedProductSource, request: CatalogueRequest
) -> CatalogueResult:
    """The mapping has one allowlisted capability and no inventory calculation."""
    return source.search(request)
