"""Model factory. Default: an open-weights model (Qwen, Gemma, Llama, Mistral, Aya...) served by Ollama
on localhost, through its OpenAI-compatible API. Gemini is available for SYNTHETIC patients only."""
import os

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from . import config

GEMINI_OPENAI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def build_model():
    if config.provider() == "gemini":
        key = os.getenv("GEMINI_API_KEY", "")
        if not key:
            raise RuntimeError("TRIAGE_PROVIDER=gemini needs GEMINI_API_KEY (synthetic patients only).")
        prov = OpenAIProvider(base_url=GEMINI_OPENAI_URL, api_key=key)
    else:
        prov = OpenAIProvider(base_url=config.ollama_url(), api_key="ollama")
    return OpenAIChatModel(config.model_name(), provider=prov)
