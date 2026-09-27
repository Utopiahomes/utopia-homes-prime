"""Environment-derived startup configuration. Fails closed: any malformed value raises
`ConfigError` rather than falling back to a default that could silently misconfigure auth,
idempotency, or the legacy upstream call.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

_ENV_PREFIX = "GUEST_ANSWER_PROVIDER_"

Environment = Literal["production", "preview"]
KeyStatus = Literal["active", "retired", "revoked"]

MINIMUM_RATE_LIMIT_PER_MINUTE: Final = 12
DEFAULT_IDEMPOTENCY_TTL_SECONDS: Final = 900
"""15 minutes. Architectural in RC2 (not pinned) — see docs/implementation-notes.md."""
DEFAULT_IDEMPOTENCY_IN_PROGRESS_CEILING_SECONDS: Final = 15
"""Legacy upstream timeout (10s) plus grace, before a stuck in_progress record is treated as
unresolved (covers a process crash mid-request)."""
HOMES_PRIME_IN_PROGRESS_CEILING_SECONDS: Final = 20
"""RC2 §6 15-second attempt plus grace for idempotency completion."""


class ConfigError(ValueError):
    """Raised when startup configuration is missing or malformed. Callers must fail closed."""


@dataclass(frozen=True, slots=True)
class JwtAllowlistedKey:
    """RC2 §7: per-key local binding of iss/sub/aud/env/capabilities — there is no global
    expected-claim set, each key carries its own."""

    kid: str
    public_key_pem: str
    environment: Environment
    issuer: str
    subject: str
    audience: str
    capabilities: tuple[str, ...]
    status: KeyStatus = "active"


_SITE_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
"""Ported verbatim from utopia-homes-web/lib/lucy/server.ts's siteHostname pattern."""


@dataclass(frozen=True, slots=True)
class LegacyUpstreamConfig:
    """Mirrors utopia-homes-web/lib/lucy/server.ts's resolvePublicLucyConfiguration() exactly —
    same env var names, same validation (including the loopback-only-outside-production HTTP
    exception) — so this provider calls the identical upstream the website already calls, with no
    new upstream config surface."""

    endpoint: str
    token: str
    site_hostname: str
    snapshot_digest: str

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str], *, provider_environment: str = "preview"
    ) -> LegacyUpstreamConfig:
        from urllib.parse import urlsplit

        if env.get("LUCY_PUBLIC_ENABLED") != "true":
            raise ConfigError("LUCY_PUBLIC_ENABLED must be 'true'")

        endpoint = (env.get("LUCY_PUBLIC_API_URL") or "").strip()
        token = (env.get("LUCY_PUBLIC_API_TOKEN") or "").strip()
        site_hostname = (env.get("LUCY_PUBLIC_SITE_HOSTNAME") or "").strip().lower()
        snapshot_digest = (env.get("LUCY_PUBLIC_SNAPSHOT_DIGEST") or "").strip()

        if not endpoint or not token or len(token) < 32 or not site_hostname or not snapshot_digest:
            raise ConfigError(
                "LUCY_PUBLIC_API_URL / LUCY_PUBLIC_API_TOKEN (>=32 chars) / "
                "LUCY_PUBLIC_SITE_HOSTNAME / LUCY_PUBLIC_SNAPSHOT_DIGEST are all required"
            )
        if not re.fullmatch(r"[a-f0-9]{64}", snapshot_digest):
            raise ConfigError("LUCY_PUBLIC_SNAPSHOT_DIGEST must be 64 lowercase hex characters")

        parsed = urlsplit(endpoint)
        if not parsed.hostname:
            raise ConfigError("LUCY_PUBLIC_API_URL is not a valid URL")
        loopback = parsed.hostname in ("127.0.0.1", "localhost")
        if parsed.scheme != "https" and not (provider_environment != "production" and loopback):
            raise ConfigError(
                "LUCY_PUBLIC_API_URL must be https:// (loopback http only outside production)"
            )
        if parsed.username or parsed.password:
            raise ConfigError("LUCY_PUBLIC_API_URL must not contain userinfo")
        if not _SITE_HOSTNAME_RE.fullmatch(site_hostname):
            raise ConfigError("LUCY_PUBLIC_SITE_HOSTNAME is not a valid DNS hostname")

        return cls(
            endpoint=endpoint,
            token=token,
            site_hostname=site_hostname,
            snapshot_digest=snapshot_digest,
        )


AnswerEngineName = Literal["legacy-bridge", "homes-prime"]

DEFAULT_GENERATE_PROFILE_ID: Final = "utopia-homes.public-answer.generate.v1"
DEFAULT_REVIEW_PROFILE_ID: Final = "utopia-homes.public-answer.support-review.v1"
DEFAULT_EXECUTION_SUBJECT: Final = "stoin:synth:utopia-homes-prime"
DEFAULT_APPROVED_HOSTNAMES: Final = ("www.utopiahomes.com",)


@dataclass(frozen=True, slots=True)
class ExecutionProfileConfig:
    profile_id: str
    ceiling_ms: int
    max_output_tokens: int
    max_cost_microusd: int


InferenceBackendName = Literal["direct-openrouter", "tiamat"]


def _direct_provider(
    require: Callable[[str], str], optional: Callable[[str, str], str], prefix: str
) -> DirectProviderConfig:
    api_key = require("OPENROUTER_API_KEY")
    if not 20 <= len(api_key) <= 2_000:
        raise ConfigError(f"{prefix}OPENROUTER_API_KEY is invalid")
    model = require("OPENROUTER_MODEL")
    if len(model) > 200 or model.startswith("~"):
        raise ConfigError(f"{prefix}OPENROUTER_MODEL must be an exact public model identifier")
    providers = tuple(
        item.strip() for item in optional("OPENROUTER_ALLOWED_PROVIDERS", "").split(",") if item
    )
    if any(not item or len(item) > 100 for item in providers):
        raise ConfigError(f"{prefix}OPENROUTER_ALLOWED_PROVIDERS is invalid")

    def price(name: str) -> float:
        try:
            value = float(require(name))
        except ValueError as exc:
            raise ConfigError(f"{prefix}{name} must be a number") from exc
        if not 0 < value < 1_000:
            raise ConfigError(f"{prefix}{name} must be a positive USD-per-million price")
        return value

    return DirectProviderConfig(
        api_key=api_key,
        model=model,
        allowed_providers=providers,
        # Price ceilings are deployment policy: required, never defaulted.
        max_prompt_usd_per_million=price("OPENROUTER_MAX_PROMPT_USD_PER_MILLION"),
        max_completion_usd_per_million=price("OPENROUTER_MAX_COMPLETION_USD_PER_MILLION"),
        referer=optional("OPENROUTER_REFERER", "https://www.utopiahomes.com"),
    )


@dataclass(frozen=True, slots=True)
class TiamatExecutionConfig:
    """Only for the optional `tiamat` backend. Issuer, key ID, registered key, and endpoint come
    from Tiamat provisioning; Homes never requires them otherwise."""

    url: str
    key_id: str
    issuer: str
    subject: str
    private_key_pem: str


@dataclass(frozen=True, slots=True)
class DirectProviderConfig:
    """Homes' own provider route. The credential, model, and price ceilings are Homes deployment
    policy; nothing here defaults a real one."""

    api_key: str
    model: str
    allowed_providers: tuple[str, ...]
    max_prompt_usd_per_million: float
    max_completion_usd_per_million: float
    referer: str


@dataclass(frozen=True, slots=True)
class HomesPrimeConfig:
    """Stage 2 candidate engine settings. Homes selects its inference backend explicitly; every
    value that selects a provider route, credential, or spending ceiling is deployment policy, and
    nothing here defaults a real one."""

    inference_backend: InferenceBackendName
    direct: DirectProviderConfig | None
    tiamat: TiamatExecutionConfig | None
    generate: ExecutionProfileConfig
    review: ExecutionProfileConfig
    transit_allowance_ms: int
    prime_reserve_ms: int
    knowledge_path: str
    knowledge_allowed_digests: frozenset[str]
    knowledge_withdrawn_ids: frozenset[str]
    approved_hostnames: frozenset[str]
    knowledge_live: bool = False
    """KNOWLEDGE_RELEASE_ID=live: guest knowledge is built from the business core's property
    records, with `knowledge_path` naming the hand-maintained base entries."""

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> HomesPrimeConfig:
        prefix = f"{_ENV_PREFIX}HOMES_PRIME_"

        def require(name: str) -> str:
            value = (env.get(prefix + name) or "").strip()
            if not value:
                raise ConfigError(f"missing required environment variable {prefix}{name}")
            return value

        def optional(name: str, default: str) -> str:
            value = (env.get(prefix + name) or "").strip()
            return value or default

        def integer(name: str, default: str | None, low: int, high: int) -> int:
            raw = require(name) if default is None else optional(name, default)
            try:
                value = int(raw)
            except ValueError as exc:
                raise ConfigError(f"{prefix}{name} must be an integer") from exc
            if not low <= value <= high:
                raise ConfigError(f"{prefix}{name} must be within {low}-{high}")
            return value

        def profile(
            kind: str, default_id: str, ceiling: str, tokens: str
        ) -> ExecutionProfileConfig:
            return ExecutionProfileConfig(
                profile_id=optional(f"{kind}_PROFILE_ID", default_id),
                ceiling_ms=integer(f"{kind}_CEILING_MS", ceiling, 1_000, 18_000),
                max_output_tokens=integer(f"{kind}_MAX_OUTPUT_TOKENS", tokens, 1, 4_096),
                # Spending ceilings are deployment policy: required, never defaulted.
                max_cost_microusd=integer(f"{kind}_MAX_COST_MICROUSD", None, 1, 1_000_000),
            )

        live = env.get(f"{_ENV_PREFIX}KNOWLEDGE_RELEASE_ID") == LIVE_KNOWLEDGE
        if live:
            digests: frozenset[str] = frozenset()
        else:
            digests_raw = require("KNOWLEDGE_ALLOWED_DIGESTS")
            digests = frozenset(item.strip() for item in digests_raw.split(",") if item.strip())
            if not digests or not all(re.fullmatch(r"[a-f0-9]{64}", item) for item in digests):
                raise ConfigError(
                    f"{prefix}KNOWLEDGE_ALLOWED_DIGESTS must be lowercase SHA-256 values"
                )

        try:
            withdrawn = json.loads(optional("KNOWLEDGE_WITHDRAWN_IDS_JSON", "[]"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{prefix}KNOWLEDGE_WITHDRAWN_IDS_JSON is not valid JSON") from exc
        if not isinstance(withdrawn, list) or not all(isinstance(i, str) and i for i in withdrawn):
            raise ConfigError(f"{prefix}KNOWLEDGE_WITHDRAWN_IDS_JSON must be an array of ids")

        hostnames = frozenset(
            item.strip().lower()
            for item in optional("APPROVED_HOSTNAMES", ",".join(DEFAULT_APPROVED_HOSTNAMES)).split(
                ","
            )
            if item.strip()
        )
        if not hostnames or not all(_SITE_HOSTNAME_RE.fullmatch(host) for host in hostnames):
            raise ConfigError(f"{prefix}APPROVED_HOSTNAMES must list valid DNS hostnames")

        backend = require("INFERENCE_BACKEND")
        if backend not in ("direct-openrouter", "tiamat"):
            raise ConfigError(f"{prefix}INFERENCE_BACKEND must be 'direct-openrouter' or 'tiamat'")
        direct: DirectProviderConfig | None = None
        tiamat: TiamatExecutionConfig | None = None
        if backend == "tiamat":
            private_key_pem = require("EXECUTION_PRIVATE_KEY_PEM")
            if "BEGIN PRIVATE KEY" not in private_key_pem:
                raise ConfigError(f"{prefix}EXECUTION_PRIVATE_KEY_PEM must be a PKCS#8 PEM key")
            tiamat = TiamatExecutionConfig(
                url=require("EXECUTION_URL"),
                key_id=require("EXECUTION_KEY_ID"),
                issuer=require("EXECUTION_ISSUER"),
                subject=optional("EXECUTION_SUBJECT", DEFAULT_EXECUTION_SUBJECT),
                private_key_pem=private_key_pem,
            )
        else:
            direct = _direct_provider(require, optional, prefix)

        return cls(
            inference_backend=backend,  # type: ignore[arg-type]
            direct=direct,
            tiamat=tiamat,
            generate=profile("GENERATE", DEFAULT_GENERATE_PROFILE_ID, "9000", "900"),
            review=profile("REVIEW", DEFAULT_REVIEW_PROFILE_ID, "4000", "300"),
            transit_allowance_ms=integer("TRANSIT_ALLOWANCE_MS", "250", 0, 5_000),
            prime_reserve_ms=integer("RESERVE_MS", "1500", 0, 10_000),
            knowledge_path=(
                optional("KNOWLEDGE_BASE_PATH", "knowledge/base/homes-base-entries.json")
                if live
                else require("KNOWLEDGE_PATH")
            ),
            knowledge_allowed_digests=digests,
            knowledge_withdrawn_ids=frozenset(withdrawn),
            approved_hostnames=hostnames,
            knowledge_live=live,
        )


DEFAULT_MEETING_PROFILE_ID: Final = "utopia-homes.meeting-assist.v1"
MEETING_RESPOND_BUDGET_MS: Final = 10_000
"""Homes' own cap on one respond call. The spoken-answer target is 3-5 s and routine answers over
8-10 s are investigated; the cap must also fit inside Tiamat's authorized execution deadlines."""
MEETING_DRAFT_BUDGET_MS: Final = 20_000
"""Homes' own cap on one draft call (target: a complete draft within 10-15 s)."""
MAX_MEETING_IDEMPOTENCY_TTL_SECONDS: Final = 900


@dataclass(frozen=True, slots=True)
class MeetingConfig:
    """Homes Dragon meeting operations. Inference uses the Homes Prime execution identity under
    one meeting execution profile; respond and draft use that profile with their own ceilings."""

    respond: ExecutionProfileConfig
    draft: ExecutionProfileConfig
    materials_path: str
    materials_allowed_digests: frozenset[str]
    idempotency_ttl_seconds: int

    @classmethod
    def from_environment(cls, env: Mapping[str, str], *, prime: HomesPrimeConfig) -> MeetingConfig:
        prefix = f"{_ENV_PREFIX}MEETING_"

        def value(name: str, default: str | None) -> str:
            raw = (env.get(prefix + name) or "").strip()
            if raw:
                return raw
            if default is None:
                raise ConfigError(f"missing required environment variable {prefix}{name}")
            return default

        def integer(name: str, default: str | None, low: int, high: int) -> int:
            try:
                parsed = int(value(name, default))
            except ValueError as exc:
                raise ConfigError(f"{prefix}{name} must be an integer") from exc
            if not low <= parsed <= high:
                raise ConfigError(f"{prefix}{name} must be within {low}-{high}")
            return parsed

        profile_id = value("PROFILE_ID", DEFAULT_MEETING_PROFILE_ID)

        def operation(
            kind: str, ceiling: str, tokens: str, budget_ms: int
        ) -> ExecutionProfileConfig:
            config = ExecutionProfileConfig(
                profile_id=profile_id,
                ceiling_ms=integer(f"{kind}_CEILING_MS", ceiling, 1_000, 18_000),
                max_output_tokens=integer(f"{kind}_MAX_OUTPUT_TOKENS", tokens, 1, 4_096),
                # Spending ceilings are deployment policy: required, never defaulted.
                max_cost_microusd=integer(f"{kind}_MAX_COST_MICROUSD", None, 1, 1_000_000),
            )
            needed = config.ceiling_ms + 2 * prime.transit_allowance_ms + prime.prime_reserve_ms
            if needed > budget_ms:
                raise ConfigError(
                    f"{prefix}{kind}_CEILING_MS plus transit and reserve exceeds {budget_ms} ms"
                )
            return config

        digests = frozenset(
            item.strip() for item in value("MATERIALS_ALLOWED_DIGESTS", None).split(",") if item
        )
        if not digests or not all(re.fullmatch(r"[a-f0-9]{64}", item) for item in digests):
            raise ConfigError(f"{prefix}MATERIALS_ALLOWED_DIGESTS must be lowercase SHA-256 values")

        return cls(
            respond=operation("RESPOND", "6000", "1200", MEETING_RESPOND_BUDGET_MS),
            draft=operation("DRAFT", "15000", "2500", MEETING_DRAFT_BUDGET_MS),
            materials_path=value("MATERIALS_PATH", None),
            materials_allowed_digests=digests,
            idempotency_ttl_seconds=integer(
                "IDEMPOTENCY_TTL_SECONDS", "300", 1, MAX_MEETING_IDEMPOTENCY_TTL_SECONDS
            ),
        )


@dataclass(frozen=True, slots=True)
class BusinessCoreConfig:
    """Utopia's property records (the business core). Present only where a database is set."""

    database_url: str
    lucy_token: str
    seed_path: str | None
    guest_token: str | None = None
    """Guest Lucy's token: only her turn-scoped /guest/v1 routes accept it."""
    guest_worker_url: str | None = None
    """Guest Lucy's webhook, e.g. http://utopia-lucy-guest:8644/webhooks/guest_turn."""
    guest_webhook_secret: str | None = None
    support_phone: str | None = None
    notify_bot_token: str | None = None
    """Utopia Lucy's Telegram bot token, used only to send host notifications."""
    notify_chat_id: str | None = None

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> BusinessCoreConfig | None:
        url = (env.get("UTOPIA_BUSINESS_DATABASE_URL") or "").strip()
        if not url:
            return None
        token = (env.get("UTOPIA_BUSINESS_LUCY_TOKEN") or "").strip()
        if len(token) < 32:
            raise ConfigError("UTOPIA_BUSINESS_LUCY_TOKEN must be at least 32 characters")
        seed = (env.get("UTOPIA_BUSINESS_SEED_PATH") or "").strip() or None
        guest = (env.get("UTOPIA_BUSINESS_GUEST_TOKEN") or "").strip() or None
        if guest is not None and (len(guest) < 32 or guest == token):
            raise ConfigError(
                "UTOPIA_BUSINESS_GUEST_TOKEN must be at least 32 characters and differ from "
                "Lucy's token"
            )
        worker = (env.get("UTOPIA_GUEST_WORKER_URL") or "").strip() or None
        secret = (env.get("UTOPIA_GUEST_WEBHOOK_SECRET") or "").strip() or None
        if worker and not secret:
            raise ConfigError("UTOPIA_GUEST_WORKER_URL needs UTOPIA_GUEST_WEBHOOK_SECRET")
        return cls(
            database_url=url,
            lucy_token=token,
            seed_path=seed,
            guest_token=guest,
            guest_worker_url=worker,
            guest_webhook_secret=secret,
            support_phone=(env.get("UTOPIA_SUPPORT_PHONE") or "").strip() or None,
            notify_bot_token=(env.get("UTOPIA_NOTIFY_TELEGRAM_BOT_TOKEN") or "").strip() or None,
            notify_chat_id=(env.get("UTOPIA_NOTIFY_TELEGRAM_CHAT_ID") or "").strip() or None,
        )


LATEST_APPROVED_KNOWLEDGE: Final = "latest-approved"
LIVE_KNOWLEDGE: Final = "live"


def resolve_knowledge_release(env: Mapping[str, str]) -> Mapping[str, str]:
    """`KNOWLEDGE_RELEASE_ID=latest-approved` serves the last release in the knowledge register
    (`HOMES_PRIME_KNOWLEDGE_REGISTER`, e.g. /app/knowledge/releases.json). Every register entry
    reached main through Ray's approval, so a deploy picks up a newly approved release without
    anyone retyping its path and digest. The digest pin still applies; it comes from the
    register."""
    release_key = f"{_ENV_PREFIX}KNOWLEDGE_RELEASE_ID"
    if env.get(release_key) != LATEST_APPROVED_KNOWLEDGE:
        return env
    prime = f"{_ENV_PREFIX}HOMES_PRIME_"
    for pinned in ("KNOWLEDGE_PATH", "KNOWLEDGE_ALLOWED_DIGESTS"):
        if env.get(prime + pinned):
            raise ConfigError(
                f"{prime}{pinned} must be unset when serving {LATEST_APPROVED_KNOWLEDGE}"
            )
    register_path = Path((env.get(prime + "KNOWLEDGE_REGISTER") or "").strip())
    if not register_path.name:
        raise ConfigError(f"missing required environment variable {prime}KNOWLEDGE_REGISTER")
    try:
        latest = json.loads(register_path.read_text(encoding="utf-8"))["releases"][-1]
        resolved = {
            release_key: str(latest["release_id"]),
            prime + "KNOWLEDGE_PATH": str(register_path.parent.parent / latest["file"]),
            prime + "KNOWLEDGE_ALLOWED_DIGESTS": str(latest["canonical_digest"]),
        }
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise ConfigError(f"{prime}KNOWLEDGE_REGISTER has no readable latest release") from exc
    return {**env, **resolved}


@dataclass(frozen=True, slots=True)
class Config:
    environment: Environment
    port: int
    jwt_keys: tuple[JwtAllowlistedKey, ...]
    rate_limit_per_minute: int
    log_level: str
    idempotency_ttl_seconds: float
    idempotency_in_progress_ceiling_seconds: float
    legacy_upstream: LegacyUpstreamConfig | None
    business_release_id: str
    knowledge_release_id: str
    answer_engine: AnswerEngineName = "legacy-bridge"
    homes_prime: HomesPrimeConfig | None = None
    meeting: MeetingConfig | None = None
    business: BusinessCoreConfig | None = None

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> Config:
        env = resolve_knowledge_release(environment if environment is not None else os.environ)

        def require(name: str) -> str:
            value = env.get(f"{_ENV_PREFIX}{name}")
            if value is None or value == "":
                raise ConfigError(f"missing required environment variable {_ENV_PREFIX}{name}")
            return value

        def optional(name: str, default: str | None = None) -> str | None:
            value = env.get(f"{_ENV_PREFIX}{name}")
            return value if value not in (None, "") else default

        try:
            port = int(env.get("PORT", "8081"))
        except ValueError as exc:
            raise ConfigError("PORT must be an integer") from exc

        provider_environment = optional("ENVIRONMENT", "preview") or "preview"
        if provider_environment not in ("production", "preview"):
            raise ConfigError(f"{_ENV_PREFIX}ENVIRONMENT must be 'production' or 'preview'")

        jwt_keys = cls._parse_jwt_keys(require("JWT_PUBLIC_KEYS_JSON"))

        rate_limit_raw = optional("RATE_LIMIT_PER_MINUTE", "120")
        try:
            rate_limit_per_minute = int(rate_limit_raw) if rate_limit_raw is not None else 120
        except ValueError as exc:
            raise ConfigError(f"{_ENV_PREFIX}RATE_LIMIT_PER_MINUTE must be an integer") from exc
        if rate_limit_per_minute < MINIMUM_RATE_LIMIT_PER_MINUTE:
            raise ConfigError(
                f"{_ENV_PREFIX}RATE_LIMIT_PER_MINUTE must be at least "
                f"{MINIMUM_RATE_LIMIT_PER_MINUTE}"
            )

        ttl_raw = optional("IDEMPOTENCY_TTL_SECONDS", str(DEFAULT_IDEMPOTENCY_TTL_SECONDS))
        try:
            idempotency_ttl_seconds = (
                float(ttl_raw) if ttl_raw is not None else float(DEFAULT_IDEMPOTENCY_TTL_SECONDS)
            )
        except ValueError as exc:
            raise ConfigError(f"{_ENV_PREFIX}IDEMPOTENCY_TTL_SECONDS must be a number") from exc
        if idempotency_ttl_seconds <= 0:
            raise ConfigError(f"{_ENV_PREFIX}IDEMPOTENCY_TTL_SECONDS must be positive")

        ceiling_raw = optional(
            "IDEMPOTENCY_IN_PROGRESS_CEILING_SECONDS",
            str(DEFAULT_IDEMPOTENCY_IN_PROGRESS_CEILING_SECONDS),
        )
        try:
            idempotency_in_progress_ceiling_seconds = (
                float(ceiling_raw)
                if ceiling_raw is not None
                else float(DEFAULT_IDEMPOTENCY_IN_PROGRESS_CEILING_SECONDS)
            )
        except ValueError as exc:
            raise ConfigError(
                f"{_ENV_PREFIX}IDEMPOTENCY_IN_PROGRESS_CEILING_SECONDS must be a number"
            ) from exc
        if idempotency_in_progress_ceiling_seconds <= 0:
            raise ConfigError(
                f"{_ENV_PREFIX}IDEMPOTENCY_IN_PROGRESS_CEILING_SECONDS must be positive"
            )

        log_level = (optional("LOG_LEVEL", "INFO") or "INFO").upper()

        answer_engine = optional("ANSWER_ENGINE", "legacy-bridge") or "legacy-bridge"
        if answer_engine not in ("legacy-bridge", "homes-prime"):
            raise ConfigError(
                f"{_ENV_PREFIX}ANSWER_ENGINE must be 'legacy-bridge' or 'homes-prime'"
            )

        legacy_upstream: LegacyUpstreamConfig | None = None
        homes_prime: HomesPrimeConfig | None = None
        if answer_engine == "legacy-bridge":
            legacy_upstream = LegacyUpstreamConfig.from_environment(
                env, provider_environment=provider_environment
            )
        else:
            # Production use was approved by Ray on 2026-09-24 (cutover gate C8). The legacy bridge
            # remains the default engine, so homes-prime runs only where it is selected explicitly.
            homes_prime = HomesPrimeConfig.from_environment(env)
            # A pipeline may legitimately run for the full 15s attempt; an in-progress record must
            # not be declared unresolved while its only pipeline can still complete.
            if optional("IDEMPOTENCY_IN_PROGRESS_CEILING_SECONDS") is None:
                idempotency_in_progress_ceiling_seconds = HOMES_PRIME_IN_PROGRESS_CEILING_SECONDS
            elif idempotency_in_progress_ceiling_seconds < HOMES_PRIME_IN_PROGRESS_CEILING_SECONDS:
                raise ConfigError(
                    f"{_ENV_PREFIX}IDEMPOTENCY_IN_PROGRESS_CEILING_SECONDS must be at least "
                    f"{HOMES_PRIME_IN_PROGRESS_CEILING_SECONDS} for the homes-prime engine"
                )

        meeting_enabled = optional("MEETING_ENABLED", "false")
        if meeting_enabled not in ("true", "false"):
            raise ConfigError(f"{_ENV_PREFIX}MEETING_ENABLED must be 'true' or 'false'")
        meeting: MeetingConfig | None = None
        if meeting_enabled == "true":
            # Meeting inference goes through the Homes Prime engine's selected backend, so the
            # meeting operations exist only where the homes-prime engine is configured.
            if homes_prime is None:
                raise ConfigError("the meeting operations require the homes-prime answer engine")
            meeting = MeetingConfig.from_environment(env, prime=homes_prime)

        release_id_pattern = r"^[\x21-\x7e]{1,128}$"
        business_release_id = require("BUSINESS_RELEASE_ID")
        if not re.fullmatch(release_id_pattern, business_release_id):
            raise ConfigError(
                f"{_ENV_PREFIX}BUSINESS_RELEASE_ID does not match the required format"
            )
        knowledge_release_id = require("KNOWLEDGE_RELEASE_ID")
        if not re.fullmatch(release_id_pattern, knowledge_release_id):
            raise ConfigError(
                f"{_ENV_PREFIX}KNOWLEDGE_RELEASE_ID does not match the required format"
            )

        business = BusinessCoreConfig.from_environment(env)
        if homes_prime is not None and homes_prime.knowledge_live and business is None:
            raise ConfigError(
                f"{_ENV_PREFIX}KNOWLEDGE_RELEASE_ID=live needs UTOPIA_BUSINESS_DATABASE_URL"
            )

        return cls(
            environment=provider_environment,  # type: ignore[arg-type]
            port=port,
            jwt_keys=jwt_keys,
            rate_limit_per_minute=rate_limit_per_minute,
            log_level=log_level,
            idempotency_ttl_seconds=idempotency_ttl_seconds,
            idempotency_in_progress_ceiling_seconds=idempotency_in_progress_ceiling_seconds,
            legacy_upstream=legacy_upstream,
            business_release_id=business_release_id,
            knowledge_release_id=knowledge_release_id,
            answer_engine=answer_engine,  # type: ignore[arg-type]
            homes_prime=homes_prime,
            meeting=meeting,
            business=business,
        )

    @staticmethod
    def _parse_jwt_keys(raw: str) -> tuple[JwtAllowlistedKey, ...]:
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{_ENV_PREFIX}JWT_PUBLIC_KEYS_JSON is not valid JSON") from exc
        if not isinstance(entries, list) or not entries:
            raise ConfigError(f"{_ENV_PREFIX}JWT_PUBLIC_KEYS_JSON must be a non-empty JSON array")

        seen_kids: set[str] = set()
        keys: list[JwtAllowlistedKey] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ConfigError(f"{_ENV_PREFIX}JWT_PUBLIC_KEYS_JSON entries must be objects")

            kid = entry.get("kid")
            public_key_pem = entry.get("public_key_pem")
            key_environment = entry.get("environment")
            issuer = entry.get("issuer")
            subject = entry.get("subject")
            audience = entry.get("audience")
            capabilities = entry.get("capabilities")
            status = entry.get("status", "active")

            if not isinstance(kid, str) or not kid:
                raise ConfigError("invalid kid in JWT_PUBLIC_KEYS_JSON")
            if kid in seen_kids:
                raise ConfigError(f"duplicate kid {kid!r} in JWT_PUBLIC_KEYS_JSON")
            seen_kids.add(kid)

            if not isinstance(public_key_pem, str) or "BEGIN PUBLIC KEY" not in public_key_pem:
                raise ConfigError(f"invalid public_key_pem for kid {kid!r}")
            if key_environment not in ("production", "preview"):
                raise ConfigError(f"invalid environment for kid {kid!r}")
            if not isinstance(issuer, str) or not issuer:
                raise ConfigError(f"invalid issuer for kid {kid!r}")
            if not isinstance(subject, str) or not subject:
                raise ConfigError(f"invalid subject for kid {kid!r}")
            if not isinstance(audience, str) or not audience:
                raise ConfigError(f"invalid audience for kid {kid!r}")
            if (
                not isinstance(capabilities, list)
                or not capabilities
                or not all(isinstance(c, str) and c for c in capabilities)
            ):
                raise ConfigError(f"invalid capabilities for kid {kid!r}")
            if status not in ("active", "retired", "revoked"):
                raise ConfigError(f"invalid status for kid {kid!r}")

            keys.append(
                JwtAllowlistedKey(
                    kid=kid,
                    public_key_pem=public_key_pem,
                    environment=key_environment,
                    issuer=issuer,
                    subject=subject,
                    audience=audience,
                    capabilities=tuple(capabilities),
                    status=status,
                )
            )
        return tuple(keys)
