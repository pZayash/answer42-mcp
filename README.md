# Answer42

<p align="center">
  <img src="docs/assets/answer42-logo.png" alt="Answer42 logo" width="220">
</p>

**Answer42** — MCP-инструмент для интерактивного управления UI **1С:Предприятия** через клиент тестирования. Он позволяет AI-агенту открывать формы 1С, нажимать кнопки, заполнять поля, выбирать ссылки из форм выбора, работать с таблицами, динамическими списками и табличными документами.

> **The Answer to Life, Universe, and 1C — UI Driver**

Проект даёт AI-агенту три способности:

1. **Интерактивное управление UI 1С** — открыть форму, кликнуть кнопку, активировать поле, заполнить значение, выбрать строку, провести сценарий через `/TESTMANAGER` + `/TESTCLIENT`.
2. **Доказательная запись клиентского тестирования** — записывать аннотированные PDF/GIF/MP4-слайды с MCP method/request/response и скриншотами окна 1С.
3. **RAG-индекс метаданных** — локальный SQLite/FTS индекс конфигураций 1С (EDT, XML-выгрузка Конфигуратора, base + extensions), включая подсказки для `ui_tree` и dynamic-list settings.

## Быстрый старт

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[screenshot,dev,window-control]'

# Запуск MCP-сервера (transport: stdio)
answer42 --ws-host 127.0.0.1 --ws-port 8765
```

Запуск сессии и подключение тестируемой базы выполняются одним MCP-вызовом:

```text
start_session(
  session_id="my-session",
  base_url="<TARGET_INFOBASE_URL>",
  username="<USERNAME>",
  password="<PASSWORD>",
  idle_timeout_minutes=60,
  start_pipeline=True,
  execute="/path/to/external.epf",  # опционально: /Execute
  command_parameter="InitScenario" # опционально: /C
)
```

`start_session` определяет версию 1С по `base_url`, поднимает инфраструктуру Answer42 (Xvfb, свободные порты, WebSocket bridge, файловую базу менеджера, автономный сервер и test-manager) и сразу подключает клиент тестирования к целевой информационной базе. После `idle_timeout_minutes` минут неактивности сессия автоматически завершается; значение по умолчанию — 60 минут. Для запуска клиента можно передать `extra_args`, `execute` (параметр командной строки `/Execute`) и `command_parameter` (параметр `/C`); если `/C` уже указан в `extra_args`, значения склеиваются через `;`.

Логин/пароль можно не передавать явно: `start_session` умеет брать их из локального credentials-файла по `base_url`.

## Безопасное хранение логинов и паролей

По умолчанию MCP-сервер читает файл:

```text
~/.onec-mcp-credentials.json
```

Путь можно переопределить переменной окружения:

```bash
export ONEC_MCP_CREDENTIALS_FILE=/secure/path/credentials.json
```

Формат файла:

```json
{
  "entries": [
    {
      "url": "https://example.invalid/infobase",
      "username": "<USERNAME>",
      "password": "<PASSWORD>"
    },
    {
      "url": "https://*.example.invalid/*",
      "username": "<WILDCARD_USERNAME>",
      "password": "<WILDCARD_PASSWORD>"
    }
  ]
}
```

Правила матчинга: сначала точное совпадение `url`, затем wildcard (`*`, `?`) через `fnmatch`; первое совпадение побеждает. Пароли не логируются и не возвращаются наружу; tool `credentials_list()` показывает только URL-шаблоны и логины с `password="***"`.

Остановка сессии:

```text
stop_session(session_id="my-session", clean_data=False)
```

## Требования

- Python 3.11+
- 1С:Предприятие 8.3.27+
- Пакеты Python: `mcp`, `websockets`, `pydantic`; для скриншотов — `mss`; для управления OS-окном — `python-xlib`/`wmctrl`/`xdotool`
- GUI/Xvfb для headless-сервера

## Безопасность стендов и учётных данных

В репозитории не должно быть реальных URL стендов, логинов или паролей. Все параметры целевой базы передаются явно при вызове `start_session` или через локальные переменные окружения/секретное хранилище.

Примеры в документации используют только плейсхолдеры (`<TARGET_INFOBASE_URL>`, `<USERNAME>`, `<PASSWORD>`). Перед публикацией артефактов проверяйте, что параметры вызовов заредактированы: recorder маскирует ключи вроде `password`, но URL и логин тоже не должны попадать в публичные материалы.

## Архитектура

Поток выполнения:

1. MCP-клиент вызывает tools через stdio MCP.
2. Answer42 принимает MCP-вызовы и передаёт команды в WebSocket bridge.
3. 1С test manager подключается к bridge и выполняет BSL-dispatch.
4. Test manager управляет 1С test client через API `ТестируемоеПриложение`.
5. Test client выполняет интерактивные операции в целевой информационной базе.

Подробнее: [`docs/architecture.md`](docs/architecture.md).

## Сборка конфигураций 1С

Конфигурация менеджера тестирования собирается из XML-исходников `src/cf/`:

```bash
DISPLAY=:99 ./scripts/build_cf.sh src/cf build/MCPTestManager.cf
```

Тестовая конфигурация для E2E собирается из XML-исходников `src/client_cf/`:

```bash
DISPLAY=:99 ./scripts/build_cf.sh src/client_cf build/MCPTestClient.cf
```

E2E-сценарий всегда пересобирает `build/MCPTestClient.cf` из `src/client_cf/`, чтобы тесты не использовали устаревший артефакт.

## MCP-инструменты

### Жизненный цикл

- `start_session(...)` — единый запуск: определение версии по `base_url`, Xvfb, свободные порты, ibsrv, test-manager и подключение клиента тестирования. Поддерживает `extra_args`, `execute` → `/Execute`, `command_parameter` → `/C`; несколько частей `/C` объединяются через `;`.
- `session_status(session_id)` — состояние bridge, runtime, клиента тестирования и RAG snapshot.
- `stop_session(session_id, clean_data)` — отключение клиента тестирования и остановка всех процессов, Xvfb и bridge.

### Автоматизация UI

- `active_window()` — активное окно тестируемого приложения через `ТестируемоеПриложение.ПолучитьАктивноеОкно()`.
- `windows_list(max_depth)` — список окон тестируемого приложения; если открыто два окна клиента, возвращает оба, потому что читает подчинённые объекты `ТестируемоеПриложение`, а не окна менеджера тестирования.
- `activate_window(index, title, name)` — переключить активное окно по индексу, заголовку или имени.
- `goto_start_page()`, `goto_previous_window()`, `goto_next_window()` — навигация основного окна: `ПерейтиКНачальнойСтранице`, `ПерейтиКПредыдущемуОкну`, `ПерейтиКСледующемуОкну`.
- `ui_tree(max_depth, include_rag_types, rag_snapshot)` — дерево UI с RAG-типами элементов, когда доступен индекс метаданных.
- `command_bar()` — диагностическая информация по командной панели, когда она доступна API тест-клиента.
- `current_element()` — диагностическая информация по текущему элементу, когда она доступна API тест-клиента.
- `find_object(name, title, type)` — найти объект в дереве UI.
- `activate_object(name, title, type)` — активировать объект.
- `open_navigation_link(navigation_link)` — открыть навигационную ссылку `e1cib/...`.
- `click_button(name, search_by_title)` — нажать кнопку.
- `set_field_value(field_name, value, search_by_title)` — установить значение поля.
- `create_new_item()` — создать новый элемент.
- `save_form()` — сохранить форму.
- `close_form()` — закрыть форму.
- `screenshot(path)` — скриншот экрана.

### Таблицы и табличные части

- `table_rows(...)` — строки таблицы.
- `table_find_row(...)` — найти строку по подстроке.
- `table_goto_row(...)` — перейти к строке.
- `table_current_row(...)` — текущая строка.
- `table_selected_rows(...)` — выделенные строки.
- `table_set_field(...)` — установить значение в ячейке.
- `table_choose_field_from_list(...)` — открыть форму выбора для ссылочного поля в строке табличной части.
- `table_edit_info(...)` — информация о редактировании таблицы.
- `table_move_row(...)` — перейти к первой/следующей/предыдущей/последней строке.

### Динамические списки

- `dynamic_list_available_fields(object, rag_snapshot)` — поля, доступные для отбора/сортировки по RAG-индексу.
- `dynamic_list_apply_settings(filters, orders, clear_first)` — применить сортировки через API тест-клиента и best-effort отборы через стандартную форму **Настроить список**.
- `dynamic_list_set_order(column_title, name="Список")` — сортировка по видимому заголовку колонки.
- `dynamic_list_output()` — команда **Вывести список** с возвратом текста табличного документа, когда он доступен.
- `dynamic_list_open_settings()` — открыть **Настроить список**.
- `dynamic_list_clear_settings()` — сбросить настройки динамического списка.
- `dynamic_list_open_form_settings()` — открыть **Изменить форму**.

### Табличные документы

- `tabular_documents(include_text)` — список табличных документов в активном окне.
- `tabular_document_text(...)` — текст табличного документа.
- `tabular_document_save(path, format)` — сохранить табличный документ в выбранном формате.


### Доказательная запись

- `recording_start(output_dir, display, window)` — включить доказательную запись.
- `recording_capture(action, note)` — вручную добавить доказательный слайд.
- `recording_stop()` — собрать `manifest.json`, `recording.gif`, `recording.mp4`, `recording.pdf` (если доступен backend PDF).

### RAG / индекс метаданных

- `rag_source_add(name, path, kind, format="auto")` — зарегистрировать источник; формат определяется автоматически при `format="auto"`.
- `rag_sources_list()` — список источников.
- `rag_snapshot_create(name, base, extensions, sources)` — создать effective snapshot.
- `rag_snapshots_list()` — список snapshot-ов.
- `rag_index_build(snapshot, source)` — построить индекс.
- `rag_lookup_object(object, snapshot)` — найти объект метаданных.
- `rag_query(query, snapshot, limit)` — полнотекстовый поиск.

CLI-пример:

```bash
python3 scripts/rag_cli.py add-source Configuration /path/to/configuration/src --kind base
python3 scripts/rag_cli.py add-source Extension1 /path/to/extension/src --kind extension
python3 scripts/rag_cli.py snapshot Configuration+Extension1 --base Configuration --extension Extension1
python3 scripts/rag_cli.py build --snapshot Configuration+Extension1
python3 scripts/rag_cli.py lookup РегистрСведений.Регистр1 --snapshot Configuration+Extension1
```

## Ограничения

- Форма пользовательской настройки **«Изменить форму»** частично недоступна для надёжной автоматизации через API клиента тестирования. В частности, команда **«Добавить поля»** в верхней панели формы настройки может быть видна на скриншоте и в диагностическом тексте, но не нажиматься как обычная `ТестируемаяКнопкаФормы`: `click_button` может не находить её, а `activate_object` может вернуть `activated=true` без открытия диалога добавления полей.

## Лицензия

MIT

## Copyright

Copyright (c) 2026 Kosolapov Stanislav aka proDOOMman <prodoomman@gmail.com>, Marvin (AI Assistant), 42Clouds, and contributors.

Licensed under the MIT License.
