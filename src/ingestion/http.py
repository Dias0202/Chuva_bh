"""Download HTTPS com timeout e retry exponencial limitado."""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Final

logger = logging.getLogger(__name__)

Fetch = Callable[[str], bytes]

USER_AGENT: Final = "chuva-bh/0.1"
RETRYABLE_STATUS: Final = frozenset({429, 500, 502, 503, 504})
DEFAULT_TIMEOUT_SECONDS: Final = 30
DEFAULT_ATTEMPTS: Final = 4
BASE_DELAY_SECONDS: Final = 1.0
MAX_DELAY_SECONDS: Final = 30.0


class FetchError(RuntimeError):
    """Falha definitiva ao baixar uma URL."""


def get_bytes(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    attempts: int = DEFAULT_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    label: str | None = None,
) -> bytes:
    """Baixa a URL, repetindo apenas erros transitórios.

    `label` substitui a URL nas mensagens de erro, para URLs que carregam credenciais.
    """
    shown = label or url
    if not url.startswith("https://"):
        raise ValueError("somente URLs https são aceitas")
    if attempts < 1:
        raise ValueError("attempts deve ser ao menos 1")

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    last_error: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            delay = min(BASE_DELAY_SECONDS * 2 ** (attempt - 1), MAX_DELAY_SECONDS)
            sleep(delay)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                body: bytes = response.read()
                return body
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_STATUS:
                raise FetchError(f"HTTP {error.code} em {shown}") from error
            last_error = error
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
        logger.warning("Tentativa %d de %d falhou: %s", attempt + 1, attempts, last_error)
    raise FetchError(f"Sem resposta de {shown} após {attempts} tentativas") from last_error
