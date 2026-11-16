from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRANSPORT_", extra="ignore")
    data_dir: Path = Path("/data")
    wacli_binary: str = "/usr/local/bin/wacli"
    provider_account_id: str = Field(min_length=1, max_length=100)
    broker_token: str = Field(min_length=32)
    operator_token: str = Field(min_length=32)
    backup_token: str | None = Field(default=None, min_length=32)
    backup_recipient: str | None = None
    messaging_token: str = Field(min_length=32, validation_alias="MESSAGING_TRANSPORT_TOKEN")
    messaging_url: str
    generation: int = Field(default=1, ge=1)
    sync_enabled: bool = True
    pairing_mode: bool = False
    outbound_enabled: bool = False
    require_mount: bool = True
    command_timeout: float = Field(default=60, gt=0, le=180)
    poll_seconds: float = Field(default=1, ge=0.1, le=60)
    max_db_size: str = "2GB"
    send_spacing_seconds: float = Field(default=1, ge=0, le=60)
    max_output_bytes: int = 4 * 1024 * 1024
    retention_enabled: bool = True
    retention_interval_seconds: int = Field(default=3600, ge=60, le=86400)
    retention_days: int = Field(default=7, ge=1, le=30)

    @property
    def store_dir(self) -> Path:
        return self.data_dir / "wacli"

    @model_validator(mode="after")
    def distinct_secrets(self):
        if len({self.broker_token, self.operator_token, self.messaging_token}) != 3:
            raise ValueError("transport principals require distinct credentials")
        if self.backup_token and self.backup_token in {self.broker_token, self.operator_token, self.messaging_token}:
            raise ValueError("backup principal requires distinct credential")
        if not self.messaging_url.startswith(("https://", "http://")):
            raise ValueError("messaging URL must be HTTP(S)")
        return self
