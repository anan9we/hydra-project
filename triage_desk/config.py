"""Settings read from the environment / .env. Functions (not constants) so tests can change env."""
import ipaddress
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def db_path() -> str:
    p = Path(os.getenv("TRIAGE_DB", "triage.db"))
    return str(p if p.is_absolute() else ROOT / p)


def provider() -> str:
    """'ollama' = open-weights model on a local/LAN server (default). 'gemini' = cloud, synthetic data only."""
    return os.getenv("TRIAGE_PROVIDER", "ollama").lower()


def model_name() -> str:
    if provider() == "gemini":
        return os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    return os.getenv("TRIAGE_MODEL", "qwen2.5:7b")


def ollama_url() -> str:
    return os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")


def is_local_endpoint(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", ""):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return False


def is_cloud_llm() -> bool:
    """True when prompts would leave the machine/LAN. Then only synthetic patients may be sent."""
    if provider() == "gemini":
        return True
    return not is_local_endpoint(ollama_url())


def price_per_million_tokens() -> tuple[float, float]:
    """(input, output) USD per 1M tokens. Default 0: open-weights model on own hardware."""
    return (float(os.getenv("PRICE_IN_PER_M", "0")), float(os.getenv("PRICE_OUT_PER_M", "0")))
