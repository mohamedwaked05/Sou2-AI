"""Owner-visible privacy-safe AI usage responses."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class CurrentAIUsageResponse(BaseModel):
    window_start: datetime
    window_end: datetime
    reset_at: datetime
    daily_token_allowance: int
    owner_reserved_tokens: int
    input_tokens_used: int
    output_tokens_used: int
    total_tokens_used: int
    tokens_currently_reserved: int
    tokens_remaining: int
    usage_percentage: float
    status: Literal["normal", "approaching_limit", "nearly_exhausted", "exhausted"]
