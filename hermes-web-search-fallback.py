"""Runtime fallback chain for Hermes' native web_search tool.

Configured with:
    web.search_backend: exa
    web.search_fallback_backends: [tavily, brave-free]

Extraction is untouched and continues to use web.extract_backend.
"""

import json
import logging

logger = logging.getLogger(__name__)


def _configured_fallbacks(web_tools):
    raw = web_tools._load_web_config().get("search_fallback_backends", [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [item.strip().lower() for item in raw if isinstance(item, str) and item.strip()]


def _install():
    import tools.web_tools as web_tools

    if getattr(web_tools, "_fallback_chain_installed", False):
        return

    original_search = web_tools.web_search_tool

    def search_with_fallback(query: str, limit: int = 5) -> str:
        primary_result = original_search(query, limit)
        try:
            decoded = json.loads(primary_result)
        except (TypeError, ValueError):
            return primary_result

        # Empty successful result is valid. Fail over only after an error.
        if decoded.get("success") is not False:
            return primary_result

        web_tools._ensure_web_plugins_loaded()
        from agent.web_search_registry import get_provider

        primary_backend = web_tools._get_search_backend()
        attempted = {primary_backend}
        for backend in _configured_fallbacks(web_tools):
            if backend in attempted:
                continue
            attempted.add(backend)

            provider = get_provider(backend)
            if provider is None or not provider.supports_search():
                logger.warning("Skipping invalid web-search fallback backend: %s", backend)
                continue
            try:
                if not provider.is_available():
                    logger.warning("Skipping unavailable web-search fallback backend: %s", backend)
                    continue
                logger.warning(
                    "Web search primary backend %s failed; retrying via %s",
                    primary_backend,
                    backend,
                )
                result = provider.search(query, limit)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Web-search fallback %s raised: %s", backend, exc)
                continue

            if isinstance(result, dict) and result.get("success") is True:
                return json.dumps(result, indent=2, ensure_ascii=False)

        return primary_result

    web_tools.web_search_tool = search_with_fallback
    web_tools._fallback_chain_installed = True


try:
    _install()
except Exception as exc:  # noqa: BLE001
    # Never prevent Hermes startup if this optional local policy hook is broken.
    logger.warning("Hermes web-search fallback hook was not installed: %s", exc)
