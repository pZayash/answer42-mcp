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
    display: str              # Xvfb display, например ":99"
    platform_dir: Path        # /opt/1cv8/x86_64/8.3.27.1606
    ws_port: int              # порт WebSocket bridge
    ibsrv_port: int           # порт HTTP-gate автономного сервера
    testclient_port: int      # TCP-порт клиента тестирования
    data_dir: Path            # /tmp/onec-mcp/sessions/{session_id}
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
2. Проверяет требование: **1С:Предприятие 8.3.27+**.
3. Подбирает свободный Xvfb display.
4. Подбирает свободные TCP-порты для bridge, ibsrv и test-client.
5. Запускает Xvfb и WebSocket bridge.
6. При `start_pipeline=True` создаёт файловую базу менеджера, загружает `MCPTestManager.cf`, запускает ibsrv и test-manager.
7. Подключает клиент тестирования к `base_url` или к локальной тестовой базе, созданной для E2E. Если `username`/`password` не переданы, Answer42 ищет их в локальном credentials-файле по `base_url`; это основной безопасный режим, потому что секреты остаются в окружении MCP-процесса и не передаются агенту в аргументах tool-call. Дополнительные параметры запуска клиента передаются через `extra_args`, `execute` (`/Execute`) и `command_parameter` (`/C`); если `/C` уже есть в `extra_args`, части значения объединяются через `;`.
8. Запоминает RAG snapshot, привязанный к `base_url`, если такой snapshot уже был создан раньше.
9. Запускает idle watcher: после `idle_timeout_minutes` минут неактивности выполняется `stop_session()`.

## Python-модули

### `mcp_1c.server`

Точка входа `answer42`. Регистрирует MCP tools через `FastMCP` и управляет `SessionState`.

Группы tools:

- жизненный цикл: `start_session`, `stop_session`, `session_status`, `credentials_check`;
- автоматизация UI: `active_window`, `windows_list`, `activate_window`, `goto_start_page`, `goto_previous_window`, `goto_next_window`, `ui_tree`, `find_object`, `activate_object`, `open_navigation_link`, `click_button`, `set_field_value`, `save_form`, `close_form`, `screenshot`;
- таблицы: `table_rows`, `table_find_row`, `table_goto_row`, `table_current_row`, `table_set_field`, `table_choose_field_from_list`, `table_move_row`;
- динамические списки: `dynamic_list_available_fields`, `dynamic_list_apply_settings`, `dynamic_list_set_order`, `dynamic_list_output`, `dynamic_list_open_settings`, `dynamic_list_clear_settings`, `dynamic_list_open_form_settings`;
- табличные документы: `tabular_documents`, `tabular_document_text`, `tabular_document_save`;
- доказательная запись: `recording_start`, `recording_capture`, `recording_stop`;
- RAG: `rag_source_add`, `rag_sources_list`, `rag_snapshot_create`, `rag_snapshots_list`, `rag_index_build`, `rag_lookup_object`, `rag_query`.

### `mcp_1c.bridge`

`OneCBridgeServer` слушает WebSocket endpoint и маршрутизирует JSON-RPC-like сообщения между Python и обработкой 1С.

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

## Доказательная запись

`recording_start()` включает запись. Каждый записываемый bridge-вызов сохраняет слайд с:

- именем MCP tool;
- параметрами вызова;
- кратким ответом;
- скриншотом окна 1С или text-only содержимым для RAG-вызовов.

`recording_stop()` собирает `manifest.json`, MP4 и PDF, если доступен backend PDF.

## Переменные окружения

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `ONEC_MCP_WS_HOST` | WebSocket host | `127.0.0.1` |
| `ONEC_MCP_WS_PORT` | WebSocket port | `8765` |
| `ONEC_MCP_REQUEST_TIMEOUT` | Таймаут запроса, сек | `60` |
| `ONEC_MCP_DATA_DIR` | Рабочая директория | `/tmp/onec-mcp` |
| `MCP_1C_RAG_DB` | Путь к RAG SQLite | `build/rag/onec-rag.sqlite` |
| `DISPLAY` | X11 display | `:99` |
