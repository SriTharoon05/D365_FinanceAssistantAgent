"""Server-side Azure OpenAI factory. Keys are never passed to a browser or DB."""

from typing import Any

from langchain_openai import AzureChatOpenAI

from app.core.errors import AppError


def create_chat_model(settings: Any) -> AzureChatOpenAI:
    key = settings.azure_openai_api_key
    if hasattr(key, "get_secret_value"):
        key = key.get_secret_value()
    if not key:
        raise AppError(
            "azure_openai_unavailable",
            "Azure OpenAI is not configured. Add AZURE_OPENAI_API_KEY in the backend environment, then retry.",
            status_code=503,
            retryable=True,
        )
    return AzureChatOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=key,
        azure_deployment=settings.azure_openai_deployment,
        api_version=settings.azure_openai_api_version,
        temperature=0.1,
        streaming=True,
        max_tokens=4096,
        timeout=45,
        max_retries=1,
    )
