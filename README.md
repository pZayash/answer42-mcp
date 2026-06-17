# testclient-bridge

MCP-мост к **1С:Предприятию** через клиент тестирования.

Проект даёт AI-агенту три способности:

1. **UI automation** — управление интерфейсом 1С через `/TESTMANAGER` + `/TESTCLIENT`.
2. **Runtime diagnostics** — выгрузка журнала регистрации автономного сервера через `ibcmd eventlog export`.
3. **RAG metadata index** — локальный SQLite/FTS индекс конфигураций 1С (EDT, XML-выгрузка Конфигуратора, base + extensions).

## Архитектура

```text
MCP client (LLM/agent)
        │ stdio MCP
        ▼
Python MCP server (testclient-bridge)
        │ JSON-RPC-like requests
        ▼
WebSocket server (ws://127.0.0.1:8765/bridge)
        │
        ▼
1C test manager (1cv8c /WS ... /TESTMANAGER)
        │ opens embedded processor MCPTestManager
        │ 1C test manager API (ТестируемоеПриложение)
        ▼
1C test client (/TESTCLIENT -TPort1538)
        │
        ▼
Target infobase, e.g. <TARGET_INFOBASE_URL>
```

Подробнее: [`docs/architecture.md`](docs/architecture.md).

## Требования

- Python 3.11+
- Пакеты: `mcp`, `websockets`, `pydantic`; для скриншотов — `mss`
- 1С:Предприятие 8.5+ с поддержкой WebSocket-клиентов и режима тестирования
- GUI/Xvfb для headless-сервера

## Установка

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[screenshot,dev]'
```

## Запуск MCP-сервера

```bash
testclient-bridge --ws-host 127.0.0.1 --ws-port 8765
```

MCP transport — stdio. WebSocket URL для обработки 1С:

```text
ws://127.0.0.1:8765/bridge
```

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

MCP tool `connect_test_client`:

- URL: `<TARGET_INFOBASE_URL>`
- user: `<USERNAME>`
- password: `<PASSWORD>`
- test client port: `1538`

## MCP tools

### Lifecycle

- `bridge_status()` — статус WebSocket bridge.
- `runtime_status()` — состояние процессов автономного сервера и менеджера.
- `launch_manager(...)` — полный конвейер: ibcmd → ibsrv → 1cv8c /WS /TESTMANAGER.
- `stop_manager()` — остановка всех процессов.
- `export_eventlog(...)` — выгрузка журнала регистрации через `ibcmd eventlog export`.

### Test client

- `connect_test_client(base_url, username, password, port, onec_path)` — подключить тест-клиент.
- `disconnect_test_client()` — отключить.

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

### RAG / metadata index

- `rag_source_detect(path)` — определить формат исходников.
- `rag_source_add(name, path, kind, format)` — зарегистрировать источник.
- `rag_sources_list()` — список источников.
- `rag_snapshot_create(name, base, extensions, sources)` — создать effective snapshot.
- `rag_snapshots_list()` — список snapshot-ов.
- `rag_index_build(snapshot, source)` — построить индекс.
- `rag_lookup_object(object, snapshot)` — найти объект метаданных.
- `rag_query(query, snapshot, limit)` — полнотекстовый поиск.

### CLI-обёртка RAG

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

## Лицензия

MIT
