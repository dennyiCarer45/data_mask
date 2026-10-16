from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRIVACY_", extra="ignore")
    api_key: SecretStr = SecretStr("")
    signing_key: SecretStr = SecretStr("")
    proxy_key: SecretStr = SecretStr("")
    detector: Literal["gliner", "rules"] = "gliner"
    model_path: Path = Path("models/gliner")
    policies_path: Path = Path("config/policies.yaml")
    proxy_url: str = "http://127.0.0.1:4000"
    proxy_model: str = "privacy-mock"
    external_enabled: bool = False
    max_text_chars: int = Field(32000, ge=1, le=200000)
    max_body_bytes: int = Field(6_000_000, ge=1024)
    max_document_bytes: int = Field(5_000_000, ge=1024)
    max_output_chars: int = Field(32000, ge=1)
    gliner_threshold: float = Field(0.35, ge=0.05, le=0.95)
    # Local Tesseract OCR for image files and image regions in PDFs. If the program or a
    # language pack is missing, images are refused and image-bearing PDFs are rejected.
    ocr: Literal["tesseract", "off"] = "tesseract"
    ocr_languages: str = Field("eng+hin", pattern=r"^[a-z_]{3,16}(?:\+[a-z_]{3,16}){0,4}$")
    # OCR words below this confidence (0-100) are masked as UNVERIFIED_TEXT.
    ocr_min_confidence: float = Field(60, ge=0, le=100)
    ocr_timeout_seconds: int = Field(60, ge=5, le=600)
