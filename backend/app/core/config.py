from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./data/finance_assistant.db"
    frontend_origin: str = "http://localhost:5173"
    app_session_secret: str = Field(default="change-this-locally", repr=False)
    session_cookie_secure: bool = False
    max_request_mb: int = Field(default=16, ge=1, le=100)
    chat_rate_limit_per_minute: int = Field(default=20, ge=1, le=1000)
    action_expiry_minutes: int = Field(default=10, ge=1, le=60)
    agent_max_iterations: int = Field(default=6, ge=1, le=12)

    d365_base_url: str = "https://org1a43c536.operations.dynamics.com"
    d365_tenant_id: str = Field(default="", repr=False)
    d365_client_id: str = Field(default="", repr=False)
    d365_client_secret: str = Field(default="", repr=False)
    d365_default_company: str = "usmf"
    d365_customers_entity: str = "CustomersV3"
    d365_customer_groups_entity: str = "CustomerGroups"
    d365_currencies_entity: str = "Currencies"
    d365_main_accounts_entity: str = "MainAccounts"
    d365_free_text_headers_entity: str = "CDSFreeTextInvoiceHeaders"
    d365_free_text_lines_entity: str = "CDSFreeTextInvoiceLines"
    d365_payment_headers_entity: str = "CustomerPaymentJournalHeaders"
    d365_payment_lines_entity: str = "CustomerPaymentJournalLines"
    d365_customer_transactions_entity: str = "auto"
    d365_open_transactions_entity: str = "auto"
    d365_payment_journal_name: str = "CustPay"
    d365_payment_bank_account: str = "USMF OPER"
    d365_payment_method: str = "CHECK"
    d365_customer_posting_profile: str = "GEN"
    d365_revenue_account: str = "401100"
    d365_delete_allowed_prefixes: str = "TEST-,DEMO-,CHAT-"
    d365_mock_mode: bool = False
    d365_write_actions_enabled: bool = True
    d365_timeout_seconds: float = Field(default=30, gt=0, le=120)
    d365_connection_timeout_seconds: float = Field(default=60, gt=0, le=300)
    d365_metadata_timeout_seconds: float = Field(default=180, gt=0, le=900)
    d365_metadata_max_mb: int = Field(default=64, ge=1, le=256)
    d365_metadata_cache_hours: float = Field(default=24, ge=0, le=168)
    d365_max_retries: int = Field(default=2, ge=0, le=4)

    azure_openai_endpoint: str = "https://voiceagentdemo-resource.cognitiveservices.azure.com/"
    azure_openai_api_key: str = Field(default="", repr=False)
    azure_openai_deployment: str = "gpt-4.1-mini"
    azure_openai_api_version: str = "2025-03-01-preview"
    groq_api_key: str = Field(default="", repr=False)
    groq_whisper_model: str = "whisper-large-v3-turbo"
    voice_max_upload_mb: int = Field(default=15, ge=1, le=25)
    voice_max_duration_seconds: int = Field(default=120, ge=1, le=600)

    @field_validator("d365_base_url", "azure_openai_endpoint")
    @classmethod
    def https_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Integration endpoints must be HTTPS URLs without credentials")
        return value.rstrip("/")

    @field_validator("d365_default_company")
    @classmethod
    def company_code(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[A-Za-z0-9_-]{1,12}", value):
            raise ValueError("Invalid legal entity")
        return value.lower()

    def database_path(self) -> Path | None:
        from sqlalchemy.engine import make_url

        url = make_url(self.database_url)
        if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
            return Path(url.database).resolve()
        return None

    def allowed_origins(self) -> list[str]:
        from urllib.parse import urlsplit

        origins = {item.strip().rstrip("/") for item in self.frontend_origin.split(",")}
        if self.app_env != "production":
            for origin in list(origins):
                parsed = urlsplit(origin)
                if parsed.hostname in {"localhost", "127.0.0.1"}:
                    alternative = "127.0.0.1" if parsed.hostname == "localhost" else "localhost"
                    port = f":{parsed.port}" if parsed.port else ""
                    origins.add(f"{parsed.scheme}://{alternative}{port}")
        return sorted(origins)


@lru_cache
def get_settings() -> Settings:
    return Settings()
