from functools import lru_cache
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MESSAGING_", extra="ignore")
    database_url: str = ""
    encryption_key: str = ""
    hmac_key: str = ""
    control_token: str = ""
    transport_token: str = ""
    operator_token: str = ""
    provider_account_id: str = "squire"
    transport_url: str = ""
    transport_broker_token: str = ""
    control_url: str = ""
    control_callback_token: str = ""
    outbound_enabled: bool = False
    auto_migrate: bool = False
    allow_sqlite: bool = False
    runtime_host_suffix: str = ".railway.internal"
    lease_seconds: int = Field(180, ge=30, le=900)
    send_spacing_seconds: float = Field(1.0, ge=0)
    queue_cap_per_tenant: int = Field(200, ge=1)
    queue_cap_total: int = Field(4000, ge=1)
    group_limit: int = Field(1, ge=1)
    challenge_ttl_seconds: int = Field(600, ge=60)
    group_claim_ttl_seconds: int = Field(600, ge=60)
    group_metadata_ttl_seconds: int = Field(60, ge=1, le=300)
    dispatch_concurrency: int = Field(4, ge=1, le=32)

    def check_secrets(self) -> None:
        if not self.database_url:
            raise ValueError("MESSAGING_DATABASE_URL is required")
        if self.database_url.startswith("sqlite") and not self.allow_sqlite:
            raise ValueError("SQLite is test-only; production requires PostgreSQL")
        if any(len(value) < 32 for value in (self.encryption_key, self.hmac_key,
                                           self.control_token, self.transport_token,
                                           self.operator_token)):
            raise ValueError("Messaging secrets must be configured with at least 32 characters")
        if len({self.control_token, self.transport_token, self.operator_token}) != 3:
            raise ValueError("Service principals must use distinct credentials")


@lru_cache
def settings() -> Settings:
    return Settings()
