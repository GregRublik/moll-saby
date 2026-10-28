from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import count
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import ClientSession

from config import settings

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")


class SabyError(Exception):
    """Ошибка при работе с Saby."""


class SabyAuthError(SabyError):
    """Ошибка авторизации в Saby."""


class SabyApiError(SabyError):
    """JSON-RPC ошибка Saby."""

    def __init__(self, method: str, code: Any = None, message: str = ""):
        self.method = method
        self.code = code
        self.message = message
        super().__init__(f"{method}: {code} {message}".strip())


@dataclass(slots=True, frozen=True)
class SabyEvent:
    """Нормализованное событие отчётности Saby."""

    report_uuid: str
    bundle_uuid: str | None
    event_uuid: str
    event_time: datetime | None
    event_name: str | None
    event_group: str | None
    document_date: datetime | None
    document_type: str | None
    note: str | None
    state_code: str | None
    state_name: str | None
    subtype: str | None
    year: str | None
    period_code: str | None
    number_sent: str | None
    number_correction: str | None
    inn: str | None
    kpp: str | None
    document: dict[str, Any] = field(repr=False)
    event: dict[str, Any] = field(repr=False)


class SabyService:
    """Работа с api saby."""

    def __init__(self, http_session: ClientSession):
        self._session = http_session
        self._session_id: str | None = None
        self._auth_lock = asyncio.Lock()
        self._rpc_ids = count(1)

    # ------------------------------------------------------------------
    # Публичные методы
    # ------------------------------------------------------------------

    async def authenticate(self, *, force: bool = False) -> str:
        """Авторизуется по логину/паролю и кэширует идентификатор сессии."""
        async with self._auth_lock:
            if self._session_id and not force:
                return self._session_id

            if settings.saby.access_token:
                # В режиме access token отдельная сессия не нужна.
                return ""

            if not settings.saby.login or not settings.saby.password:
                raise SabyAuthError(
                    "не заданы APP_SABY_LOGIN/APP_SABY_PASSWORD или APP_SABY_ACCESS_TOKEN"
                )

            params: dict[str, Any] = {
                "Логин": settings.saby.login,
                "Пароль": settings.saby.password,
            }
            if settings.saby.account_number is not None:
                params["НомерАккаунта"] = settings.saby.account_number

            payload = {
                "jsonrpc": "2.0",
                "method": "СБИС.Аутентифицировать",
                "params": params,
                "id": next(self._rpc_ids),
            }
            last_error: Exception | None = None

            for attempt in range(settings.saby.max_retries + 1):
                try:
                    async with self._session.post(
                        settings.saby.auth_url,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=settings.saby.timeout),
                    ) as response:
                        raw = await response.read()
                        status = response.status
                        retry_after = self._retry_after(response)
                        body = self._parse_body(raw)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    last_error = exc
                    if attempt < settings.saby.max_retries:
                        await asyncio.sleep(self._delay(attempt))
                        continue
                    raise SabyError("Saby auth: сетевая ошибка после повторов") from exc

                if status == 429:
                    last_error = SabyError("Saby auth: HTTP 429")
                    if attempt < settings.saby.max_retries:
                        await asyncio.sleep(self._delay(attempt, retry_after))
                        continue
                    raise last_error

                error = body.get("error") or {}
                if error:
                    # Неверные учётные данные повторять бессмысленно.
                    raise SabyAuthError(
                        f"Saby auth: {error.get('code')} "
                        f"{str(error.get('message') or '').strip()}"
                    )

                if status >= 500:
                    last_error = SabyError(f"Saby auth: HTTP {status}")
                    if attempt < settings.saby.max_retries:
                        await asyncio.sleep(self._delay(attempt, retry_after))
                        continue
                    raise last_error

                if status != 200:
                    raise SabyAuthError(f"Saby auth: HTTP {status}")

                session_id = self._find_session_id(body.get("result"))
                if not session_id:
                    raise SabyAuthError("Saby auth: идентификатор сессии не найден в ответе")

                self._session_id = session_id
                logger.info("Saby: сессия авторизована")
                return session_id

            raise SabyError("Saby auth: повторы исчерпаны") from last_error

    async def iter_events(
        self,
        date_from: datetime,
        date_to: datetime,
        *,
        page_size: int | None = None,
    ) -> AsyncIterator[SabyEvent]:
        """Итерирует события отчётности за период, обходя все страницы."""
        filter_ = self._build_filter(date_from, date_to, page_size)
        async for event in self._iter_events_from_pages(self._iter_pages(filter_)):
            yield event

    async def list_events(
        self,
        date_from: datetime,
        date_to: datetime,
        *,
        page_size: int | None = None,
    ) -> list[SabyEvent]:
        """Возвращает все события отчётности за период."""
        return [
            event
            async for event in self.iter_events(date_from, date_to, page_size=page_size)
        ]

    async def list_document_events(
        self,
        document_uuid: str,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[SabyEvent]:
        """Возвращает события одного документа (для reconcile)."""
        if not document_uuid:
            raise ValueError("document_uuid must not be empty")

        date_to = date_to or datetime.now(tz=MSK)
        date_from = date_from or (date_to - timedelta(days=30))

        filter_ = self._build_filter(date_from, date_to, None)
        filter_["ИдентификаторДокумента"] = document_uuid
        return [
            event
            async for event in self._iter_events_from_pages(self._iter_pages(filter_))
        ]

    async def read_document(self, document_uuid: str) -> dict[str, Any]:
        """Читает карточку/отчёт Saby по UUID."""
        if not document_uuid:
            raise ValueError("document_uuid must not be empty")

        result = await self._rpc(
            "СБИС.ПрочитатьДокумент",
            {
                "Документ": {
                    "Идентификатор": document_uuid,
                    "ДопПоля": "ДополнительныеПоля",
                }
            },
        )
        document = result.get("Документ")
        if document is None:
            document = result
        if not isinstance(document, dict):
            raise SabyApiError(
                "СБИС.ПрочитатьДокумент", None, "неожиданный формат ответа"
            )
        return document

    async def get_document_state(
        self, document_uuid: str
    ) -> tuple[str | None, str | None]:
        """Возвращает (код, название) текущего состояния документа."""
        document = await self.read_document(document_uuid)
        state = document.get("Состояние") or {}
        code = state.get("Код")
        return (str(code) if code is not None else None, state.get("Название"))

    # ------------------------------------------------------------------
    # Период и фильтры
    # ------------------------------------------------------------------

    def _build_filter(
        self,
        date_from: datetime,
        date_to: datetime,
        page_size: int | None,
    ) -> dict[str, Any]:
        date_from = self._as_msk(date_from)
        date_to = self._as_msk(date_to)
        if date_to <= date_from:
            raise ValueError("date_to must be greater than date_from")

        if not settings.saby.inn or not settings.saby.kpp:
            raise ValueError("APP_SABY_INN and APP_SABY_KPP must be configured")

        page_size = page_size or settings.saby.page_size
        page_size = max(1, min(int(page_size), 50))

        return {
            "ДатаВремяС": self.format_datetime(date_from),
            "ДатаВремяПо": self.format_datetime(date_to),
            "ТолькоОтчетность": "Да",
            "НашаОрганизация": {
                "СвЮЛ": {"ИНН": settings.saby.inn, "КПП": settings.saby.kpp}
            },
            "Навигация": {"РазмерСтраницы": str(page_size)},
        }

    @classmethod
    def format_datetime(cls, value: datetime) -> str:
        """Форматирует время в московской зоне для Saby."""
        return cls._as_msk(value).strftime("%d.%m.%Y %H.%M.%S")

    @classmethod
    def parse_datetime(cls, value: str | datetime | None) -> datetime | None:
        """Разбирает время Saby (московская зона)."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return cls._as_msk(value)

        text = str(value).strip()
        if not text:
            return None

        for fmt in ("%d.%m.%Y %H.%M.%S", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=MSK)
            except ValueError:
                continue

        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Unsupported Saby datetime: {text!r}") from exc
        return cls._as_msk(parsed)

    @staticmethod
    def _as_msk(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=MSK)
        return value.astimezone(MSK)

    # ------------------------------------------------------------------
    # Пагинация и нормализация
    # ------------------------------------------------------------------

    async def _iter_pages(
        self, filter_: dict[str, Any]
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Обходит страницы СБИС.СписокИзменений по ИдентификаторСобытия."""
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page = 0

        while True:
            if cursor:
                filter_["ИдентификаторСобытия"] = cursor

            result = await self._rpc("СБИС.СписокИзменений", {"Фильтр": filter_})
            documents = result.get("Документ") or []
            if not isinstance(documents, list):
                raise SabyApiError(
                    "СБИС.СписокИзменений", None, "Документ is not a list"
                )

            page += 1
            yield documents

            navigation = result.get("Навигация") or {}
            if str(navigation.get("ЕстьЕще") or "").strip().lower() != "да":
                return

            next_cursor = self._last_event_id(documents)
            if not next_cursor:
                raise SabyApiError(
                    "СБИС.СписокИзменений",
                    None,
                    "ЕстьЕще=Да, но нет идентификатора события",
                )
            if next_cursor in seen_cursors:
                raise SabyApiError(
                    "СБИС.СписокИзменений", None, "курсор пагинации повторился"
                )

            seen_cursors.add(next_cursor)
            cursor = next_cursor

            if page >= settings.saby.max_pages:
                raise SabyApiError(
                    "СБИС.СписокИзменений", None, "превышен лимит страниц"
                )

    async def _iter_events_from_pages(
        self, pages: AsyncIterator[list[dict[str, Any]]]
    ) -> AsyncIterator[SabyEvent]:
        seen: set[tuple[str, str]] = set()
        async for documents in pages:
            for document in documents:
                report_uuid = self._str_or_none(document.get("Идентификатор"))
                if not report_uuid:
                    continue
                for event in self._as_list(document.get("Событие")):
                    event_uuid = self._str_or_none(event.get("Идентификатор"))
                    if not event_uuid:
                        continue
                    key = (report_uuid, event_uuid)
                    if key in seen:
                        continue
                    seen.add(key)
                    yield self._to_event(report_uuid, document, event)

    def _to_event(
        self,
        report_uuid: str,
        document: dict[str, Any],
        event: dict[str, Any],
    ) -> SabyEvent:
        extension = document.get("Расширение") or {}
        state = document.get("Состояние") or {}
        organization = (document.get("НашаОрганизация") or {}).get("СвЮЛ") or {}
        event_uuid = str(event.get("Идентификатор"))

        try:
            event_time = self.parse_datetime(event.get("ДатаВремя"))
        except ValueError:
            logger.warning("Saby: не разобрано время события отчёта %s", report_uuid)
            event_time = None

        try:
            document_date = self.parse_datetime(document.get("Дата"))
        except ValueError:
            logger.warning("Saby: не разобрана дата документа %s", report_uuid)
            document_date = None

        state_code = state.get("Код")
        return SabyEvent(
            report_uuid=report_uuid,
            bundle_uuid=self._str_or_none(extension.get("ИдентификаторКомплекта")),
            event_uuid=event_uuid,
            event_time=event_time,
            event_name=self._str_or_none(event.get("Название")),
            event_group=self._str_or_none(event.get("Группа")),
            document_date=document_date,
            document_type=self._str_or_none(document.get("Тип")),
            note=self._str_or_none(document.get("Примечание")),
            state_code=str(state_code) if state_code is not None else None,
            state_name=self._str_or_none(state.get("Название")),
            subtype=self._str_or_none(extension.get("ПодТип")),
            year=self._str_or_none(extension.get("Год")),
            period_code=self._str_or_none(extension.get("КодПериода")),
            number_sent=self._str_or_none(extension.get("НомерОтправки")),
            number_correction=self._str_or_none(extension.get("НомерКорректировки")),
            inn=self._str_or_none(organization.get("ИНН")),
            kpp=self._str_or_none(organization.get("КПП")),
            document=document,
            event=event,
        )

    @classmethod
    def _last_event_id(cls, documents: list[dict[str, Any]]) -> str | None:
        for document in reversed(documents):
            for event in reversed(cls._as_list(document.get("Событие"))):
                event_uuid = cls._str_or_none(event.get("Идентификатор"))
                if event_uuid:
                    return event_uuid
        return None

    @staticmethod
    def _as_list(value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
        return []

    @staticmethod
    def _str_or_none(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    # ------------------------------------------------------------------
    # Транспорт и авторизация
    # ------------------------------------------------------------------

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Выполняет JSON-RPC запрос с повторами и переавторизацией."""
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": next(self._rpc_ids),
        }
        auth_retried = False
        last_error: Exception | None = None

        for attempt in range(settings.saby.max_retries + 1):
            headers = {
                "Content-Type": "application/json",
                "X-API-Version": settings.saby.api_version,
            }
            headers.update(await self._auth_headers())

            try:
                async with self._session.post(
                    settings.saby.service_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=settings.saby.timeout),
                ) as response:
                    raw = await response.read()
                    status = response.status
                    retry_after = self._retry_after(response)
                    body = self._parse_body(raw)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < settings.saby.max_retries:
                    await asyncio.sleep(self._delay(attempt))
                    continue
                raise SabyError(f"{method}: сетевая ошибка после повторов") from exc

            if status == 401 or self._is_auth_error(status, body):
                if settings.saby.access_token:
                    raise SabyAuthError(f"{method}: access token отклонён")
                if auth_retried:
                    raise SabyAuthError(
                        f"{method}: сессия отклонена после переавторизации"
                    )
                auth_retried = True
                self._session_id = None
                await self.authenticate(force=True)
                continue

            if status == 429 or status >= 500:
                error = body.get("error") or {}
                last_error = SabyApiError(
                    method,
                    error.get("code"),
                    str(error.get("message") or f"HTTP {status}"),
                )
                if attempt < settings.saby.max_retries:
                    await asyncio.sleep(self._delay(attempt, retry_after))
                    continue
                raise last_error

            error = body.get("error") or {}
            if error:
                raise SabyApiError(
                    method,
                    error.get("code"),
                    str(error.get("message") or ""),
                )

            if status >= 400:
                raise SabyError(f"{method}: HTTP {status}")

            result = body.get("result")
            if not isinstance(result, dict):
                raise SabyError(f"{method}: неожиданный формат result")
            return result

        raise SabyError(f"{method}: повторы исчерпаны") from last_error

    async def _auth_headers(self) -> dict[str, str]:
        if settings.saby.access_token:
            return {"X-SBISAccessToken": settings.saby.access_token}

        if not self._session_id:
            await self.authenticate()
        if not self._session_id:
            raise SabyAuthError("Saby: сессия не установлена")
        return {"X-SBISSessionID": self._session_id}

    @staticmethod
    def _parse_body(raw: bytes) -> dict[str, Any]:
        text = SabyService._decode(raw)
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _decode(raw: bytes) -> str:
        for encoding in ("utf-8", "cp1251"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")

    @staticmethod
    def _is_auth_error(status: int, body: dict[str, Any]) -> bool:
        if status == 401:
            return True
        error = body.get("error") or {}
        message = str(error.get("message") or "").lower()
        return any(
            marker in message
            for marker in ("сесси", "авториз", "логин", "парол", "session")
        )

    @classmethod
    def _find_session_id(cls, value: Any) -> str | None:
        if isinstance(value, str):
            text = value.strip()
            return text or None
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).lower()
                if (
                    ("сесси" in normalized or "session" in normalized)
                    and isinstance(item, (str, int))
                ):
                    text = str(item).strip()
                    if text:
                        return text
            for item in value.values():
                found = cls._find_session_id(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = cls._find_session_id(item)
                if found:
                    return found
        return None

    @staticmethod
    def _retry_after(response: aiohttp.ClientResponse) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None

    @staticmethod
    def _delay(attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(retry_after, settings.saby.retry_max_delay)
        base = min(
            settings.saby.retry_base_delay * (2**attempt),
            settings.saby.retry_max_delay,
        )
        return base + random.uniform(0, 0.5)
