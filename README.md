# testclient-bridge

MCP-мост к **1С:Предприятию** через клиент тестирования.

Проект даёт AI-агенту три способности:

1. **UI automation** — управление интерфейсом 1С через `/TESTMANAGER` + `/TESTCLIENT`.
2. **Runtime diagnostics** — выгрузка журнала регистрации автономного сервера через `ibcmd eventlog export`.
3. **RAG metadata index** — локальный SQLite/FTS индекс конфигураций 1С (EDT, XML-выгрузка Конфигуратора, base + extensions).

## Быстрый старт

### One-shot сессия (рекомендуется)

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[screenshot,dev,window-control]'

# Запуск MCP-сервера (transport: stdio)
testclient-bridge --ws-host 127.0.0.1 --ws-port 8765
```

Одна сессия через MCP tool:

```text
start_session(
  base_url="https://your-stand.42clouds.com/ms",
  username="your-login",
  password="your-password",
  version="8.5.1.1150",  # опционально, авто-детект из base_url
  session_id="my-session",  # опционально, авто-генерация
  start_pipeline=True  # запустить ibsrv + test-manager
)
```

Возвращает:

```json
{
  "session_id": "my-session",
  "version": "8.5.1.1150",
  "platform_dir": "/opt/1cv8/x86_64/8.5.1.1150",
  "display": ":99",
  "ws_port": 8765,
  "ibsrv_port": 8314,
  "testclient_port": 1538,
  "data_dir": "/tmp/onec-mcp/sessions/my-session",
  "bridge_url": "ws://127.0.0.1:8765/bridge",
  "manager_pid": 12345,
  "ws_url": "http://127.0.0.1:8314",
  "log_dir": "/tmp/onec-mcp/sessions/my-session/logs"
}
```

Подключение тестируемой базы:

```text
connect_test_client(
  session_id="my-session",
  base_url="<TARGET_INFOBASE_URL>",
  username="<USERNAME>",
  password="<PASSWORD>",
  port=1538
)
```

Логин/пароль можно не передавать явно: `connect_test_client` умеет брать их из локального credentials-файла по `base_url`.

### Безопасное хранение логинов/паролей

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
      "url": "https://beta-ms.42clouds.com/ms",
      "username": "Admin",
      "password": "beta"
    },
    {
      "url": "https://*.42clouds.com/*",
      "username": "testuser",
      "password": "123321"
    }
  ]
}
```

Правила матчинга: сначала точное совпадение `url`, затем wildcard (`*`, `?`) через `fnmatch`; первое совпадение побеждает. Пароли не логируются и не возвращаются наружу; tool `credentials_list()` показывает только URL-шаблоны и логины с `password="***"`.

Остановка сессии:

```text
stop_session(session_id="my-session", clean_data=False)
```

### Low-level запуск (legacy)

Через `launch_manager()` и ручное управление процессами. См. раздел "Запуск менеджера тестирования".

## Требования

- Python 3.11+
- Пакеты: `mcp`, `websockets`, `pydantic`; для скриншотов — `mss`; для управления OS-окном — `python-xlib`/`wmctrl`/`xdotool`
- 1С:Предприятие 8.5+ с поддержкой WebSocket-клиентов и режима тестирования
- GUI/Xvfb для headless-сервера

## Безопасность стендов и учётных данных

В репозитории не должно быть реальных URL стендов, логинов или паролей. Все параметры целевой базы передаются явно при вызове `connect_test_client` или через локальные переменные окружения/секретное хранилище:

- `TARGET_INFOBASE_URL` — URL тестируемой информационной базы;
- `TARGET_USERNAME` — пользователь тестируемой базы;
- `TARGET_PASSWORD` — пароль;
- `TARGET_TESTCLIENT_PORT` — порт клиента тестирования, по умолчанию `1538`.

Примеры в документации используют только плейсхолдеры (`<TARGET_INFOBASE_URL>`, `<USERNAME>`, `<PASSWORD>`). Перед публикацией артефактов записи проверяйте, что параметры вызовов заредактированы: recorder маскирует ключи вроде `password`, но URL и логин тоже не должны попадать в публичные материалы.

## Архитектура

### Сессии

Сервер поддерживает **одновременные изолированные сессии** в рамках одного MCP-процесса. Каждая сессия получает:

- Свой `Xvfb` display (автоподбор из `:99`–`:199`)
- Свои TCP-порты (WebSocket bridge, ibsrv HTTP, test-client)
- Свою файловую базу (`/tmp/onec-mcp/sessions/{session_id}/ib`)
- Свои логи (`/tmp/onec-mcp/sessions/{session_id}/logs`)

`session_id` передаётся явным keyword-only параметром во все MCP tools. Если не указан — используется сессия `"default"` (обратная совместимость).

### Компоненты

```text
MCP client (LLM/agent)
        │ stdio MCP
        ▼
Python MCP server (testclient-bridge)
        │ JSON-RPC-like requests
        ▼
WebSocket server (ws://127.0.0.1:{port}/bridge)
        │
        ▼
1C test manager (1cv8c /WS ... /TESTMANAGER)
        │ opens embedded processor MCPTestManager
        │ 1C test manager API (ТестируемоеПриложение)
        ▼
1C test client (/TESTCLIENT -TPort{port})
        │
        ▼
Target infobase (<TARGET_INFOBASE_URL>)
```

Подробнее: [`docs/architecture.md`](docs/architecture.md).

## Сборка конфигурации 1С

Конфигурация `MCPTestManager.cf` содержит встроенную обработку-мост и собирается из XML-исходников `src/cf/`:

```bash
DISPLAY=:99 ./scripts/build_cf.sh
```

## Запуск менеджера тестирования

Через MCP tool `launch_manager` или вручную:

```bash
# 1. Создать файловую базу
/opt/1cv8/x86_64/8.5.1.1150/ibcmd infobase create --db-path=/tmp/onec-mcp/ib --create-database

# 2. Загрузить конфигурацию с обработкой-мостом
/opt/1cv8/x86_64/8.5.1.1150/ibcmd config load --db-path=/tmp/onec-mcp/ib build/MCPTestManager.cf

# 3. Запустить автономный сервер
/opt/1cv8/x86_64/8.5.1.1150/ibsrv --data=/tmp/onec-mcp/ibsrv-data --name=mcp

# 4. Запустить менеджер тестирования
DISPLAY=:99 /opt/1cv8/x86_64/8.5.1.1150/1cv8c ENTERPRISE \
  /WS"http://127.0.0.1:8314" \
  /Lru /VLru_RU \
  /TESTMANAGER
```

## Подключение тестируемой базы

MCP tool `connect_test_client` требует явные параметры целевой базы. Значения не дефолтятся в коде и не должны коммититься:

```text
connect_test_client(
  base_url="<TARGET_INFOBASE_URL>",
  username="<USERNAME>",
  password="<PASSWORD>",
  port=1538,
  onec_path="/opt/1cv8/x86_64/8.5.1.1150/1cv8c"
)
```

## MCP tools

### Lifecycle

- `start_session(base_url, username, password, version, session_id, start_pipeline)` — one-shot: Xvfb + free ports + version detection + ibsrv + test-manager.
- `stop_session(session_id, clean_data)` — остановка всех процессов, Xvfb, bridge, опционально удаление data_dir.
- `bridge_status(session_id)` — статус WebSocket bridge.
- `runtime_status(session_id)` — состояние процессов автономного сервера и менеджера.
- `launch_manager(...)` — полный конвейер: ibcmd → ibsrv → 1cv8c /WS /TESTMANAGER (low-level, внутри существующей сессии).
- `stop_manager(session_id)` — остановка manager + ibsrv (без остановки сессии).
- `export_eventlog(...)` — выгрузка журнала регистрации через `ibcmd eventlog export`.

### Test client

- `connect_test_client(base_url, username, password, port, onec_path, session_id)` — подключить тест-клиент.
- `disconnect_test_client(session_id)` — отключить.

### UI automation

- `active_window()` — активное окно.
- `ui_tree(max_depth)` — дерево UI.
- `command_bar()` — командная панель.
- `current_element()` — текущий элемент.
- `find_object(name, type)` — найти объект в дереве UI.
- `activate_object(name, type)` — активировать объект.
- `execute_window_command(command)` — выполнить команду окна.
- `click_button(name, search_by_title)` — нажать кнопку.
- `set_field_value(name, value)` — установить значение поля.
- `create_new_item()` — создать новый элемент.
- `save_form()` — сохранить форму.
- `close_form()` — закрыть форму.
- `screenshot(path)` — скриншот экрана.

### Table / table-part

- `table_rows(table_name)` — строки таблицы.
- `table_find_row(table_name, substring)` — найти строку по подстроке.
- `table_goto_row(table_name, index)` — перейти к строке по индексу.
- `table_current_row(table_name)` — текущая строка.
- `table_selected_rows(table_name)` — выделенные строки.
- `table_set_field(table_name, field, value)` — установить значение в ячейке.
- `table_edit_info(table_name)` — информация о редактировании таблицы.
- `toggle_field(name)` — переключить флажок.

### Reference / selection

- `choose_field_from_list(field_name, substring)` — выбрать из списка по подстроке.
- `choose_field_first_row(field_name)` — выбрать первую строку списка.
- `choose_current_row()` — выбрать текущую строку.

### Dynamic list tools

- `dynamic_list_set_order` — сортировка по видимому заголовку колонки (`УстановитьПорядок(<ЗаголовокКолонки>)`).
- `dynamic_list_output` — команда **Вывести список** с возвратом текста табличного документа, когда он доступен.
- `dynamic_list_open_settings` — открыть **Настроить список**.
- `dynamic_list_clear_settings` — сбросить настройки динамического списка.
- `dynamic_list_open_form_settings` — открыть **Изменить форму**.

### Evidence recording

Use `recording_start(output_dir, display, window)` before a workflow. Recorded tool calls create screenshots and annotated slides. Finish with `recording_stop()` to build:

- `manifest.json`
- `recording.gif`
- `recording.mp4`
- `recording.pdf` when ImageMagick is installed

For OS window maximization use `maximize_test_client_external_window()`. It tries `wmctrl`, then `xdotool`, then the built-in `python-xlib` fallback.

### RAG / metadata index

- `rag_source_detect(path)` — определить формат исходников.
- `rag_source_add(name, path, kind, format)` — зарегистрировать источник.
- `rag_sources_list()` — список источников.
- `rag_snapshot_create(name, base, extensions, sources)` — создать effective snapshot.
- `rag_snapshots_list()` — список snapshot-ов.
- `rag_index_build(snapshot, source)` — построить индекс.
- `rag_lookup_object(object, snapshot)` — найти объект метаданных.
- `rag_query(query, snapshot, limit)` — полнотекстовый поиск.

CLI-обёртка:

```bash
python3 scripts/rag_cli.py add-source MS /path/to/MS/src --kind base
python3 scripts/rag_cli.py snapshot MS+Platform42 --base MS --extension Platform42
python3 scripts/rag_cli.py build --snapshot MS+Platform42
python3 scripts/rag_cli.py lookup РегистрСведений.РезервныеКопииОбластейДанных --snapshot MS+Platform42
```

## Ограничения

- Одно активное WebSocket-подключение обработки 1С.
- Скриншот делается Python-стороной через `mss`; нужен общий DISPLAY.
- `/TESTMANAGER` обязателен — это режим тонкого клиента с управлением клиентами тестирования.
- `ManagedApplicationModule.ПриНачалеРаботыСистемы` не вызывается в `/TESTMANAGER` на 8.5.1 — обработка открывается через `ОбщаяКоманда` с `Autostart=true` или через командную панель.
- Форма пользовательской настройки **«Изменить форму»** частично недоступна для надёжной автоматизации через API клиента тестирования. В частности, команда **«Добавить поля»** в верхней панели формы настройки может быть видна на скриншоте и в диагностическом тексте, но не нажиматься как обычная `ТестируемаяКнопкаФормы`: `click_button` может не находить её, а `activate_object` может вернуть `activated=true` без открытия диалога добавления полей. Не используйте X11-клики как обходной путь. Для проверяемых сценариев фильтрации динамических списков используйте **«Настроить список...»** и доступные поля компоновщика; для вывода вложенной колонки через **«Изменить форму»** текущая архитектура не гарантирует выполнение.

## Лицензия

MIT
