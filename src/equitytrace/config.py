"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
import warnings
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Preferred EQUITYTRACE_* names with temporary FILINGEDGE_* fallbacks.
_ENV_ALIASES: tuple[tuple[str, str, str], ...] = (
    # (field, new_env, legacy_env)
    ("sec_email", "EQUITYTRACE_SEC_EMAIL", "FILINGEDGE_SEC_EMAIL"),
    ("sec_organization", "EQUITYTRACE_SEC_ORGANIZATION", "FILINGEDGE_SEC_ORGANIZATION"),
    ("database_path", "EQUITYTRACE_DATABASE_PATH", "FILINGEDGE_DATABASE_PATH"),
    ("cache_dir", "EQUITYTRACE_CACHE_DIR", "FILINGEDGE_CACHE_DIR"),
    (
        "http_timeout_seconds",
        "EQUITYTRACE_HTTP_TIMEOUT_SECONDS",
        "FILINGEDGE_HTTP_TIMEOUT_SECONDS",
    ),
    (
        "max_requests_per_second",
        "EQUITYTRACE_MAX_REQUESTS_PER_SECOND",
        "FILINGEDGE_MAX_REQUESTS_PER_SECOND",
    ),
    ("enable_cache", "EQUITYTRACE_ENABLE_CACHE", "FILINGEDGE_ENABLE_CACHE"),
)


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class LegacyEnvVarWarning(UserWarning):
    """Warned when a deprecated FILINGEDGE_* environment variable is used."""


def _key_present(key: str, dotenv_vals: Mapping[str, str | None]) -> bool:
    """Return True when *key* is set in the process env or dotenv file."""
    if key in os.environ:
        return True
    return key in dotenv_vals and dotenv_vals.get(key) is not None


def emit_legacy_env_warnings(*, env_file: Path | None = Path(".env")) -> list[str]:
    """
    Emit deprecation warnings for FILINGEDGE_* variables still in use.

    Warnings never include variable values. Returns the list of legacy keys warned.
    """
    dotenv_vals: dict[str, str | None] = {}
    if env_file is not None and env_file.is_file():
        dotenv_vals = dict(dotenv_values(env_file))

    warned: list[str] = []
    for _field, new_key, old_key in _ENV_ALIASES:
        if _key_present(old_key, dotenv_vals) and not _key_present(new_key, dotenv_vals):
            warnings.warn(
                f"{old_key} is deprecated; use {new_key} instead. "
                "The FILINGEDGE_* prefix will be removed in a future release.",
                LegacyEnvVarWarning,
                stacklevel=2,
            )
            warned.append(old_key)
    return warned


class Settings(BaseSettings):
    """EquityTrace runtime settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    sec_email: str = Field(
        default="",
        description="Contact email for SEC User-Agent",
        validation_alias=AliasChoices("EQUITYTRACE_SEC_EMAIL", "FILINGEDGE_SEC_EMAIL"),
    )
    sec_organization: str = Field(
        default="EquityTrace Development",
        description="Organization name",
        validation_alias=AliasChoices(
            "EQUITYTRACE_SEC_ORGANIZATION",
            "FILINGEDGE_SEC_ORGANIZATION",
        ),
    )
    database_path: Path = Field(
        default=Path("data/equitytrace.duckdb"),
        validation_alias=AliasChoices(
            "EQUITYTRACE_DATABASE_PATH",
            "FILINGEDGE_DATABASE_PATH",
        ),
    )
    cache_dir: Path = Field(
        default=Path("data/cache"),
        validation_alias=AliasChoices("EQUITYTRACE_CACHE_DIR", "FILINGEDGE_CACHE_DIR"),
    )
    http_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        validation_alias=AliasChoices(
            "EQUITYTRACE_HTTP_TIMEOUT_SECONDS",
            "FILINGEDGE_HTTP_TIMEOUT_SECONDS",
        ),
    )
    max_requests_per_second: float = Field(
        default=8.0,
        gt=0,
        lt=10,
        validation_alias=AliasChoices(
            "EQUITYTRACE_MAX_REQUESTS_PER_SECOND",
            "FILINGEDGE_MAX_REQUESTS_PER_SECOND",
        ),
    )
    enable_cache: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "EQUITYTRACE_ENABLE_CACHE",
            "FILINGEDGE_ENABLE_CACHE",
        ),
    )

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
                "EQUITYTRACE_SEC_EMAIL is required for live SEC requests. "
                "Copy .env.example to .env and set your contact email. "
                "(Legacy FILINGEDGE_SEC_EMAIL is still accepted temporarily.) "
                "SEC fair-access policy requires an identifying User-Agent."
            )
        return self.sec_email

    def user_agent(self) -> str:
        """Build a descriptive SEC User-Agent string."""
        from equitytrace import __version__

        email = self.require_sec_email()
        org = self.sec_organization.strip() or "EquityTrace Development"
        return f"{org} EquityTrace/{__version__} ({email})"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings (emits legacy-env warnings once per process)."""
    emit_legacy_env_warnings()
    return Settings()


def clear_settings_cache() -> None:
    """Clear the settings cache (useful in tests)."""
    get_settings.cache_clear()
