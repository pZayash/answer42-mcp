# Архитектура Answer42

<p align="center">
  <img src="assets/answer42-logo.png" alt="Answer42 logo" width="180">
</p>

## Обзор

`Answer42` — MCP-сервер для интерактивного управления UI 1С:Предприятия через клиент тестирования.

Основные подсистемы:

1. **UI Driver** — запуск менеджера тестирования, подключение тест-клиента и выполнение интерактивных операций над формами 1С.
2. **Доказательная запись** — запись аннотированных слайдов с MCP method/request/response и скриншотом окна 1С.
3. **RAG metadata index** — SQLite/FTS индекс метаданных конфигураций и расширений 1С для подсказок агенту и обогащения `ui_tree` типами элементов формы.

## Сессии

Каждая сессия Answer42 изолирует процессы и временные ресурсы:

```python
class SessionState:
    session_id: str
    display: str              # X11 display (Linux) или пустая строка (Windows)
    platform_dir: Path        # путь к платформе 1С (Linux: /opt/1cv8/..., Windows: C:\Program Files\1cv8\...)
    ws_port: int              # порт WebSocket bridge
    ibsrv_port: int           # порт HTTP-gate автономного сервера (0 если file-direct fallback)
    testclient_port: int      # TCP-порт клиента тестирования
    data_dir: Path            # временная директория сессии (tempdir/answer42/sessions/{session_id})
    base_url: str
    username: str
    password: str
    xvfb_process: Popen | None
    runtime: ManagerRuntime | None
    bridge: OneCBridgeServer
    recorder: RecordingSession
```

Если `session_id` не указан в tool-вызове, используется сессия `default`.

## Общая схема

Поток выполнения:

1. **MCP-клиент** — LLM/агент вызывает MCP tools через stdio.
2. **Python MCP server Answer42** — принимает MCP-вызовы, ведёт сессии, RAG и доказательную запись.
3. **WebSocket bridge** — передаёт JSON-RPC-like команды между Python и 1С.
4. **1С test manager** — тонкий клиент с обработкой `MCPTestManager`; выполняет BSL-dispatch к API клиента тестирования.
5. **1С test client** — тестируемое приложение, подключённое к целевой информационной базе.
6. **Целевая информационная база** — база, по UI которой агент выполняет интерактивные операции.

Передача управления:

| Шаг | Откуда | Куда | Канал |
|---:|---|---|---|
| 1 | MCP-клиент | Answer42 | stdio MCP |
| 2 | Answer42 | WebSocket bridge | локальный TCP/WebSocket |
| 3 | WebSocket bridge | 1С test manager | WebSocket client из 1С |
| 4 | 1С test manager | 1С test client | API `ТестируемоеПриложение` |
| 5 | 1С test client | Целевая ИБ | стандартное подключение 1С |

## Конвейер one-shot запуска

`start_session()` выполняет полный запуск автоматически:

1. Определяет версию платформы по `base_url` или использует переданную.
2. Проверяет требование: **1С:Предприятие 8.3.27+ или 8.5+**.
3. Подбирает свободный X11 display (Linux) или пропускает (Windows).
4. Подбирает свободные TCP-порты для bridge, ibsrv и test-client.
5. Запускает Xvfb (Linux) и WebSocket bridge.
6. Создаёт файловую базу менеджера, загружает `MCPTestManager.cf`. Если `ibsrv` доступен — запускает автономный сервер и test-manager через `/WS`; если нет — запускает test-manager напрямую через `/F` (file-direct fallback).
7. Подключает клиент тестирования к `base_url` или к локальной тестовой базе, созданной для E2E. Если `username`/`password` не переданы, Answer42 ищет их в локальном credentials-файле по `base_url`; это основной безопасный режим, потому что секреты остаются в окружении MCP-процесса и не передаются агенту в аргументах tool-call. Дополнительные параметры запуска клиента передаются через `extra_args`, `execute` (`/Execute`) и `command_parameter` (`/C`); если `/C` уже есть в `extra_args`, части значения объединяются через `;`.
8. Запоминает RAG snapshot, привязанный к `base_url`, если такой snapshot уже был создан раньше.
9. Запускает idle watcher: после `idle_timeout_minutes` минут неактивности выполняется `stop_session()`.

## Python-модули

### `mcp_1c.server`

Точка входа `answer42`. Регистрирует MCP tools через `FastMCP` и управляет `SessionState`.

Группы tools:

- жизненный цикл: `start_session`, `stop_session`, `session_status`, `credentials_check`;
- автоматизация UI: `active_window`, `windows_list`, `activate_window`, `goto_window`, `ui_tree`, `find_object`, `focus_object`, `open_navigation_link`, `click_button`, `set_field_value`, `save_form`, `close_form`, `screenshot`;
- таблицы: `table_rows`, `table_find_row`, `table_goto_row`, `table_current_row`, `table_set_field`, `table_choose_field_from_list`, `table_move_row`;
- динамические списки: `dynamic_list_metadata_available_fields`, `dynamic_list_settings_available_fields`, `dynamic_list_set_settings`, `dynamic_list_current_settings`, `dynamic_list_find`, `dynamic_list_sort_by_column`, `dynamic_list_output`, `dynamic_list_open_settings`, `dynamic_list_clear_settings`, `form_open_settings`;

`dynamic_list_set_settings` открывает стандартную форму **Настройка списка** и изменяет пользовательские настройки именно в ней. Строки отбора/сортировки добавляются через таблицу **Доступные поля** и кнопку **Выбрать**, а параметры добавленных строк затем заполняются через table-editor таблиц настроек: для отборов — колонки `Вид сравнения` и `Значение`, для сортировки — колонка `Направление сортировки`. Tool не использует post-close/list-level fallback для `orders`: если направление не удалось записать в форме настроек, вызов должен вернуть ошибку элемента, а не маскировать её сортировкой самого динамического списка.

`dynamic_list_sort_by_column` — отдельный быстрый tool для сортировки уже открытого динамического списка по видимой колонке через API списка. Он не является fallback-веткой `dynamic_list_set_settings`.
- табличные документы: `tabular_documents` (список или ячейки одного документа по `name`/`title`), `tabular_document_save`;
- доказательная запись: `recording_start`, `recording_capture`, `recording_stop`;
- RAG: `rag_source_add`, `rag_sources_add_from_directory`, `rag_source_roots_list`, `rag_sources_list`, `rag_available_configurations`, `rag_snapshot_create`, `rag_snapshots_list`, `rag_index_build`, `rag_lookup_object`, `rag_query`, `rag_session_autoconfigure`.

`rag_session_autoconfigure` умеет fallback-выгрузку конфигурации и расширений через Designer `DumpConfigToFiles`, если подходящий RAG-источник не найден. Для веб-сессий можно передать `dump_connection_value` (прямой сервер/файловая база) или `cf_files`/`cfe_files` (загружаются во временную базу, выгружаются в XML, временная база удаляется).

### `mcp_1c.bridge`

`OneCBridgeServer` слушает WebSocket endpoint и маршрутизирует JSON-RPC-like сообщения между Python и обработкой 1С.

### `mcp_1c.os_support`

Кроссплатформенные OS-хелперы: `file_lock` (POSIX `fcntl` / Windows `msvcrt`), `kill_process_tree` (SIGTERM/SIGKILL / `taskkill`), `subprocess_creation_kwargs` (start_new_session / CREATE_NEW_PROCESS_GROUP), `executable_name` (`.exe` на Windows), `default_data_dir` (tempdir/answer42).

### `mcp_1c.platform`

Поиск установленной платформы 1С: Linux (`/opt/1cv8/x86_64/<version>/`, `/opt/1cv8/i386/<version>/`), Windows (`C:\Program Files\1cv8\<version>\bin`, `C:\Program Files (x86)\1cv8\<version>\bin`), macOS/manual installs (`/Applications/1cv8/<version>/`, `/opt/1cv8/<version>/`), переопределение через `ONEC_PLATFORM_DIR`. Кандидат платформы должен содержать минимум `1cv8c` и `ibcmd`; при одинаковой версии предпочтение отдаётся установке с `ibsrv`. Проверка версии и поддержки WebSocket: **8.3.27+ или 8.5+**.

### `mcp_1c.window_control`

OS-specific слой окна тест-клиента: Linux (`wmctrl`/`xdotool`/`python-xlib`, `xwininfo`), Windows (WinAPI; для скрытых рабочих столов — через `mcp_1c.win_desktop`). Допускается для подготовки геометрии окна и скриншотов (resize/maximize/geometry capture). Ввод, клики и результат действий 1С не определяются через X11/WinAPI; автоматизация 1С идёт через API клиента тестирования.

### `mcp_1c.win_desktop`

Windows-аналог Xvfb: скрытые рабочие столы (`CreateDesktopW` в `WinSta0`), `DesktopPopen` — `subprocess.Popen` с `STARTUPINFO.lpDesktop` (CPython его не поддерживает), перечисление окон рабочего стола, `PrintWindow`-скриншоты и maximize из вспомогательного потока, привязанного к рабочему столу через `SetThreadDesktop`. Включается `ONEC_MCP_WINDOWS_DESKTOP=hidden`; тогда `session.display`/`client_display` содержат имена рабочих столов.

### `mcp_1c.runtime`

Управляет процессами 1С:

- создание файловой базы;
- загрузка и применение `.cf`;
- запуск автономного сервера;
- запуск тонкого клиента в режиме `/TESTMANAGER`;
- остановка процессов;
- ожидание портов и проверка runtime-состояния.

### `mcp_1c.rag`

RAG-индекс хранит сведения о конфигурации и расширениях:

- объекты метаданных;
- реквизиты и стандартные реквизиты;
- табличные части и колонки;
- формы и элементы формы;
- поля СКД;
- навигационные ссылки;
- полнотекстовые chunks для FTS.

Поддерживаются источники EDT и XML-выгрузки Конфигуратора. Snapshot может объединять базовую конфигурацию и расширения, например `Configuration + Extension1`.

## RAG и автогенерируемые формы 1С

1С умеет автоматически генерировать формы элементов, списков и выбора по метаданным объекта. В таких формах типы полей совпадают с типами соответствующих реквизитов, стандартных реквизитов и колонок табличных частей.

Answer42 использует это правило при RAG-обогащении `ui_tree`:

- если элемент формы явно найден в исходниках формы — берётся тип из формы и связанного реквизита;
- если явной формы или элемента нет — строятся synthetic form elements по реквизитам метаданных объекта;
- для автогенерируемых форм поле с именем/заголовком реквизита получает тип реквизита;
- для dynamic-list полей используются реквизиты объекта, стандартные реквизиты и поля СКД, если они есть в snapshot.

Это позволяет агенту понимать типы полей даже на типовых автогенерируемых формах без явного `Form.xml`.

## Форматы источников RAG

### EDT

```text
src/Catalogs/Имя/Имя.mdo
src/Catalogs/Имя/Forms/ФормаЭлемента/Ext/Form.xml
src/Reports/Имя/Templates/ИмяСхемы/Template.dcs
```

### XML-выгрузка Конфигуратора

```text
src/cf/Catalogs/Имя.xml
src/cf/Catalogs/Имя/Forms/ФормаЭлемента/Ext/Form.xml
src/cf/Reports/Имя/Templates/ИмяСхемы/Ext/Template.xml
```

### Snapshot

```bash
python3 scripts/rag_cli.py add-source Configuration /path/to/configuration/src --kind base
python3 scripts/rag_cli.py add-source Extension1 /path/to/extension/src --kind extension
python3 scripts/rag_cli.py snapshot Configuration+Extension1 --base Configuration --extension Extension1
python3 scripts/rag_cli.py build --snapshot Configuration+Extension1
python3 scripts/rag_cli.py lookup РегистрСведений.Регистр1 --snapshot Configuration+Extension1
```

### Discovery roots

MCP tool `rag_sources_add_from_directory(path, watch=true)` scans a directory for
EDT/Designer XML configuration and extension roots, registers discovered sources,
and stores the directory in `source_roots`. Later calls to `rag_sources_list`,
`rag_source_roots_list` or `rag_available_configurations` can sync those roots and
pick up newly added source trees automatically. Discovery is bounded by
`max_depth` and skips noisy directories such as `.git`, `.venv`, `build`, `dist`
and `node_modules`.

`rag_available_configurations` returns grouped base/configuration sources,
extensions, other sources, snapshots, discovery roots and per-source indexed
object/chunk counts.

If a RAG lookup/query runs without a session snapshot, or the selected snapshot
has no indexed data, the result includes `rag_diagnostic.diagnostics[]` with a
machine-readable code and remediation message for the agent.

`start_session()` defaults to `rag_enabled=false` so ordinary sessions do not pay
for live configuration registry reads. To bind a summary snapshot explicitly,
pass `summary_rag_config`, for example `summary_rag_config="bp"`. Supported
aliases include summary repository codes (`bp`, `bpc`, `ka`, `ut`, `zup`, etc.),
1C identifiers (`Accounting30`, `AccountingCorp30`, `ARAutomation20`, etc.) and
common Russian names such as `Бухгалтерия предприятия`, `Комплексная автоматизация`,
`Управление торговлей` and `Зарплата и управление персоналом`; Answer42 creates a
`configuration+fresh` summary snapshot without live autodetection and
returns `rag_notice`. Successful explicit binding reports
`rag_notice.enabled=true`, `backend="configuration-summary"`, the selected
snapshot, layers and `summary_backend` transport/project/ref. If the summary
configuration is not found or RAG is disabled, `rag_notice.enabled=false` and the
`reason`/`message` explain why no binding was applied.

The older live autodetection path for `start_session(rag_enabled=true)` is kept
behind `ONEC_MCP_AUTO_SUMMARY_RAG_ON_START=1`. It reads
`РегистрСведений.ВерсииПодсистем`, matches the configured summary repository,
adds available extension layers from `ВерсииРасширений`, and can be slower on
large remote infobases.

## Доказательная запись

`recording_start()` включает запись. Каждый записываемый bridge-вызов сохраняет слайд с:

- именем MCP tool;
- параметрами вызова;
- кратким ответом;
- скриншотом окна 1С или text-only содержимым для RAG-вызовов.

`recording_stop()` собирает `manifest.json` и `recording.pdf`, если доступен backend PDF. MP4/GIF больше не генерируются.

## Переменные окружения

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `ONEC_MCP_WS_HOST` | WebSocket host | `127.0.0.1` |
| `ONEC_MCP_WS_PORT` | WebSocket port | `8765` |
| `ONEC_MCP_REQUEST_TIMEOUT` | Таймаут запроса, сек | `60` |
| `ONEC_MCP_WINDOWS_DESKTOP` | Windows: `hidden` — запускать менеджер и клиент 1С на скрытых рабочих столах сессии; `current` — на текущем рабочем столе | `current` |
| `ONEC_MCP_DATA_DIR` | Рабочая директория | OS tempdir + `answer42` (`/tmp/answer42` on Linux, `%TEMP%\answer42` on Windows) |
| `MCP_1C_RAG_DB` | Путь к RAG SQLite | `build/rag/onec-rag.sqlite` |
| `ONEC_MCP_DISABLE_RAG` | Отключить встроенный RAG (`1/true/yes/on`) | пусто |
| `ONEC_MCP_RAG_SOURCE_DIRS` | `:`/`;`-separated directories for startup RAG discovery/watch | пусто |
| `ONEC_MCP_RAG_SOURCE_PREFIX` | Prefix for auto-discovered source names | пусто |
| `ONEC_MCP_RAG_SOURCE_MAX_DEPTH` | Discovery depth for RAG source dirs | `4` |
| `DISPLAY` | X11 display | `:99` |


## `ui_tree` output profiles

`ui_tree` is intended for UI structure diagnostics, not bulk data extraction.
The default `profile="navigation"` returns visible UI structure with compact fields,
`include_data=false`, `include_rag_types=false`, and command panels excluded. The tree
has no depth limit. Use targeted tools such as `field_info`, `table_rows`,
`tabular_documents(name=...)`, or `tabular_document_save` when values/report data are
needed.

Supported presentation controls:

- `profile="navigation"` — lightweight default for navigating forms.
- `profile="diagnostic"` — includes command panels and diagnostic state, without data.
- `profile="data"` — includes data presentations; use sparingly on large forms.
- `profile="full"` — closest to the historical verbose behavior, including RAG when available.
- `profile="command_panels"` or `command_panels="only"` — returns command-panel nodes as a deduplicated flat list.
- `include_command_panels` / `command_panels` — explicitly include, exclude, or return only command panels.
- `parent_name` — serializes only the subtree rooted at a specific technical element name.
- `name`, `title`, `type` — filter nodes; active filters return a flat `objects` list and are pushed to the 1C manager on current protocol versions.
- `group_mode="include|flatten|flatten_layout|exclude|exclude_empty|pages_only"` — controls whether group nodes are returned as regular nodes, lifted, or omitted.
- `format="json|outline|yaml|yml"` — JSON remains the structured default; outline/YAML are readable text views wrapped in a structured result. Mode arguments are strictly validated, and responses include `metrics` for timing/payload diagnostics.
