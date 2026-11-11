"""루트 .env만 읽는다. 설정 생성과 모듈 import는 외부 API를 호출하지 않는다."""

from dataclasses import dataclass, field
from pathlib import Path
import os

from dotenv import dotenv_values
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]



@dataclass(frozen=True)
class Settings:
    openai_api_key: SecretStr | None = field(default=None, repr=False)
    openai_model: str = "gpt-5.4-mini"
    openai_timeout_seconds: float = 45
    backend_internal_base_url: str = ""
    internal_service_token: SecretStr | None = field(default=None, repr=False)
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")

    def __post_init__(self):
        if not 1 <= self.openai_timeout_seconds <= 120:
            raise ValueError("OPENAI_TIMEOUT_SECONDS는 1~120초여야 합니다.")

    @property
    def model_ready(self):
        return bool(self.openai_api_key and self.openai_api_key.get_secret_value().strip() and self.openai_model.strip())

    @property
    def backend_ready(self):
        return bool(self.backend_internal_base_url and self.internal_service_token)


def get_settings():
    values = {**dotenv_values(ROOT / ".env"), **os.environ}

    def value(name, default=""):
        return (values.get(name) or default).strip()

    def secret(name):
        return SecretStr(value(name)) if value(name) else None

    return Settings(
        openai_api_key=secret("OPENAI_API_KEY"),
        openai_model=value("OPENAI_MODEL", "gpt-5.4-mini"),
        openai_timeout_seconds=float(value("OPENAI_TIMEOUT_SECONDS", "45")),
        backend_internal_base_url=value("BACKEND_INTERNAL_BASE_URL").rstrip("/"),
        internal_service_token=secret("INTERNAL_SERVICE_TOKEN"),
        cors_origins=tuple(s.strip() for s in value("AI_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if s.strip()),
    )
