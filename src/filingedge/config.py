"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class Settings(BaseSettings):
    """FilingEdge runtime settings."""

    model_config = SettingsConfigDict(
        env_prefix="FILINGEDGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    sec_email: str = Field(default="", description="Contact email for SEC User-Agent")
    sec_organization: str = Field(default="FilingEdge", description="Organization name")
    database_path: Path = Field(default=Path("data/filingedge.duckdb"))
    cache_dir: Path = Field(default=Path("data/cache"))
    http_timeout_seconds: float = Field(default=30.0, gt=0)
    max_requests_per_second: float = Field(default=8.0, gt=0, lt=10)
    enable_cache: bool = Field(default=True)

    @field_validator("sec_email", mode="before")
    @classmethod
    def _strip_email(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("database_path", "cache_dir", mode="before")
    @classmethod
    def _coerce_path(cls, value: object) -> object:
        if isinstance(value, str) and value.strip():
            return Path(value.strip())
        return value

    def require_sec_email(self) -> str:
        """Return the SEC contact email or raise a clear configuration error."""
        if not self.sec_email:
            raise ConfigurationError(
                "FILINGEDGE_SEC_EMAIL is required for live SEC requests. "
                "Copy .env.example to .env and set your contact email. "
                "SEC fair-access policy requires an identifying User-Agent."
            )
        return self.sec_email

    def user_agent(self) -> str:
        """Build a descriptive SEC User-Agent string."""
        email = self.require_sec_email()
        org = self.sec_organization.strip() or "FilingEdge"
        return f"{org} research bot ({email})"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()


def clear_settings_cache() -> None:
    """Clear the settings cache (useful in tests)."""
    get_settings.cache_clear()
