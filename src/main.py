from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from config import settings
from depends import get_bitrix_service, get_saby_service
from services.bitrix import BitrixError, BitrixService
from services.saby import SabyError, SabyEvent, SabyService
from utils.session_manager import SessionManager

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")
MIN_DT = datetime.min.replace(tzinfo=MSK)

MONTH_NAMES = (
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)

# Подтверждённые соответствия из ТЗ. Новые строки добавлять только после
# согласования с бухгалтерией; неизвестные значения уходят в исключения.
FORM_BY_TYPE_SUBTYPE: dict[tuple[str, str], int] = {
    ("ОтчетФНС", "1151162"): 22050,
}
RECIPIENT_BY_TYPE: dict[str, int] = {
    "ОтчетФНС": 3601,
}


@dataclass(slots=True)
class Report:
    """Отчёт Saby, собранный из событий одного UUID."""

    report_uuid: str
    bundle_uuid: str | None
    document_type: str | None
    subtype: str | None
    year: str | None
    period_code: str | None
    inn: str | None
    kpp: str | None
    title: str | None
    note: str | None
    state_code: str | None
    state_name: str | None
    document_date: datetime | None
    number_sent: str | None
    number_correction: str | None
    events: list[SabyEvent] = field(repr=False)
    fact_date: datetime | None = None
    submitted: bool = False


@dataclass(slots=True)
class BitrixContext:
    """Прочитанная конфигурация смарт-процесса и его элементы."""

    entity_type_id: int
    category_id: int
    fields: dict[str, Any]
    stages: dict[str, str]
    items: list[dict[str, Any]]
    form_ready: bool
    uuid_ready: bool
    missing_fields: list[str]


@dataclass(slots=True)
class Plan:
    """Планируемые изменения одной карточки."""

    changes: dict[str, Any] = field(default_factory=dict)
    blocked_reason: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Summary:
    """Итоги одного запуска."""

    events: int = 0
    reports: int = 0
    submitted: int = 0
    matched: int = 0
    planned: int = 0
    updated: int = 0
    exceptions: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# Saby: сбор отчётов и статусов
# ----------------------------------------------------------------------


def build_reports(events: Sequence[SabyEvent]) -> list[Report]:
    """Группирует события по UUID отчёта и определяет текущий статус."""
    grouped: dict[str, list[SabyEvent]] = {}

    for event in events:
        if not _is_report_type(event.document_type):
            continue
        if event.inn != settings.saby.inn or event.kpp != settings.saby.kpp:
            continue
        grouped.setdefault(event.report_uuid, []).append(event)

    reports: list[Report] = []
    for report_uuid, report_events in grouped.items():
        report_events.sort(key=lambda item: item.event_time or MIN_DT)
        latest = report_events[-1]

        report = Report(
            report_uuid=report_uuid,
            bundle_uuid=latest.bundle_uuid,
            document_type=latest.document_type,
            subtype=latest.subtype,
            year=latest.year,
            period_code=latest.period_code,
            inn=latest.inn,
            kpp=latest.kpp,
            title=latest.document_title,
            note=latest.note,
            state_code=latest.state_code,
            state_name=latest.state_name,
            document_date=latest.document_date,
            number_sent=latest.number_sent,
            number_correction=latest.number_correction,
            events=report_events,
        )
        report.fact_date = _positive_protocol_date(report_events)
        report.submitted = report.state_code == "7"
        reports.append(report)

    reports.sort(key=lambda item: (item.document_date or MIN_DT, item.report_uuid))
    return reports


def _is_report_type(document_type: str | None) -> bool:
    return bool(document_type) and document_type.startswith("Отчет")


def _positive_protocol_date(events: Sequence[SabyEvent]) -> datetime | None:
    """Дата положительного протокола — только она считается фактом сдачи."""
    dates: list[datetime] = []

    for event in events:
        group = event.event.get("Группа")
        group = group if isinstance(group, dict) else {}
        group_name = event.event_group or group.get("Название")
        description = (event.event_description or "").lower()

        if group_name != "Протокол" and event.event_group_code != "2":
            continue
        if not description.startswith("положительн"):
            continue

        parsed = None
        try:
            parsed = SabyService.parse_datetime(group.get("ДатаВремя"))
        except ValueError:
            parsed = None
        dates.append(parsed or event.event_time)

    dates = [value for value in dates if value is not None]
    return max(dates) if dates else None


# ----------------------------------------------------------------------
# Bitrix: конфигурация и элементы
# ----------------------------------------------------------------------


async def load_bitrix_context(bitrix: BitrixService) -> BitrixContext:
    """Читает поля, стадии и карточки смарт-процесса."""
    entity_type_id = settings.bitrix.entity_type_id
    category_id = settings.bitrix.category_id

    fields = await bitrix.get_fields(entity_type_id)
    stages_list = await bitrix.list_statuses(entity_type_id, category_id)
    stages = {
        str(item.get("STATUS_ID")): str(item.get("NAME") or "")
        for item in stages_list
        if item.get("STATUS_ID")
    }

    required = (
        settings.bitrix.form_field,
        settings.bitrix.period_field,
        settings.bitrix.recipient_field,
    )
    missing = [name for name in required if not name or name not in fields]
    form_ready = not missing

    uuid_field = settings.bitrix.saby_uuid_field
    uuid_ready = bool(uuid_field and uuid_field in fields)

    items: list[dict[str, Any]] = []
    if form_ready or uuid_ready:
        items = await bitrix.list_all_items(
            entity_type_id,
            filter={"categoryId": category_id},
            select=_item_select(fields),
            order={"id": "ASC"},
        )

    logger.info(
        "Bitrix: полей=%s, стадий=%s, элементов=%s",
        len(fields),
        len(stages),
        len(items),
    )
    if missing:
        logger.warning("Bitrix: нет полей сопоставления: %s", ", ".join(missing))

    return BitrixContext(
        entity_type_id=entity_type_id,
        category_id=category_id,
        fields=fields,
        stages=stages,
        items=items,
        form_ready=form_ready,
        uuid_ready=uuid_ready,
        missing_fields=missing,
    )


def _item_select(fields: dict[str, Any]) -> list[str]:
    select = ["id", "title", "stageId", "categoryId"]
    for name in (
        settings.bitrix.form_field,
        settings.bitrix.period_field,
        settings.bitrix.recipient_field,
        settings.bitrix.fact_date_field,
        settings.bitrix.saby_uuid_field,
        settings.bitrix.saby_status_code_field,
        settings.bitrix.saby_status_field,
        settings.bitrix.saby_checked_field,
    ):
        if name and name in fields and name not in select:
            select.append(name)
    return select


def find_candidates(
    report: Report,
    context: BitrixContext,
) -> tuple[list[dict[str, Any]], str | None]:
    """Ищет карточку по сохранённой связи или по точному составному ключу."""
    uuid_field = settings.bitrix.saby_uuid_field
    if context.uuid_ready and uuid_field:
        exact = [
            item
            for item in context.items
            if _same_simple(item.get(uuid_field), report.report_uuid)
        ]
        if exact:
            return exact, None

    if not context.form_ready:
        return [], (
            "в Bitrix нет полей сопоставления: "
            + ", ".join(context.missing_fields)
        )

    key, reason = _match_key(report)
    if key is None:
        return [], reason or "не удалось построить ключ сопоставления"

    form_id, recipient_id, period = key
    candidates = [
        item
        for item in context.items
        if _same_simple(item.get(settings.bitrix.form_field), form_id)
        and _same_simple(item.get(settings.bitrix.recipient_field), recipient_id)
        and _same_simple(item.get(settings.bitrix.period_field), period)
    ]
    return candidates, None


def _match_key(report: Report) -> tuple[tuple[int, int, str] | None, str | None]:
    if not report.document_type or not report.subtype:
        return None, "в отчёте нет типа/подтипа Saby"

    form_id = FORM_BY_TYPE_SUBTYPE.get((report.document_type, report.subtype))
    if form_id is None:
        return None, (
            f"нет соответствия формы для {report.document_type}/{report.subtype}"
        )

    recipient_id = RECIPIENT_BY_TYPE.get(report.document_type)
    if recipient_id is None:
        return None, f"нет получателя для типа {report.document_type}"

    period = _period_label(report.year, report.period_code)
    if period is None:
        return None, (
            f"нет соответствия периода для {report.year}/{report.period_code}"
        )

    return (form_id, recipient_id, period), None


def _period_label(year: str | None, period_code: str | None) -> str | None:
    if not year or not period_code:
        return None
    try:
        year_int = int(year)
        month = int(period_code)
    except (TypeError, ValueError):
        return None
    if not 1 <= month <= 12:
        return None
    return f"{MONTH_NAMES[month - 1]}'{year_int % 100:02d}"


# ----------------------------------------------------------------------
# Решение об изменении карточки
# ----------------------------------------------------------------------


def plan_changes(
    report: Report,
    item: dict[str, Any],
    context: BitrixContext,
    now: datetime,
) -> Plan:
    """Сравнивает карточку с отчётом и возвращает только реальные изменения."""
    if report.number_correction not in (None, "", "0"):
        return Plan(
            blocked_reason=(
                f"корректировка №{report.number_correction}: "
                "политика обработки не утверждена"
            )
        )

    if _as_int(report.number_sent) > 1:
        return Plan(
            blocked_reason=(
                f"повторная отправка №{report.number_sent}: "
                "политика обработки не утверждена"
            )
        )

    if report.state_code is None:
        return Plan(blocked_reason="не удалось определить код состояния Saby")

    changes: dict[str, Any] = {}
    notes: list[str] = []

    uuid_field = settings.bitrix.saby_uuid_field
    if uuid_field and uuid_field in context.fields:
        current_uuid = item.get(uuid_field)
        if current_uuid and not _same_simple(current_uuid, report.report_uuid):
            return Plan(
                blocked_reason=(
                    f"в карточке уже UUID {current_uuid}, "
                    f"сейчас {report.report_uuid}: нужна ручная проверка"
                )
            )

    for name, value in (
        (settings.bitrix.saby_uuid_field, report.report_uuid),
        (settings.bitrix.saby_status_code_field, report.state_code),
        (settings.bitrix.saby_status_field, _status_label(report)),
    ):
        if name and name in context.fields and value:
            _set_change(changes, item, name, value)

    if report.submitted:
        if report.fact_date is None:
            return Plan(blocked_reason="код 7, но положительный протокол не найден")

        fact_field = settings.bitrix.fact_date_field
        if fact_field not in context.fields:
            return Plan(
                blocked_reason=f"в Bitrix нет поля фактической даты {fact_field}"
            )

        target_date = report.fact_date.date().isoformat()
        existing_date = _date_only(item.get(fact_field))
        if item.get(fact_field) and existing_date is None:
            return Plan(
                blocked_reason=(
                    f"не удалось разобрать текущую дату {item.get(fact_field)!r}"
                )
            )
        if existing_date and existing_date != target_date:
            return Plan(
                blocked_reason=(
                    f"конфликт фактической даты: Bitrix {existing_date}, "
                    f"Saby {target_date}"
                )
            )
        if existing_date != target_date:
            changes[fact_field] = target_date

        stage_id = settings.bitrix.sent_stage_id
        if stage_id:
            if stage_id not in context.stages:
                return Plan(
                    blocked_reason=f"стадия {stage_id} не найдена в Bitrix"
                )
            if str(item.get("stageId") or "") != stage_id:
                changes["stageId"] = stage_id
        else:
            notes.append("стадия «Сдано» не настроена — stageId не меняем")

    checked_field = settings.bitrix.saby_checked_field
    if checked_field and checked_field in context.fields and changes:
        _set_change(changes, item, checked_field, _format_datetime(now))

    return Plan(changes=changes, notes=notes)


def _status_label(report: Report) -> str | None:
    parts = [part for part in (report.state_code, report.state_name) if part]
    return " ".join(parts) or None


def _set_change(
    changes: dict[str, Any],
    item: dict[str, Any],
    name: str,
    value: Any,
) -> None:
    if not _same_value(name, item.get(name), value):
        changes[name] = value


# ----------------------------------------------------------------------
# Выполнение
# ----------------------------------------------------------------------


async def process_reports(
    reports: Sequence[Report],
    bitrix: BitrixService,
    context: BitrixContext,
    *,
    apply: bool,
    events_count: int,
    forced_item_id: int | None = None,
) -> Summary:
    """Проходит отчёты, сопоставляет карточки и обновляет изменившиеся поля."""
    summary = Summary(
        events=events_count,
        reports=len(reports),
        submitted=sum(1 for report in reports if report.submitted),
    )
    mode = "APPLY" if apply else "DRY-RUN"

    for report in reports:
        label = _report_label(report)
        print(f"\n[{mode}] {label}")
        print(
            f"  Saby: {report.document_type}/{report.subtype}, "
            f"{report.year}/{report.period_code}, "
            f"state={report.state_code} {report.state_name or ''}, "
            f"fact={_date_text(report.fact_date)}"
        )

        try:
            item, reason = await _resolve_item(
                report, bitrix, context, forced_item_id
            )
        except BitrixError as exc:
            _add_exception(summary, label, f"Bitrix: {exc}")
            continue

        if reason:
            _add_exception(summary, label, reason)
            continue

        summary.matched += 1
        print(
            f"  Bitrix: item {item.get('id')} «{item.get('title') or ''}», "
            f"stage={item.get('stageId')}"
        )

        plan = plan_changes(report, item, context, datetime.now(tz=MSK))
        for note in plan.notes:
            print(f"  примечание: {note}")

        if plan.blocked_reason:
            _add_exception(summary, label, plan.blocked_reason)
            continue

        if not plan.changes:
            print("  изменений нет")
            continue

        summary.planned += 1
        _print_changes(item, plan.changes)

        if not apply:
            continue

        try:
            await bitrix.update_item(
                context.entity_type_id,
                int(item["id"]),
                plan.changes,
            )
            verified = await bitrix.get_item(
                context.entity_type_id,
                int(item["id"]),
            )
        except BitrixError as exc:
            _add_exception(summary, label, f"Bitrix update: {exc}")
            continue

        mismatches = [
            name
            for name, value in plan.changes.items()
            if not _same_value(name, verified.get(name), value)
        ]
        if mismatches:
            _add_exception(
                summary,
                label,
                "проверка после записи не совпала: " + ", ".join(mismatches),
            )
            continue

        summary.updated += 1
        print("  обновлено и проверено")

    return summary


async def _resolve_item(
    report: Report,
    bitrix: BitrixService,
    context: BitrixContext,
    forced_item_id: int | None,
) -> tuple[dict[str, Any], str | None]:
    if forced_item_id is not None:
        item = await bitrix.get_item(context.entity_type_id, forced_item_id)
        return item, None

    candidates, reason = find_candidates(report, context)
    if reason:
        return {}, reason
    if not candidates:
        return {}, "кандидат в Bitrix не найден"
    if len(candidates) > 1:
        ids = ", ".join(str(item.get("id")) for item in candidates)
        return {}, f"несколько кандидатов в Bitrix: {ids}"

    # Перед сравнением читаем актуальное состояние карточки.
    item = await bitrix.get_item(
        context.entity_type_id,
        int(candidates[0]["id"]),
    )
    return item, None


def _print_changes(item: dict[str, Any], changes: dict[str, Any]) -> None:
    for name, value in changes.items():
        current = item.get(name)
        print(f"  изменить {name}: {current!r} -> {value!r}")


def _add_exception(summary: Summary, label: str, reason: str) -> None:
    message = f"{label}: {reason}"
    summary.exceptions.append(message)
    print(f"  исключение: {reason}")


def _print_summary(summary: Summary, *, apply: bool) -> None:
    print("\n=== Итог ===")
    print(f"Режим: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Событий Saby: {summary.events}")
    print(f"Отчётов: {summary.reports}, сдано: {summary.submitted}")
    print(f"Сопоставлено: {summary.matched}")
    print(f"Предложено обновлений: {summary.planned}")
    print(f"Обновлено и проверено: {summary.updated}")
    print(f"Исключений: {len(summary.exceptions)}")
    for message in summary.exceptions[:100]:
        print(f"  - {message}")


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Синхронизация статусов отчётности Saby с Bitrix24",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    poll = subparsers.add_parser("poll", help="инкрементальный опрос Saby")
    poll.add_argument(
        "--days",
        type=int,
        default=30,
        help="глубина перекрытия в днях (по умолчанию 30)",
    )
    poll.add_argument(
        "--apply",
        action="store_true",
        help="записать изменения в Bitrix; без флага — dry-run",
    )

    sync = subparsers.add_parser("sync", help="исторический интервал")
    sync.add_argument("--from", dest="date_from", required=True, help="ГГГГ-ММ-ДД")
    sync.add_argument("--to", dest="date_to", required=True, help="ГГГГ-ММ-ДД")
    sync.add_argument(
        "--apply",
        action="store_true",
        help="записать изменения в Bitrix; без флага — dry-run",
    )

    reconcile = subparsers.add_parser(
        "reconcile",
        help="сверка одного отчёта или карточки",
    )
    reconcile.add_argument("--report-uuid")
    reconcile.add_argument("--bitrix-id", type=int)
    reconcile.add_argument(
        "--days",
        type=int,
        default=30,
        help="глубина поиска событий (по умолчанию 30)",
    )
    reconcile.add_argument(
        "--apply",
        action="store_true",
        help="записать изменения в Bitrix; без флага — dry-run",
    )

    return parser


async def run(args: argparse.Namespace) -> None:
    _validate_settings()

    saby = await get_saby_service()
    bitrix = await get_bitrix_service()

    try:
        if args.command == "reconcile":
            events, forced_item_id = await _load_reconcile(saby, bitrix, args)
        else:
            date_from, date_to = _resolve_range(args)
            print(
                f"Период Saby: {_format_datetime(date_from)} — "
                f"{_format_datetime(date_to)}"
            )
            events = await saby.list_events(date_from, date_to)
            forced_item_id = None

        reports = build_reports(events)
        print(f"Получено событий: {len(events)}, отчётов: {len(reports)}")

        context = await load_bitrix_context(bitrix)
        summary = await process_reports(
            reports,
            bitrix,
            context,
            apply=args.apply,
            events_count=len(events),
            forced_item_id=forced_item_id,
        )
        _print_summary(summary, apply=args.apply)
    finally:
        await SessionManager.close_session()


async def _load_reconcile(
    saby: SabyService,
    bitrix: BitrixService,
    args: argparse.Namespace,
) -> tuple[list[SabyEvent], int | None]:
    date_to = datetime.now(tz=MSK)
    date_from = date_to - timedelta(days=args.days)
    report_uuid = args.report_uuid

    if not report_uuid:
        uuid_field = settings.bitrix.saby_uuid_field
        if not uuid_field:
            raise SystemExit(
                "нужен --report-uuid: поле Saby UUID в Bitrix не настроено"
            )
        item = await bitrix.get_item(settings.bitrix.entity_type_id, args.bitrix_id)
        report_uuid = item.get(uuid_field)
        if not report_uuid:
            raise SystemExit(
                f"в карточке {args.bitrix_id} не заполнено поле {uuid_field}"
            )
        print(f"Карточка {args.bitrix_id}: Saby UUID {report_uuid}")

    events = await saby.list_document_events(
        report_uuid,
        date_from=date_from,
        date_to=date_to,
    )
    print(
        f"События отчёта {report_uuid}: {len(events)} "
        f"за {_format_datetime(date_from)} — {_format_datetime(date_to)}"
    )
    return events, args.bitrix_id


def _resolve_range(args: argparse.Namespace) -> tuple[datetime, datetime]:
    if args.command == "poll":
        if args.days <= 0:
            raise SystemExit("--days должен быть больше нуля")
        date_to = datetime.now(tz=MSK)
        return date_to - timedelta(days=args.days), date_to

    date_from = _parse_day(args.date_from)
    date_to = _parse_day(args.date_to, end_of_day=True)
    if date_to <= date_from:
        raise SystemExit("--to должен быть больше --from")
    return date_from, date_to


def _parse_day(value: str, *, end_of_day: bool = False) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=MSK)
    except ValueError as exc:
        raise SystemExit(f"дата должна быть в формате ГГГГ-ММ-ДД: {value}") from exc
    if end_of_day:
        parsed += timedelta(days=1) - timedelta(seconds=1)
    return parsed


def _validate_settings() -> None:
    if not settings.saby.inn or not settings.saby.kpp:
        raise SystemExit(
            "не заданы SABY_INN и SABY_KPP — без них Saby не отфильтровать"
        )


# ----------------------------------------------------------------------
# Сравнение значений Bitrix
# ----------------------------------------------------------------------


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _same_value(name: str, current: Any, target: Any) -> bool:
    if name == settings.bitrix.fact_date_field:
        return _date_only(current) == _date_only(target)
    if name == settings.bitrix.saby_checked_field:
        return _datetime_key(current) == _datetime_key(target)
    return _same_simple(current, target)


def _same_simple(current: Any, target: Any) -> bool:
    if isinstance(current, (list, tuple)):
        return any(_same_simple(item, target) for item in current)
    current_text = "" if current is None else str(current).strip()
    target_text = "" if target is None else str(target).strip()
    return current_text == target_text


def _date_only(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    for fmt in ("%d.%m.%Y", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _datetime_key(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(microsecond=0, tzinfo=None).isoformat()
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    return text


def _format_datetime(value: datetime) -> str:
    return value.astimezone(MSK).strftime("%d.%m.%Y %H:%M:%S")


def _date_text(value: datetime | None) -> str:
    return value.astimezone(MSK).date().isoformat() if value else "—"


def _report_label(report: Report) -> str:
    title = report.title or report.note or ""
    return f"{report.report_uuid[:8]}… {title}".strip()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "reconcile" and not (args.report_uuid or args.bitrix_id):
        parser.error("укажите --report-uuid или --bitrix-id")

    try:
        asyncio.run(run(args))
    except (SabyError, BitrixError) as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
