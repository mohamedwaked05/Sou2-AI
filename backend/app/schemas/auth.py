"""Validated authentication request and safe response schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.security import normalize_email


class MessageResponse(BaseModel):
    message: str


class EmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def normalize_and_validate_email(cls, value: str) -> str:
        normalized = normalize_email(value)
        local, separator, domain = normalized.partition("@")
        if not separator or not local or "." not in domain or domain.startswith("."):
            raise ValueError("Enter a valid email address.")
        return normalized


class RegistrationRequest(EmailRequest):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=512)
    password_confirmation: str = Field(min_length=1, max_length=512)

    @field_validator("first_name", "last_name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("Name cannot be blank.")
        return clean

    @model_validator(mode="after")
    def passwords_match(self) -> RegistrationRequest:
        if self.password != self.password_confirmation:
            raise ValueError("Password confirmation does not match.")
        return self


class TokenRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class LoginRequest(EmailRequest):
    password: str = Field(min_length=1, max_length=128)
    keep_me_signed_in: bool = False


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


class CurrentUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    first_name: str
    last_name: str
    email_verified_at: datetime | None
    status: str


class PasswordResetRequest(TokenRequest):
    password: str = Field(min_length=1, max_length=512)
    password_confirmation: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def passwords_match(self) -> PasswordResetRequest:
        if self.password != self.password_confirmation:
            raise ValueError("Password confirmation does not match.")
        return self


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=512)
    new_password_confirmation: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def passwords_match(self) -> PasswordChangeRequest:
        if self.new_password != self.new_password_confirmation:
            raise ValueError("Password confirmation does not match.")
        return self
