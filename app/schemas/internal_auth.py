from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl, field_validator

GOOGLE_IDENTITY_PROVIDER = "google"
CANONICAL_GOOGLE_ISSUER = "https://accounts.google.com"
ACCEPTED_GOOGLE_ISSUERS = {
    CANONICAL_GOOGLE_ISSUER,
    "accounts.google.com",
}


class ResolveInternalUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_provider: str = Field(min_length=1, max_length=32)
    provider_issuer: str = Field(min_length=1, max_length=255)
    provider_subject: str = Field(min_length=1, max_length=255)
    email: EmailStr
    email_verified: bool
    display_name: str | None = Field(default=None, max_length=255)
    avatar_url: HttpUrl | None = Field(default=None)

    @field_validator("identity_provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized != GOOGLE_IDENTITY_PROVIDER:
            raise ValueError("Unsupported identity provider")
        return normalized

    @field_validator("provider_issuer")
    @classmethod
    def validate_issuer(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in ACCEPTED_GOOGLE_ISSUERS:
            raise ValueError("Unsupported provider issuer")
        return CANONICAL_GOOGLE_ISSUER

    @field_validator("provider_subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("provider_subject is required")
        return normalized

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("avatar_url")
    @classmethod
    def normalize_avatar_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        return value

    @field_validator("email_verified")
    @classmethod
    def validate_email_verified(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("email_verified must be true")
        return value


class ResolveInternalUserResponse(BaseModel):
    user_id: str
