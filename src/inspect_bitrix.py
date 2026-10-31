"""Диагностика полей сопоставления Bitrix без записи данных.

Показывает метаданные полей «Форма», «Период», «Получатель» и несколько
карточек. Запускать в терминале, откуда доступен портал Bitrix:

    PYTHONPATH=src python -m inspect_bitrix
"""

from __future__ import annotations

import asyncio
from typing import Any

from config import settings
from depends import get_bitrix_service
from utils.session_manager import SessionManager


def _short(value: Any, limit: int = 120) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _field_items(meta: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "enum", "list"):
        items = meta.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


async def main() -> None:
    bitrix = await get_bitrix_service()
    try:
        fields = await bitrix.get_fields(settings.bitrix.entity_type_id)
        configured = (
            ("Форма", settings.bitrix.form_field),
            ("Период", settings.bitrix.period_field),
            ("Получатель", settings.bitrix.recipient_field),
            ("UUID Saby", settings.bitrix.saby_uuid_field),
        )
        for label, name in configured:
            if not name:
                print(f"{label}: не настроено")
                continue
            meta = fields.get(name)
            if not isinstance(meta, dict):
                print(f"{label}: поле {name} не найдено")
                continue
            items = _field_items(meta)
            print(f"\n{label}: {name}")
            print(f"  title: {meta.get('title')!r}")
            print(f"  type: {meta.get('type')!r}")
            print(f"  settings: {_short(meta.get('settings'))}")
            print(f"  items: {len(items)}")
            for item in items[:20]:
                print(f"    {item.get('ID')!r} -> {item.get('VALUE')!r}")

        select = [
            name
            for name in (
                "id",
                "title",
                "stageId",
                settings.bitrix.form_field,
                settings.bitrix.period_field,
                settings.bitrix.recipient_field,
            )
            if name
        ]
        items = await bitrix.list_all_items(
            settings.bitrix.entity_type_id,
            filter={"categoryId": settings.bitrix.category_id},
            select=select,
            order={"id": "ASC"},
        )
        print(f"\nЭлементов: {len(items)}")
        for item in items[:10]:
            print(
                f"  id={item.get('id')} title={item.get('title')!r} "
                f"form={_short(item.get(settings.bitrix.form_field or ''))} "
                f"period={_short(item.get(settings.bitrix.period_field or ''))} "
                f"recipient={_short(item.get(settings.bitrix.recipient_field or ''))}"
            )
    finally:
        await SessionManager.close_session()


if __name__ == "__main__":
    asyncio.run(main())
