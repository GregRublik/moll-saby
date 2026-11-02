# moll-saby

Синхронизация статусов отчётности Saby с Bitrix24. Сервис читает события
`СБИС.СписокИзменений`, находит карточку смарт-процесса «Корпоративная
Отчетность» (`entityTypeId=137`, воронка `118`) и при статусе `7` пишет дату
сдачи и переводит карточку в стадию «Сдано».

## Установка

```bash
uv sync
```

## Настройка

Создайте `.env` в корне проекта.

Обязательные переменные:

```dotenv
# Saby: либо токен, либо логин/пароль техпользователя
SABY_ACCESS_TOKEN=...
# SABY_LOGIN=...
# SABY_PASSWORD=...
# SABY_ACCOUNT_NUMBER=...

SABY_INN=...
SABY_KPP=...

# Bitrix: вебхук
BITRIX_BASE_URL=https://...
BITRIX_ID_USER=...
BITRIX_KEY_201=...
```

Коды полей карточки и ID стадии «Сдано»:

```dotenv
BITRIX_FORM_FIELD=ufCrm110_...
BITRIX_PERIOD_FIELD=ufCrm110_...
BITRIX_RECIPIENT_FIELD=ufCrm110_...
BITRIX_FACT_DATE_FIELD=ufCrm110_...
BITRIX_SABY_UUID_FIELD=ufCrm110_...
BITRIX_SABY_STATUS_CODE_FIELD=ufCrm110_...
BITRIX_SABY_STATUS_FIELD=ufCrm110_...
BITRIX_SABY_CHECKED_FIELD=ufCrm110_...
BITRIX_SENT_STAGE_ID=DT137_118:...
```

## Подготовка Bitrix

```bash
# только проверить поля и стадию
PYTHONPATH=src .venv/bin/python -m setup_bitrix

# создать отсутствующие поля и стадию «Сдано»
PYTHONPATH=src .venv/bin/python -m setup_bitrix --apply --create-stage
```

## Запуск

Все команды выполняются из корня проекта, `PYTHONPATH=src` обязателен.

```bash
# dry-run: показать планируемые изменения без записи в Bitrix
PYTHONPATH=src .venv/bin/python -m main poll --days 30

# записать изменения
PYTHONPATH=src .venv/bin/python -m main poll --days 30 --apply

# исторический интервал
PYTHONPATH=src .venv/bin/python -m main sync --from 2026-01-01 --to 2026-01-31

# сверка одного отчёта или карточки
PYTHONPATH=src .venv/bin/python -m main reconcile --report-uuid <uuid>
PYTHONPATH=src .venv/bin/python -m main reconcile --bitrix-id <id>

# диагностика полей и карточек Bitrix (только чтение)
PYTHONPATH=src .venv/bin/python -m inspect_bitrix
```

Без флага `--apply` команды `poll`, `sync` и `reconcile` работают в режиме
dry-run и ничего не пишут в Bitrix.

## Как ищется карточка

Ключ сопоставления — **название отчёта + период**. Код периода Saby
преобразуется в название: `1–12` → `Янв'26`…`Дек'26`, `21–24` →
`I кв'26`…`IV кв'26`, `34` → `2025`. Год уже входит в название периода.
Если карточек с таким ключом несколько, используется поле «Получатель».
При неоднозначности или отсутствии карточки отчёт попадает в исключения,
Bitrix не изменяется.
