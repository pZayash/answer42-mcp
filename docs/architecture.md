# Архитектура testclient-bridge

## Обзор

`testclient-bridge` — MCP-сервер, который даёт AI-агенту три способности:

1. **UI automation** — управление интерфейсом 1С через `/TESTMANAGER` + `/TESTCLIENT`.
2. **Runtime diagnostics** — выгрузка журнала регистрации автономного сервера.
3. **RAG metadata index** — локальный SQLite/FTS индекс конфигураций 1С.

## Сессии

Начиная с версии 0.3.0, сервер поддерживает **множественные изолированные сессии** в рамках одного процесса. Это позволяет:

- Запускать несколько независимых тестовых окружений параллельно
- Изолировать скриншоты через отдельные Xvfb displays
- Избежать конфликтов портов и файловых баз
- Управлять жизненным циклом через one-shot API

### SessionState

Каждая сессия описывается классом `SessionState`:

```python
class SessionState:
    session_id: str
    display: str              # Xvfb display, e.g. ":99"
    platform_dir: Path        # /opt/1cv8/x86_64/8.5.1.1150
    ws_port: int              # WebSocket bridge port
    ibsrv_port: int           # ibsrv HTTP-gate port
    testclient_port: int      # test-client TCP port
    data_dir: Path            # /tmp/onec-mcp/sessions/{session_id}
    base_url: str
    username: str
    password: str
    xvfb_process: Popen | None
    runtime: ManagerRuntime | None
    bridge: OneCBridgeServer
    recorder: RecordingSession
```

### Глобальное состояние

```python
_SESSIONS: dict[str, SessionState] = {}
```

Если `session_id` не указан в tool-вызове — используется `"default"`.

### One-shot API

```text
start_session(
  base_url="https://your-stand.42clouds.com/ms",
  username="your-login",
  password="your-password",
  version="8.5.1.1150",
  session_id="my-session",
  start_pipeline=True
)
```

Выполняет:

1. Автоопределение версии платформы (из URL или максимальная установленная)
2. Подбор свободного DISPLAY (`:99`–`:199`)
3. Подбор свободных портов (ws, ibsrv, testclient)
4. Запуск Xvfb
5. Старт WebSocket bridge
6. Опционально: создание БД, загрузка .cf, запуск ibsrv, запуск test-manager

Возвращает структуру с session_id, портами, путями, PID менеджера.

```text
stop_session(session_id="my-session", clean_data=False)
```

Останавливает:

- test-manager процесс
- ibsrv процесс
- Xvfb процесс
- WebSocket bridge
- Опционально удаляет data_dir

### Изоляция ресурсов

| Ресурс | Изоляция |
|--------|----------|
| Xvfb display | per-session, автоподбор |
| WebSocket port | per-session, автоподбор |
| ibsrv HTTP port | per-session, автоподбор |
| test-client port | per-session, автоподбор |
| data_dir | `/tmp/onec-mcp/sessions/{session_id}` |
| log_dir | `{data_dir}/logs` |
| eventlog | `{server_data_dir}/log-data` |

## Компоненты

```text
┌─────────────────────────────────────────────────────────────┐
│ MCP client (LLM/agent)                                     │
│   stdio MCP                                                 │
└──────────────┬──────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────┐
│ Python MCP server (testclient-bridge)                      │
│                                                             │
│  src/mcp_1c/server.py    — MCP tools, FastMCP, SessionState      │
│  src/mcp_1c/bridge.py    — per-session WebSocket server          │
│  src/mcp_1c/runtime.py   — 1C process lifecycle, Xvfb helpers    │
│  src/mcp_1c/platform.py  — platform/version/port/display detect  │
│  src/mcp_1c/protocol.py  — JSON-RPC message types           │
│  src/mcp_1c/rag/         — RAG index (model, parsers,       │
│                             store, service, detect)          │
└──────────────┬──────────────────────────────────────────────┘
               │ JSON-RPC-like requests over WebSocket
               ▼
┌─────────────────────────────────────────────────────────────┐
│ WebSocket server (ws://127.0.0.1:{session.ws_port}/bridge)       │
│   OneCBridgeServer:                                         │
│   - one active 1C connection per session                     │
│   - request/response with UUID correlation                  │
│   - 60s default timeout                                     │
└──────────────┬──────────────────────────────────────────────┘
               │ WebSocket
               ▼
┌─────────────────────────────────────────────────────────────┐
│ 1C test manager (1cv8c /WS ... /TESTMANAGER)                │
│                                                             │
│  Автономный сервер (ibsrv)                                  │
│    └─ HTTP-gate на 127.0.0.1:{session.ibsrv_port}                │
│                                                             │
│  Тонкий клиент (1cv8c)                                      │
│    └─ /WS"http://127.0.0.1:{session.ibsrv_port}" /TESTMANAGER  │
│                                                             │
│  Встроенная обработка MCPTestManager                        │
│    └─ src/cf/DataProcessors/MCPTestManager/                  │
│       Forms/Форма/Ext/Form/Module.bsl                       │
│       - WebSocketКлиентСоединения → ws://127.0.0.1:{session.ws_port}     │
│       - ТестируемоеПриложение (test manager API)            │
│       - dispatch: команды → BSL-методы                      │
└──────────────┬──────────────────────────────────────────────┘
               │ ТестируемоеПриложение.УстановитьСоединение()
               ▼
┌─────────────────────────────────────────────────────────────┐
│ 1C test client (/TESTCLIENT -TPort{session.testclient_port})                     │
│   Подключается к целевой базе                               │
└──────────────┬──────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────┐
│ Target infobase                                             │
│   URL/user/password are supplied at connect time            │
│   from local env/secret storage, not from repository files   │
└─────────────────────────────────────────────────────────────┘
```

## Безопасность параметров подключения

`testclient-bridge` не должен содержать в исходниках реальные URL стендов, логины или пароли.
Параметры тестируемой базы передаются в `connect_test_client` явно (`base_url`, `username`, `password`) или берутся вызывающим скриптом из локального окружения/секретного хранилища.

Встроенная обработка 1С также не содержит рабочих значений по умолчанию: если параметры не переданы, URL/пользователь/пароль остаются пустыми, и подключение должно завершиться ошибкой конфигурации запуска, а не неявным обращением к конкретному стенду.

## Транспорт Python ↔ 1С

Обработка 1С подключается к WebSocket-серверу Python и отправляет hello:

```json
{"type":"hello","role":"bridge","version":"0.2.0","platform":"1c-enterprise"}
```

Команды из Python в 1С:

```json
{"type":"request","id":"<uuid>","method":"active_window","params":{}}
```

Ответы из 1С:

```json
{"type":"response","id":"<uuid>","ok":true,"result":{...}}
{"type":"response","id":"<uuid>","ok":false,"error":"..."}
```

## Конвейер запуска

### One-shot сессия

`start_session()` выполняет:

1. Автоопределение версии платформы (из `base_url` HTML или максимальная установленная)
2. Проверка поддержки WebSocket (требуется 1С 8.5+)
3. Подбор свободного Xvfb display (`:99`–`:199`)
4. Подбор свободных TCP-портов (ws, ibsrv, testclient)
5. Запуск Xvfb
6. Старт WebSocket bridge
7. Опционально (`start_pipeline=True`):
   - `ibcmd infobase create --db-path=<data_dir>/ib --create-database`
   - `ibcmd config load --db-path=<db> build/MCPTestManager.cf`
   - `ibcmd config apply --db-path=<db> --force --dynamic=force --session-terminate=force`
   - `ibsrv --data=<data_dir>/ibsrv-data --config=standalone.yaml --name=mcp`
   - `1cv8c ENTERPRISE /WS"http://127.0.0.1:{ibsrv_port}" /Lru /VLru_RU /TESTMANAGER /C ws://127.0.0.1:{ws_port}/bridge`

Возвращает структуру с `session_id`, `version`, `platform_dir`, `display`, портами, `data_dir`, `bridge_url`, `manager_pid`, `log_dir`.

### Legacy low-level

`launch_manager()` внутри существующей сессии:

1. `ibcmd infobase create --db-path=<db> --create-database` — файловая база.
2. `ibcmd config load --db-path=<db> <MCPTestManager.cf>` — загрузка конфигурации.
3. `ibcmd config apply --db-path=<db> --force --dynamic=force --session-terminate=force` — применение.
4. `ibsrv --data=<data> --config=<standalone.yaml> --name=mcp` — автономный сервер.
5. `1cv8c ENTERPRISE /WS"http://host:port" /Lru /VLru_RU /TESTMANAGER` — менеджер тестирования.

## Модули Python

### `mcp_1c.server` — MCP tools

Точка входа `testclient-bridge`. Регистрирует все MCP tools через `FastMCP`.
Содержит три группы инструментов:

- **Lifecycle**: `start_session`, `stop_session`, `bridge_status`, `runtime_status`, `launch_manager`, `stop_manager`, `export_eventlog`
- **Test client**: `connect_test_client`, `disconnect_test_client`
- **UI automation**: `active_window`, `ui_tree`, `command_bar`, `current_element`, `find_object`, `activate_object`, `execute_window_command`, `click_button`, `set_field_value`, `create_new_item`, `save_form`, `close_form`, `screenshot`
- **Table**: `table_rows`, `table_find_row`, `table_goto_row`, `table_current_row`, `table_selected_rows`, `table_set_field`, `table_edit_info`, `toggle_field`
- **Reference/selection**: `choose_field_from_list`, `choose_field_first_row`, `choose_current_row`
- **RAG**: `rag_source_detect`, `rag_source_add`, `rag_sources_list`, `rag_snapshot_create`, `rag_snapshots_list`, `rag_index_build`, `rag_lookup_object`, `rag_query`

### `mcp_1c.bridge` — WebSocket server

`OneCBridgeServer`:
- Слушает `ws://host:port/bridge`.
- Принимает одно активное подключение от обработки 1С.
- Маршрутизирует JSON-RPC-like запросы: `request` → dispatch → `response`.
- Поддерживает таймаут запросов (по умолчанию 60с).

### `mcp_1c.runtime` — 1C process lifecycle

Управляет процессами 1С:
- `create_file_infobase()` — `ibcmd infobase create`
- `load_configuration()` — `ibcmd config load` + `apply`
- `start_standalone_server()` — `ibsrv`
- `start_thin_test_manager()` — `1cv8c /WS ... /TESTMANAGER`
- `export_eventlog()` — `ibcmd eventlog export`
- `kill_process()` — остановка с эскалацией до SIGKILL
- `is_port_listening()` / `wait_for_port()` — проверка TCP-порта

Датаклассы:
- `InfobaseSpec` — путь к файловой базе
- `StandaloneServerSpec` — адрес/порт/имя/данные ibsrv
- `TestManagerSpec` — путь к 1cv8c
- `ManagerRuntime` — живые процессы + логи

### `mcp_1c.protocol` — типы сообщений

Pydantic-модели для JSON-сообщений WebSocket-протокола:
- `BridgeMessage` — hello/request/response/error
- `RequestMessage` — method + params + id
- `ResponseMessage` — ok + result или error

### `mcp_1c.rag` — RAG metadata index

Локальный SQLite/FTS индекс конфигураций 1С.

**Модель** (`model.py`):
- `SourceInfo` — источник (путь, формат, kind, git-метаданные)
- `MetadataObject` — объект метаданных (полное имя, вид, синоним)
- `Attribute` — поле/реквизит/измерение/ресурс с ролью
- `TablePart` / `TablePartColumn` — табличные части
- `Form` / `FormElement` — формы и их элементы
- `DataCompositionField` — поле СКД отчёта
- `ParsedSource` — результат парсинга одного источника

**Парсер** (`parsers.py`):
- Поддерживает форматы: EDT (`*.mdo`), Configurator XML dump
- Виды объектов: Справочник, Документ, Обработка, Отчет, Перечисление, ОбщийМодуль, РегистрСведений, РегистрНакопления, РегистрБухгалтерии, РегистрРасчета
- Роли полей регистров: `dimension`, `resource`, `attribute`, `standard_attribute`
- СКД отчётов: поля наборов данных, вычисляемые поля, итоги
- Навигационные ссылки: `e1cib/list/...`, `e1cib/app/...`

**Хранилище** (`store.py`):
- SQLite schema v2
- Таблицы: `sources`, `snapshots`, `snapshot_layers`, `metadata_objects`, `attributes`, `table_parts`, `table_part_columns`, `forms`, `form_elements`, `data_composition_fields`, `navigation_links`, `business_terms`, `rag_chunks`
- FTS5: `search_fts` для полнотекстового поиска
- Миграция: `ALTER TABLE attributes ADD COLUMN role`

**Сервис** (`service.py`):
- `RagService` — основной API
- `add_source()` / `list_sources()` — управление источниками
- `create_snapshot()` / `list_snapshots()` — effective snapshots (base + extensions)
- `build_index()` — парсинг + индексация
- `lookup_object()` — поиск объекта с навигацией, полями, формами, СКД
- `query()` — FTS + business terms поиск

**Детектор** (`detect.py`):
- `detect_source_format()` — автоопределение EDT vs designer_xml

## Модуль 1С: обработка MCPTestManager

Расположение: `src/cf/DataProcessors/MCPTestManager/`

### Form module (`Forms/Форма/Ext/Form/Module.bsl`)

Все переменные `&НаКлиенте`:
- `СоединениеWebSocket` — WebSocket-клиент к Python-серверу
- `ТестируемоеПриложение` — API менеджера тестирования
- `КлючСоединенияWebSocket` — идентификатор соединения
- `ПроцессТестКлиента` — процесс тест-клиента
- `ФайлЛога` — путь к `mcp_manager.log`

WebSocket URL: `ws://127.0.0.1:8765/bridge` (хардкод).

Обработчики WebSocket:
- `ПриОткрытии` — hello при подключении
- `ПриПолученииСообщения` — dispatch JSON-команд
- `ПриЗакрытии` — cleanup
- `ПриОшибке` — логгирование

Dispatch команд:
- `ping` → `{"ok":true}`
- `connect_test_client` → запуск `1cv8c /TESTCLIENT -TPort<port>`, `УстановитьСоединение()`
- `disconnect_test_client` → `РазорватьСоединение()`
- `active_window` → `АктивноеОкно.Заголовок`
- `ui_tree` → рекурсивный обход `АктивноеОкно.Содержимое`
- `command_bar` → команды активного окна
- `current_element` → `АктивноеОкно.ТекущийЭлемент`
- `find_object` → поиск в дереве UI
- `activate_object` → `Активизировать()`
- `execute_window_command` → `АктивноеОкно.ВыполнитьКоманду()`
- `click_button` → поиск кнопки + `Нажать()`
- `set_field_value` → `УстановитьЗначениеПоля()`
- `create_new_item` → `АктивноеОкно.ПолучитьКоманду("Создать").Выполнить()`
- `save_form` → `АктивноеОкно.Записать()`
- `close_form` → `АктивноеОкно.Закрыть()`
- `screenshot` → делегирован на Python-сторону (mss)
- `table_*` → `ПолучитьСодержимое()` таблицы, `ТекущаяСтрока`, `ВыделенныеСтроки`
- `choose_field_from_list` → открытие списка выбора + `ВыбратьСтрокуПоПодстроке()`
- `choose_field_first_row` → `ВыбратьПервуюСтроку()`
- `choose_current_row` → `ВыбратьТекущуюСтроку()`
- `toggle_field` → `УстановитьЗначение(НЕ ТекущееЗначение)`

### Object module (`Ext/ObjectModule.bsl`)

Пустой — логика живёт в form module.

### Managed application module (`Ext/ManagedApplicationModule.bsl`)

`ПриНачалеРаботыСистемы` (без параметров, 8.5.1):
- Открывает форму обработки `MCPTestManager`.
- **Не вызывается** в `/TESTMANAGER` на 8.5.1 — форма открывается через `ОбщаяКоманда` с `Autostart=true`.

## 1С 8.5.1: важные особенности

- `/Execute=path` — баг: парсится как один аргумент `=path`. Нужно `/Execute path` (два элемента argv).
- `ibsrv` игнорирует `--http-port`, `--port` без `--config`. Нужен YAML-конфиг.
- `ibcmd config apply` с активными сессиями зависает на диалоге. Использовать `--dynamic=force --session-terminate=force`.
- Тонкий клиент WS URL: `/WS"http://host:port"` (http:// в кавычках, без слеша на конце).
- Толстый клиент не подключается к ibsrv по HTTP.
- `1cv8c /TESTMANAGER /Execute <epf>` — краш, не комбинировать.
- `/CheckModules` — только толстый клиент (`1cv8 DESIGNER`).
- `МенеджерОбработкиОшибок` — server-only; на клиенте `ПодробноеПредставлениеОшибки(Инфо)`.
- `ТекущаяДатаСеанса()` — server-only; на клиенте `ТекущаяДата()`.
- `ПриНачалеРаботыСистемы` в ManagedApplicationModule — 0 параметров в 8.5.1.
- `&НаКлиенте` в ManagedApplicationModule — бессмысленно, модуль уже клиентский.

## RAG: форматы источников

### EDT (`edt`)

Структура: `src/Catalogs/Имя/Имя.mdo`, `src/Documents/Имя/Имя.mdo`, и т.д.
Формы: `src/Catalogs/Имя/Forms/ИмяФормы/Ext/Form.xml`.
СКД: `src/Reports/Имя/Templates/ИмяСхемы/Template.dcs`.

### Configurator XML dump (`designer_xml`)

Структура: `src/cf/Catalogs/Имя.xml`, `src/cf/Documents/Имя.xml`, и т.д.
Формы: `src/cf/Catalogs/Имя/Forms/ИмяФормы/Ext/Form.xml`.

### Snapshot (effective configuration)

Snapshot = base + extensions (в порядке слоёв).
Приоритет: extension переопределяет base.
Индекс строится по всем слоям; `lookup_object` возвращает все совпадения.

## Ограничения

- Одно активное WebSocket-подключение обработки 1С.
- Скриншот делается Python-стороной через `mss`; нужен общий DISPLAY/VNC/Xvfb.
- `/TESTMANAGER` обязателен для управления клиентами тестирования.
- `ManagedApplicationModule.ПриНачалеРаботыСистемы` не вызывается в `/TESTMANAGER` на 8.5.1.
- RAG не парсит `.bsl`-модули (только метаданные: `.mdo`, `.xml`, `.dcs`).
- RAG не индексирует тела запросов СКД (только поля/выражения).

## Переменные окружения

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `ONEC_MCP_WS_HOST` | WebSocket host | `127.0.0.1` |
| `ONEC_MCP_WS_PORT` | WebSocket port | `8765` |
| `ONEC_MCP_REQUEST_TIMEOUT` | Таймаут запроса (сек) | `60` |
| `ONEC_MCP_DATA_DIR` | Рабочая директория | `/tmp/onec-mcp` |
| `ONEC_MCP_SERVER_DATA_DIR` | Данные ibsrv | `/tmp/onec-mcp/ibsrv-data` |
| `MCP_1C_RAG_DB` | Путь к RAG SQLite | `build/rag/onec-rag.sqlite` |
| `DISPLAY` | X11 display | `:99` |

## Dynamic lists and evidence recording

The bridge exposes first-class tools for 1C dynamic-list workflows:

- `dynamic_list_set_order(column_title, name="Список")` — calls the tested table/list method
  `УстановитьПорядок(<ЗаголовокКолонки>)` through the 1C test-client API.
- `dynamic_list_output()` — runs the standard **Вывести список** command and returns detected
  tabular documents, including text when the platform exposes it through the test API.
- `dynamic_list_open_settings()` — opens the standard **Настроить список** dialog used for
  filter/order/conditional-formatting/grouping setup.
- `dynamic_list_clear_settings()` — tries common standard commands for resetting list settings
  back to defaults.
- `dynamic_list_open_form_settings()` — opens **Изменить форму** for form customization.

The exact command names can differ between configurations and languages; each tool accepts an
optional `command` parameter to force the exact command name if the generic candidates do not work.

Evidence recording is implemented on the MCP side. `recording_start()` enables capture; every
recorded bridge tool call saves a screenshot and an SVG slide containing:

- the screenshot of the 1C UI (or whole display);
- the tool name;
- parameters;
- a short JSON/text summary of the result.

`recording_stop()` writes `manifest.json` and builds `recording.gif` / `recording.mp4` with ffmpeg.
If ImageMagick (`magick` or `convert`) is available, it also builds `recording.pdf`.

Window maximization uses `maximize_test_client_external_window()` with priority:

1. `wmctrl` when installed;
2. `xdotool` when installed;
3. `python-xlib` fallback.

The window-control helpers only move/resize/activate OS windows. They do not click or type inside
1C; business interactions remain through the 1C test-client API.

## Ограничения

- Одно активное WebSocket-подключение обработки 1С.
- Скриншот делается Python-стороной через `mss`; нужен общий DISPLAY.
- `/TESTMANAGER` обязателен — это режим тонкого клиента с управлением клиентами тестирования.
- `ManagedApplicationModule.ПриНачалеРаботыСистемы` не вызывается в `/TESTMANAGER` на 8.5.1 — обработка открывается через `ОбщаяКоманда` с `Autostart=true` или через командную панель.
- Форма пользовательской настройки **«Изменить форму»** частично недоступна для надёжной автоматизации через API клиента тестирования. В частности, команда **«Добавить поля»** в верхней панели формы настройки может быть видна на скриншоте и в диагностическом тексте, но не нажиматься как обычная `ТестируемаяКнопкаФормы`: `click_button` может не находить её, а `activate_object` может вернуть `activated=true` без открытия диалога добавления полей. Не используйте X11-клики как обходной путь. Для проверяемых сценариев фильтрации динамических списков используйте **«Настроить список...»** и доступные поля компоновщика; для вывода вложенной колонки через **«Изменить форму»** текущая архитектура не гарантирует выполнение.
