import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .ratelimit import RateLimiter

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


class LLMError(RuntimeError):
    pass


class LLMClient:
    """One OpenAI-compatible client for every configured provider.

    Retries 429/5xx with exponential backoff, then falls back to the next
    provider. A provider marked ``fallback_on_429`` skips the retries on a 429
    (free-tier quota) and hands the request straight to the fallback. Providers
    whose key is missing are skipped. Returns the response text and the
    ``provider/model`` that served it.
    """

    def __init__(
        self,
        config_path: str | Path | None = None,
        only_provider: str | None = None,
        client_factory: Callable[[dict[str, Any]], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        load_dotenv()
        self._config = yaml.safe_load(Path(config_path or _CONFIG_DIR / "llm.yaml").read_text("utf-8"))
        self._factory = client_factory or self._default_factory
        self._clients: dict[str, Any] = {}
        self._disabled: set[str] = set()
        self._sleeper = sleeper
        self._max_retries = self._config.get("max_retries", 3)
        self._max_tokens = self._config.get("max_tokens", 4096)
        self._temperature = self._config.get("temperature", 0)
        self._timeout = self._config.get("timeout_seconds", 60)
        if only_provider:
            order = [only_provider]
        else:
            order = [self._config["default"]]
            if self._config.get("fallback"):
                order.append(self._config["fallback"])
        # Without a factory override, a provider with no key can't serve anything.
        self._order = [
            name
            for name in order
            if client_factory or os.environ.get(self._config["providers"][name]["api_key_env"])
        ]
        if not self._order:
            raise LLMError("no LLM provider has an API key set (GEMINI_API_KEY / NVIDIA_API_KEY)")
        self._limiters = {
            name: RateLimiter(self._config["providers"][name].get("rpm")) for name in self._order
        }

    def _default_factory(self, provider: dict[str, Any]) -> Any:
        from openai import OpenAI

        # max_retries=0: this class owns retries, the SDK's own would double them.
        return OpenAI(
            base_url=provider["base_url"],
            api_key=os.environ[provider["api_key_env"]],
            timeout=self._timeout,
            max_retries=0,
        )

    def _client(self, name: str) -> Any:
        if name not in self._clients:
            self._clients[name] = self._factory(self._config["providers"][name])
        return self._clients[name]

    def _model(self, name: str) -> str:
        provider = self._config["providers"][name]
        return os.environ.get(provider.get("model_env", ""), "") or provider["model"]

    def model_used(self, name: str) -> str:
        return f"{name}/{self._model(name)}"

    def complete(self, system: str, user: str, json_mode: bool = True) -> tuple[str, str]:
        errors: list[str] = []
        for name in self._order:
            if name in self._disabled:
                continue
            provider = self._config["providers"][name]
            limiter = self._limiters[name]
            for attempt in range(self._max_retries + 1):
                limiter.acquire()
                try:
                    response = self._client(name).chat.completions.create(
                        model=self._model(name),
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        temperature=self._temperature,
                        max_tokens=self._max_tokens,
                        **self._request_extra(provider, json_mode),
                    )
                    content = response.choices[0].message.content
                    logger.info("model %s handled request", self.model_used(name))
                    return content, self.model_used(name)
                except Exception as exc:  # noqa: BLE001 - classified below by status
                    status = getattr(exc, "status_code", None)
                    errors.append(f"{name}:{status}:{exc}")
                    if status == 429 and provider.get("fallback_on_429"):
                        if "per day" in str(exc).lower() or "perday" in str(exc).lower():
                            # Daily quota gone: stop asking this provider for the rest of the run.
                            self._disabled.add(name)
                        logger.warning("provider %s rate-limited (429); using fallback", name)
                        break
                    retryable = status is None or status == 429 or status >= 500
                    if not retryable:
                        # Auth/quota/request errors will not fix themselves on a
                        # retry, but the fallback provider may still serve us.
                        if status in (401, 402, 403):
                            self._disabled.add(name)
                        logger.warning(
                            "provider %s failed with status %s; trying fallback",
                            name,
                            status,
                        )
                        break
                    if attempt < self._max_retries:
                        backoff = 2**attempt
                        logger.warning(
                            "retry %d/%d for %s in %ss (%s)",
                            attempt + 1,
                            self._max_retries,
                            name,
                            backoff,
                            exc,
                        )
                        self._sleeper(backoff)
            else:
                logger.warning("provider %s exhausted retries; trying fallback", name)
        raise LLMError("all providers failed: " + "; ".join(errors))

    @staticmethod
    def _request_extra(provider: dict[str, Any], json_mode: bool) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if json_mode and provider.get("response_format"):
            extra["response_format"] = provider["response_format"]
        if provider.get("reasoning_effort"):
            extra["reasoning_effort"] = provider["reasoning_effort"]
        if provider.get("extra_body"):
            extra["extra_body"] = provider["extra_body"]
        return extra


class MockLLMClient:
    """Deterministic stand-in for ``--dry-run`` and tests; no network calls.

    Marks every item as an open placement for the first canonical field it was
    asked about, so the rest of the pipeline can be exercised end to end.
    """

    def __init__(self, name: str = "mock") -> None:
        self._name = name
        self.calls = 0

    def complete(self, system: str, user: str, json_mode: bool = True) -> tuple[str, str]:
        self.calls += 1
        payload = json.loads(user)
        items = [
            {
                "id": item["id"],
                "isPlacementListing": True,
                "title": item.get("title") or "SIWES placement",
                "organization": f"Mock Org {abs(hash(item['url'])) % 10_000}",
                "location": "Lagos",
                "state": "Lagos",
                "fields": payload.get("fields_hint", [])[:1],
                "openToAllFields": False,
                "deadline": None,
                "postedDate": None,
                "howToApply": "Send your CV and SIWES letter by email.",
                "contact": None,
                "applyLink": item.get("url"),
                "scamWarning": False,
                "summary": "Mock listing for a dry run.",
            }
            for item in payload["items"]
        ]
        return json.dumps({"items": items}), self._name
