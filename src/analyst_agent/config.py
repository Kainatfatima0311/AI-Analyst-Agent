"""Application settings.

Every value comes from the environment, or from ``.env`` in development. Nothing is
hard-coded and no credential has a usable default (control C9).

The two database DSNs are deliberately separate fields rather than one connection string with
a role switch. ``db_ro_dsn`` is the only one the tool layer is ever handed; ``db_rw_dsn`` stays
with the service for its own state. Keeping them apart in the type system means a mistake in
wiring is a visible mistake rather than a silent privilege escalation.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]
Provider = Literal["anthropic", "groq"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Model provider ----------------------------------------------------
    llm_provider: Provider = "anthropic"
    """Which backend answers a node's call. See agent/llm_groq.py for what Groq lacks."""

    # --- Anthropic ---------------------------------------------------------
    anthropic_api_key: SecretStr | None = None
    analyst_model: str = "claude-opus-5"
    """Model id. Never date-suffixed — the plain id is complete as-is."""

    # Per-node effort tiers. Cheap classification stays low; the reasoning the project is
    # actually judged on runs at xhigh. See docs/design-document.md section 5.
    effort_classify: Effort = "low"
    effort_author: Effort = "high"
    effort_reason: Effort = "xhigh"

    max_tokens_nonstreaming: int = 16_000
    max_tokens_streaming: int = 64_000

    # --- Groq (OpenAI-compatible chat completions) --------------------------
    groq_api_key: SecretStr | None = None
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com"
    """Host only. The SDK appends /openai/v1 itself — including it here double-prefixes."""
    groq_max_tokens: int = 4_096
    """Output cap for this provider, well below the Anthropic one.

    Groq counts ``max_completion_tokens`` against the per-minute token allowance *before*
    generating, so a 16k request is refused outright on a free tier with an 8k limit even when
    the answer would have been three lines.
    """

    groq_temperature: float = 0.2
    """Low, not zero: SQL authoring wants determinism, hypothesis generation wants variety."""

    groq_reasoning_effort: bool = False
    """Send the effort tier as ``reasoning_effort``.

    Off by default because a model that does not support the parameter rejects the whole
    request rather than ignoring it. Turn it on for a reasoning model.
    """

    # --- Database ----------------------------------------------------------
    db_rw_dsn: SecretStr = Field(description="Service state only. Never handed to the tool layer.")
    db_ro_dsn: SecretStr = Field(description="Read-only analyst role. All generated SQL runs here.")
    db_pool_min: int = 1
    db_pool_max: int = 8

    # --- SQL safety (controls C2, C3, C5) ----------------------------------
    # NoDecode: pydantic-settings would otherwise try to JSON-parse this from .env before
    # the validator runs, so ALLOWED_SCHEMAS=analytics,marts would raise instead of split.
    allowed_schemas: Annotated[tuple[str, ...], NoDecode] = ("analytics",)
    sql_statement_timeout_ms: int = 15_000
    sql_idle_tx_timeout_ms: int = 30_000
    sql_default_row_limit: int = 5_000
    sql_max_row_limit: int = 50_000
    sql_max_explain_cost: float = 5_000_000.0

    # --- Agent budgets (control C7) ---------------------------------------
    max_queries_per_run: int = 25
    max_hypotheses_per_finding: int = 4
    max_agent_iterations: int = 20
    max_run_wall_clock_seconds: int = 600
    max_tokens_per_run: int = 400_000
    approval_timeout_seconds: int = 1_800

    # --- Service -----------------------------------------------------------
    api_host: str = "0.0.0.0"  # noqa: S104 — bound inside a container, published by compose
    api_port: int = 8000
    api_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    environment: Literal["dev", "ci", "prod"] = "dev"

    @field_validator("allowed_schemas", mode="before")
    @classmethod
    def _split_schemas(cls, value: object) -> object:
        """Accept ALLOWED_SCHEMAS=analytics,marts as well as a real sequence."""
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("anthropic_api_key", "groq_api_key", mode="before")
    @classmethod
    def _blank_key_is_absent(cls, value: object) -> object:
        """A blank key in .env must read as absent, not as a configured empty key.

        Without this, the placeholder line committed in .env.example makes the service look
        configured and the failure surfaces much later, at the first model call.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        level = value.upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return level

    def require_api_key(self) -> str:
        """Fail loudly and early rather than at the first model call."""
        if self.anthropic_api_key is None or not self.anthropic_api_key.get_secret_value().strip():
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. It is required from Step 7 onward "
                "(the agent nodes); Steps 0-6 run without it."
            )
        return self.anthropic_api_key.get_secret_value()

    def require_groq_key(self) -> str:
        """Fail loudly and early rather than at the first model call."""
        if self.groq_api_key is None or not self.groq_api_key.get_secret_value().strip():
            raise RuntimeError(
                "GROQ_API_KEY is not set, and LLM_PROVIDER=groq. Set it in .env, or switch "
                "LLM_PROVIDER back to anthropic."
            )
        return self.groq_api_key.get_secret_value()

    def require_provider_key(self) -> str:
        """The key for whichever provider is configured."""
        if self.llm_provider == "groq":
            return self.require_groq_key()
        return self.require_api_key()

    @property
    def analyst_model_id(self) -> str:
        """The model id actually in use, for logging and for the run row."""
        return self.groq_model if self.llm_provider == "groq" else self.analyst_model

    @property
    def secret_values(self) -> tuple[str, ...]:
        """Every secret string, for the log redactor to scrub. See observability/logging.py."""
        values: list[str] = []
        for secret in (
            self.anthropic_api_key,
            self.groq_api_key,
            self.db_rw_dsn,
            self.db_ro_dsn,
        ):
            if secret is None:
                continue
            raw = secret.get_secret_value()
            values.append(raw)
            # Also register the bare password out of each DSN, so a log line that
            # interpolated only the password is still caught.
            if "://" in raw and "@" in raw:
                credentials = raw.split("://", 1)[1].rsplit("@", 1)[0]
                if ":" in credentials:
                    password = credentials.split(":", 1)[1]
                    if password:
                        values.append(password)
        return tuple(dict.fromkeys(v for v in values if len(v) >= 4))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. Call ``get_settings.cache_clear()`` in tests that change the env."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
