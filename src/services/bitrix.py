from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
from aiohttp import ClientSession

from config import settings

# Ошибки Bitrix, которые не имеет смысла повторять.
AUTH_ERROR_CODES = frozenset(
    {
        "INVALID_CREDENTIALS",
        "NO_AUTH_FOUND",
        "INVALID_TOKEN",
        "ACCESS_DENIED",
        "INSUFFICIENT_SCOPE",
        "USER_NOT_FOUND",
    }
)

NOT_FOUND_ERROR_CODES = frozenset(
    {
        "NOT_FOUND",
        "ERROR_NOT_FOUND",
        "CRM_ITEM_NOT_FOUND",
    }
)

# Лимиты и временные сбои Bitrix — повторяем с backoff.
RETRYABLE_ERROR_CODES = frozenset(
    {
        "QUERY_LIMIT_EXCEEDED",
        "OPERATION_TIME_LIMIT",
        "OVERLOAD_LIMIT",
        "INTERNAL_SERVER_ERROR",
        "ERROR_INTERNAL",
    }
)


class BitrixError(Exception):
    """Ошибка при работе с Bitrix24."""


class BitrixTransportError(BitrixError):
    """Сетевая ошибка или таймаут."""


class BitrixApiError(BitrixError):
    """Ошибка REST-метода Bitrix24."""

    def __init__(self, method: str, code: Any = None, description: str = ""):
        self.method = method
        self.code = code
        self.description = description
        super().__init__(f"{method}: {code} {description}".strip())


class BitrixAuthError(BitrixApiError):
    """Ошибка авторизации/прав вебхука."""


class BitrixNotFoundError(BitrixApiError):
    """Элемент или сущность не найдены."""


class BitrixService:
    """Работа с api bitrix."""

    def __init__(self, http_session: ClientSession):
        self._session = http_session

    # ------------------------------------------------------------------
    # Смарт-процессы: типы, поля, воронки, стадии
    # ------------------------------------------------------------------

    async def list_types(self) -> list[dict[str, Any]]:
        """Возвращает все типы смарт-процессов (crm.type.list)."""
        return [item async for item in self.iter_types()]

    async def iter_types(self) -> AsyncIterator[dict[str, Any]]:
        """Итерирует типы смарт-процессов постранично."""
        async for item in self._iter_pages("crm.type.list", {}, "types"):
            yield item

    async def get_fields(
        self,
        entity_type_id: int,
        *,
        use_original_uf_names: bool = False,
    ) -> dict[str, Any]:
        """Возвращает описание полей смарт-процесса по их реальным кодам."""
        params: dict[str, Any] = {"entityTypeId": self._int_id(entity_type_id)}
        if use_original_uf_names:
            params["useOriginalUfNames"] = "Y"

        result = await self._call("crm.item.fields", params)
        if isinstance(result, dict) and isinstance(result.get("fields"), dict):
            return result["fields"]
        raise BitrixError("crm.item.fields: поля не найдены в ответе")

    async def list_categories(self, entity_type_id: int) -> list[dict[str, Any]]:
        """Возвращает все воронки смарт-процесса."""
        return [item async for item in self.iter_categories(entity_type_id)]

    async def iter_categories(
        self, entity_type_id: int
    ) -> AsyncIterator[dict[str, Any]]:
        """Итерирует воронки смарт-процесса постранично."""
        params = {"entityTypeId": self._int_id(entity_type_id)}
        async for item in self._iter_pages("crm.category.list", params, "categories"):
            yield item

    async def list_statuses(
        self,
        entity_type_id: int,
        category_id: int,
    ) -> list[dict[str, Any]]:
        """Возвращает стадии воронки смарт-процесса."""
        entity_id = (
            f"DYNAMIC_{self._int_id(entity_type_id)}"
            f"_STAGE_{self._int_id(category_id, allow_zero=True)}"
        )
        result = await self._call(
            "crm.status.list",
            {
                "filter": {"ENTITY_ID": entity_id},
                "order": {"SORT": "ASC"},
            },
        )
        return self._extract_list(result, "statuses")

    # ------------------------------------------------------------------
    # Элементы смарт-процесса
    # ------------------------------------------------------------------

    async def list_items(
        self,
        entity_type_id: int,
        *,
        filter: dict[str, Any] | None = None,
        select: list[str] | None = None,
        order: dict[str, str] | None = None,
        start: int | None = None,
    ) -> list[dict[str, Any]]:
        """Возвращает одну страницу элементов (до 50)."""
        items, _ = await self._list_items_page(
            entity_type_id,
            filter=filter,
            select=select,
            order=order,
            start=start,
        )
        return items

    async def iter_items(
        self,
        entity_type_id: int,
        *,
        filter: dict[str, Any] | None = None,
        select: list[str] | None = None,
        order: dict[str, str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Итерирует все элементы смарт-процесса постранично."""
        params: dict[str, Any] = {"entityTypeId": self._int_id(entity_type_id)}
        if filter is not None:
            params["filter"] = filter
        if select is not None:
            params["select"] = list(select)
        if order is not None:
            params["order"] = order

        async for item in self._iter_pages("crm.item.list", params, "items"):
            yield item

    async def list_all_items(
        self,
        entity_type_id: int,
        *,
        filter: dict[str, Any] | None = None,
        select: list[str] | None = None,
        order: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Возвращает все элементы, обходя пагинацию."""
        return [
            item
            async for item in self.iter_items(
                entity_type_id,
                filter=filter,
                select=select,
                order=order,
            )
        ]

    async def get_item(
        self,
        entity_type_id: int,
        item_id: int,
    ) -> dict[str, Any]:
        """Читает элемент смарт-процесса (crm.item.get)."""
        result = await self._call(
            "crm.item.get",
            {
                "entityTypeId": self._int_id(entity_type_id),
                "id": self._int_id(item_id),
            },
        )
        if isinstance(result, dict) and isinstance(result.get("item"), dict):
            return result["item"]
        raise BitrixError("crm.item.get: элемент не найден в ответе")

    async def update_item(
        self,
        entity_type_id: int,
        item_id: int,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        """Обновляет только переданные поля. После записи нужен повторный get."""
        if not fields:
            raise ValueError("fields must not be empty")

        result = await self._call(
            "crm.item.update",
            {
                "entityTypeId": self._int_id(entity_type_id),
                "id": self._int_id(item_id),
                "fields": fields,
            },
        )
        if isinstance(result, dict) and isinstance(result.get("item"), dict):
            return result["item"]
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------
    # Транспорт
    # ------------------------------------------------------------------

    @staticmethod
    def _url(method: str) -> str:
        base_url = settings.bitrix.base_url.rstrip("/")
        method = method.strip().lstrip("/")
        return (
            f"{base_url}/rest/{settings.bitrix.id_user}"
            f"/{settings.bitrix.key_201}/{method}.json"
        )

    async def _list_items_page(
        self,
        entity_type_id: int,
        *,
        filter: dict[str, Any] | None = None,
        select: list[str] | None = None,
        order: dict[str, str] | None = None,
        start: int | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        params: dict[str, Any] = {"entityTypeId": self._int_id(entity_type_id)}
        if filter is not None:
            params["filter"] = filter
        if select is not None:
            params["select"] = list(select)
        if order is not None:
            params["order"] = order
        if start is not None:
            params["start"] = self._int_id(start, allow_zero=True)

        result, meta = await self._call_with_meta("crm.item.list", params)
        return self._extract_list(result, "items"), self._next_start(meta)

    async def _iter_pages(
        self,
        method: str,
        params: dict[str, Any],
        key: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Обходит страницы списочного метода по top-level next."""
        start = 0
        seen_starts: set[int] = set()

        while True:
            page_params = dict(params)
            page_params["start"] = start
            result, meta = await self._call_with_meta(method, page_params)

            for item in self._extract_list(result, key):
                yield item

            next_start = self._next_start(meta)
            if next_start is None:
                return
            if next_start <= start or next_start in seen_starts:
                raise BitrixError(f"{method}: пагинация зациклилась")

            seen_starts.add(next_start)
            start = next_start

    @staticmethod
    def _next_start(meta: dict[str, Any]) -> int | None:
        raw_next = meta.get("next")
        if isinstance(raw_next, int):
            return raw_next
        if isinstance(raw_next, str) and raw_next.strip().isdigit():
            return int(raw_next)
        return None

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Выполняет REST-метод с повторами на 429/5xx и таймаутах."""
        result, _ = await self._call_with_meta(method, params)
        return result

    async def _call_with_meta(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """Возвращает result и полный ответ (для next/total)."""
        payload = params or {}
        last_error: Exception | None = None

        for attempt in range(settings.bitrix.max_retries + 1):
            try:
                async with self._session.post(
                    self._url(method),
                    json=payload,
                    headers={"Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=settings.bitrix.timeout),
                ) as response:
                    status = response.status
                    retry_after = self._retry_after(response)
                    raw = await response.read()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt < settings.bitrix.max_retries:
                    await asyncio.sleep(self._delay(attempt))
                    continue
                # Не прицепляем исходное исключение: в URL вебхука есть токен.
                raise BitrixTransportError(
                    f"{method}: сетевая ошибка после повторов ({type(exc).__name__})"
                ) from None

            body = self._parse(raw)
            error_code, error_description = self._error(body)

            if error_code:
                if error_code in AUTH_ERROR_CODES or status in (401, 403):
                    raise BitrixAuthError(method, error_code, error_description)
                if error_code in NOT_FOUND_ERROR_CODES:
                    raise BitrixNotFoundError(method, error_code, error_description)
                if (
                    error_code in RETRYABLE_ERROR_CODES
                    or status == 429
                    or status >= 500
                ):
                    last_error = BitrixApiError(
                        method, error_code, error_description
                    )
                    if attempt < settings.bitrix.max_retries:
                        await asyncio.sleep(self._delay(attempt, retry_after))
                        continue
                    raise last_error
                raise BitrixApiError(method, error_code, error_description)

            if status == 429 or status >= 500:
                last_error = BitrixApiError(method, status, f"HTTP {status}")
                if attempt < settings.bitrix.max_retries:
                    await asyncio.sleep(self._delay(attempt, retry_after))
                    continue
                raise last_error

            if status >= 400:
                detail = ""
                if isinstance(body, dict):
                    detail = str(body.get("error_description") or "")
                suffix = f": {detail}" if detail else ""
                raise BitrixError(f"{method}: HTTP {status}{suffix}")

            if not isinstance(body, dict) or "result" not in body:
                raise BitrixError(f"{method}: неожиданный формат ответа")

            return body["result"], body

        raise BitrixError(f"{method}: повторы исчерпаны") from None

    @staticmethod
    def _parse(raw: bytes) -> dict[str, Any] | None:
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return body if isinstance(body, dict) else None

    @staticmethod
    def _error(body: dict[str, Any] | None) -> tuple[str | None, str]:
        if not body:
            return None, ""
        code = body.get("error")
        if not code:
            return None, ""
        return str(code), str(body.get("error_description") or "")

    @staticmethod
    def _extract_list(result: Any, key: str) -> list[dict[str, Any]]:
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if isinstance(result, dict):
            value = result.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _int_id(value: Any, *, allow_zero: bool = False) -> int:
        number = int(value)
        if number < 0 or (number == 0 and not allow_zero):
            raise ValueError(f"invalid id: {value!r}")
        return number

    @staticmethod
    def _retry_after(response: aiohttp.ClientResponse) -> float | None:
        for header in ("Retry-After", "X-RateLimit-Reset"):
            value = response.headers.get(header)
            if not value:
                continue
            try:
                seconds = float(value)
            except ValueError:
                continue
            # X-RateLimit-Reset встречается и как unix-время, и как секунды.
            if header == "X-RateLimit-Reset" and seconds > 1_000_000_000:
                seconds -= time.time()
            return max(0.0, seconds)
        return None

    @staticmethod
    def _delay(attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(retry_after, settings.bitrix.retry_max_delay)
        base = min(
            settings.bitrix.retry_base_delay * (2**attempt),
            settings.bitrix.retry_max_delay,
        )
        return base + random.uniform(0, 0.5)
