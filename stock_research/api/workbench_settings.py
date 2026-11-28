"""Administrator settings for the local workbench."""

from __future__ import annotations

from ipaddress import ip_address
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from stock_research.config import get_settings
from stock_research.settings_store import SettingsValues, read_overrides, update_overrides

router = APIRouter(prefix="/api/workbench/settings")

_SECRET_NAMES = ("deepseek_api_key", "finnhub_api_key")


def normalize_model_url(value: str) -> str | None:
    url = value.strip().rstrip("/")
    if not url:
        return None
    try:
        parts = urlsplit(url)
        if (
            parts.scheme in {"http", "https"}
            and parts.hostname
            and parts.port != 0
            and parts.username is None
            and "?" not in url
            and "#" not in url
            and "\\" not in url
            and not any(char.isspace() or ord(char) < 32 for char in url)
        ):
            return url
    except ValueError:
        pass
    raise ValueError("请输入有效的 HTTP 或 HTTPS Base URL，例如 https://api.deepseek.com/v1。")


class WorkbenchSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deepseek_base_url: Annotated[str, Field(max_length=512)] | None = None
    deepseek_model: Annotated[str, Field(max_length=128)] | None = None
    deepseek_api_key: Annotated[SecretStr, Field(max_length=4096)] | None = None
    finnhub_api_key: Annotated[SecretStr, Field(max_length=4096)] | None = None
    sec_user_agent: Annotated[str, Field(max_length=256)] | None = None
    llm_batch_k: Annotated[int, Field(strict=True, ge=1, le=128)] | None = None
    llm_threads: Annotated[int, Field(strict=True, ge=1, le=64)] | None = None
    llm_timeout: Annotated[float, Field(strict=True, ge=1, le=900, allow_inf_nan=False)] | None = None

    @field_validator("deepseek_base_url")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = normalize_model_url(value)
        if normalized is None:
            raise ValueError("请填写模型服务地址。")
        return normalized

    @field_validator("deepseek_model", "sec_user_agent")
    @classmethod
    def valid_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("请填写内容，且勿包含控制字符。")
        return value

    @field_validator("sec_user_agent")
    @classmethod
    def valid_user_agent(cls, value: str | None) -> str | None:
        if value is not None and not value.isascii():
            raise ValueError("SEC 联系信息须使用英文字符，便于写入 HTTP 请求头。")
        return value

    @field_validator(*_SECRET_NAMES)
    @classmethod
    def valid_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return value
        token = value.get_secret_value().strip()
        if not token or not token.isprintable() or any(char.isspace() for char in token):
            raise ValueError("请输入有效的 API Key。")
        return SecretStr(token)


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _require_local(request: Request) -> None:
    origin = request.headers.get("origin")
    if (
        request.client is None
        or not _loopback(request.client.host)
        or not _loopback(request.url.hostname)
        or (origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}")
    ):
        raise HTTPException(403, "请通过本机工作台打开服务端设置。")


def _snapshot() -> JSONResponse:
    settings = get_settings(ensure_dirs=False)
    overrides = read_overrides(settings.runtime_dir)
    return JSONResponse({
        "deepseek_base_url": settings.deepseek_base_url,
        "deepseek_model": settings.deepseek_model,
        "sec_user_agent": settings.sec_user_agent,
        "llm_batch_k": settings.llm_batch_k,
        "llm_threads": settings.llm_threads,
        "llm_timeout": settings.llm_timeout,
        "has_deepseek_api_key": bool(settings.deepseek_api_key),
        "has_finnhub_api_key": bool(settings.finnhub_api_key),
        "overridden": list(overrides),
    }, headers={"Cache-Control": "no-store"})


@router.get("")
def read_settings(request: Request) -> JSONResponse:
    _require_local(request)
    return _snapshot()


@router.patch("")
def save_settings(request: Request, patch: WorkbenchSettingsPatch) -> JSONResponse:
    _require_local(request)
    defaults = get_settings(ensure_dirs=False, use_saved=False)
    values = patch.model_dump(exclude_unset=True)

    def apply(previous: SettingsValues) -> SettingsValues:
        updated = previous.copy()
        for key, value in values.items():
            if value is None:
                updated.pop(key, None)
            else:
                updated[key] = value.get_secret_value() if isinstance(value, SecretStr) else value
        old_url = previous.get("deepseek_base_url", defaults.deepseek_base_url)
        new_url = updated.get("deepseek_base_url", defaults.deepseek_base_url)
        if old_url != new_url and "deepseek_api_key" not in values:
            if previous.get("deepseek_api_key") or (old_url == defaults.deepseek_base_url and defaults.deepseek_api_key):
                raise HTTPException(422, "更换模型服务地址时，请填写新服务的 Key，或选择恢复环境配置。")
        return updated

    try:
        update_overrides(defaults.runtime_dir, apply)
    except OSError as exc:
        raise HTTPException(500, "保存失败，请检查运行目录的写入权限。") from exc
    return _snapshot()
