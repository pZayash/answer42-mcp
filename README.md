# Answer42

![Answer42 logo](https://gitlab.com/platform42/answer42-mcp/-/raw/v0.2.4/docs/assets/answer42-logo.png)

**Answer42** — MCP-инструмент для интерактивного управления UI **1С:Предприятия** через клиент тестирования. Он позволяет AI-агенту открывать формы 1С, нажимать кнопки, заполнять поля, выбирать ссылки из форм выбора, работать с таблицами, динамическими списками и табличными документами.

> **The Answer to Life, Universe, and 1C — UI Driver**

Проект даёт AI-агенту три способности:

1. **Интерактивное управление UI 1С** — открыть форму, кликнуть кнопку, активировать поле, заполнить значение, выбрать строку, провести сценарий через `/TESTMANAGER` + `/TESTCLIENT`.
2. **Доказательная запись клиентского тестирования** — записывать аннотированные PDF-слайды с MCP method/request/response и скриншотами окна 1С.
3. **RAG-индекс метаданных** — локальный SQLite/FTS индекс конфигураций 1С (EDT, XML-выгрузка Конфигуратора, base + extensions), включая подсказки для `ui_tree` и dynamic-list settings.

## Быстрый старт

```bash
# Рекомендуется для обычной установки из PyPI
pipx install 'answer42[screenshot,linux-window-control]'

# Альтернатива без pipx
python3 -m venv .venv
. .venv/bin/activate
pip install 'answer42[screenshot,linux-window-control]'

# Разработка из git checkout
pip install -e '.[screenshot,dev,linux-window-control]'

# Windows: запускать из интерактивной пользовательской сессии, не как service
# pipx install "answer42[screenshot,windows-window-control]"
# pip install "answer42[screenshot,windows-window-control]"

# Запуск MCP-сервера (transport: stdio)
answer42 --ws-host 127.0.0.1 --ws-port 8765
```

Запуск сессии и подключение тестируемой базы выполняются одним MCP-вызовом:

```text
start_session(
  session_id="my-session",
  base_url="<TARGET_INFOBASE_URL>",
  idle_timeout_minutes=60,
  execute="/path/to/external.epf",  # опционально: /Execute
  command_parameter="InitScenario" # опционально: /C
)
```

`start_session` определяет версию 1С по `base_url`, поднимает инфраструктуру Answer42 (на Linux — Xvfb, если нужен headless; на Windows — интерактивная desktop-сессия), свободные порты, WebSocket bridge, файловую базу менеджера и test-manager. Если доступны серверные компоненты 1С (`ibsrv`), менеджер и встроенная тестовая база публикуются через автономный сервер; если доступен только тонкий клиент и `ibcmd`, Answer42 автоматически использует fallback `file-direct` (`/F`) для файловых баз менеджера и клиента. После `idle_timeout_minutes` минут неактивности сессия автоматически завершается; значение по умолчанию — 60 минут. Для запуска клиента можно передать `extra_args`, `execute` (параметр командной строки `/Execute`) и `command_parameter` (параметр `/C`); если `/C` уже указан в `extra_args`, значения склеиваются через `;`.

Логин/пароль рекомендуется **не передавать явно**: `start_session` умеет брать их из локального credentials-файла по `base_url`. Параметры `username`/`password` остаются в API для разовых сценариев и обратной совместимости, но при передаче в MCP-вызове они видны вызывающему агенту в аргументах tool-call.

## `ui_tree` profiles and output formats

`ui_tree` is optimized for form-structure diagnostics. The default `profile="navigation"` returns a compact visible UI tree without data presentations, RAG enrichment, or command-panel subtrees. The tree is not depth-limited.

Useful modes:

- `ui_tree(parent_name=...)` serializes only the subtree under a known parent element, useful for large/complex forms.
- `ui_tree(name=.../title=.../type=...)` returns a flat `objects` list instead of a nested tree; on current managers filtering is performed on the 1C side and respects `limit`.
- `profile="diagnostic"` includes command panels without enabling expensive data reads.
- `profile="data"` enables node data presentations; prefer targeted `field_value_text`, `table_rows`, `tabular_document_text`, or `tabular_document_save` unless you really need data for many nodes.
- `profile="full"` gives the closest historical verbose result.
- `command_panels="include|exclude|only"` controls command-panel serialization explicitly; `only` returns a deduplicated flat list.
- `group_mode="include|flatten|flatten_layout|exclude|exclude_empty|pages_only"` controls whether group nodes are kept, lifted, or omitted.
- `format="json|outline|yaml|yml"` keeps JSON as the default structured result and offers readable text views for inspection. Invalid profile/group/command-panel/format values are rejected; every result includes `metrics` with timings and payload counters.

## Установка skills

PyPI-пакет не записывает agent skills в пользовательские каталоги автоматически. После установки Answer42 поставьте bundled skills отдельной командой:

```bash
answer42 install-skills
```

По умолчанию команда устанавливает `answer42`, `answer42-rag` и `answer42-universal-report` в OpenClaw:

```text
~/.openclaw/workspace/skills
```

Другие агенты и варианты установки:

```bash
answer42 install-skills --agent claude
answer42 install-skills --agent codex
answer42 install-skills --agent opencode
answer42 install-skills --agent pi
answer42 install-skills --agent hermes
answer42 install-skills --agent all
answer42 install-skills --target-dir /path/to/skills
answer42 install-skills --list-agents
answer42 install-skills --dry-run --agent all
```

Для агентов без стабильного стандарта skill-директорий (`codex`, `opencode`, `pi`, `hermes`) presets best-effort. Если конкретная установка использует другой путь, укажите `--target-dir` явно.

## Инструкция для AI-агентов

Если вы AI-агент и пользователь просит установить Answer42 и skills, используйте отдельную инструкцию: [`docs/agent-installation.md`](docs/agent-installation.md).

Коротко:

1. установите пакет через `pipx` или virtualenv;
2. выполните `answer42 install-skills`;
3. подготовьте `ONEC_MCP_CREDENTIALS_FILE` вне репозитория;
4. зарегистрируйте MCP-сервер в агентском клиенте;
5. проверьте установку через `credentials_check`, затем smoke-сессию `start_session` → `active_window` → `stop_session`.

## Транспорты: stdio и StreamableHTTP

По умолчанию Answer42 использует stdio MCP transport (запускается MCP-хостом как subprocess). Для удалённого deployment доступен **StreamableHTTP** режим с MCP auth:

```bash
answer42 --http --http-host 0.0.0.0 --http-port 8080 \
  --http-token "static-secret-token" \
  --http-account-id "tenant-42"
```

Переменные окружения:

```bash
export ONEC_MCP_HTTP=1
export ONEC_MCP_HTTP_HOST=0.0.0.0
export ONEC_MCP_HTTP_PORT=8080
export ONEC_MCP_HTTP_TOKEN="static-secret-token"
export ONEC_MCP_ACCOUNT_ID="tenant-42"
answer42
```

- Каждый MCP-запрос должен содержать заголовок `Authorization: Bearer <token>`.
- `account_id` определяет namespace для credentials. Если не задан, вычисляется как стабильный хеш токена (`token-<16hex>`).
- Режим по умолчанию **stateless** (`--http-stateless`/`ONEC_MCP_HTTP_STATELESS` default `1`); для resumable sessions передай `--http-stateless`/`0`.

## Безопасное хранение логинов и паролей

Чтобы креды были доступны MCP-серверу, но не попадали в чат и аргументы tool-call, храните их в локальном файле за пределами репозитория и передайте путь в окружение процесса Answer42:

```bash
export ONEC_MCP_CREDENTIALS_FILE=/secure/path/credentials.json
```

Если переменная окружения не задана, MCP-сервер читает файл по умолчанию:

```text
~/.answer42-credentials.json
```

Формат файла v2 (аккаунт-bound):

```json
{
  "version": 2,
  "accounts": {
    "tenant-42": {
      "entries": [
        {
          "url": "https://example.invalid/infobase",
          "username": "<USERNAME>",
          "password": "<PASSWORD>",
          "title": "dev-example",
          "aliases": ["dev42"]
        },
        {
          "url": "https://*.example.invalid/*",
          "username": "<WILDCARD_USERNAME>",
          "password": "<WILDCARD_PASSWORD>",
          "title": "wildcard-example",
          "aliases": ["we"]
        }
      ]
    }
  }
}
```

Устаревший v1 формат (flat `entries`) автоматически загружается в account `legacy` и мигрирует в v2 при следующем сохранении. Рекомендуется явно задать `account_id` (через HTTP auth claim или `ONEC_MCP_ACCOUNT_ID` в stdio) и перенести записи в соответствующий account.

Рекомендуемые права на файл: `0600`.

Правила матчинга внутри аккаунта: сначала точное совпадение `url`, затем wildcard (`*`, `?`) через `fnmatch`; первое совпадение побеждает. Пароли не логируются и не возвращаются наружу.

Для сохранения или обновления записи можно использовать MCP-tool:

```text
credentials_save(
  url="<TARGET_INFOBASE_URL>",
  username="<USERNAME>",
  password="<PASSWORD>"
)
```

Для удаления записи:

```text
credentials_remove(url="<TARGET_INFOBASE_URL>")
```

Для проверки, что для адреса есть сохранённые креды, используйте MCP-tool:

```text
credentials_check(base_url="<TARGET_INFOBASE_URL>")
```

Ответы этих tools содержат только факт наличия/изменения/удаления записи и URL; логины и пароли не возвращаются. `credentials_list()` дополнительно показывает статус проверки (`verified` / `verification_status`): новые или изменённые записи сохраняются как `unverified`, после успешного `start_session` с этой учёткой помечаются как `verified`, а `unverified` запись удаляется при ошибке авторизации. Tool `credentials_list()` оставлен в коде для локальной диагностики, но в OpenClaw-конфигурации его рекомендуется скрывать через `toolFilter.exclude`, чтобы агент не мог получить список URL-шаблонов.

Остановка сессии:

```text
stop_session(session_id="my-session", clean_data=False)
```

## Требования

- Python 3.11+
- 1С:Предприятие **8.3.27+ или 8.5+**
- Пакеты Python: `mcp`, `websockets`, `pydantic`; для скриншотов — `mss`
- Linux/X11: `python-xlib`/`wmctrl`/`xdotool`, GUI/Xvfb для headless-сервера; OS-слой допускается для подготовки геометрии окна (resize/maximize), но не для ввода/кликов/автоматизации 1С
- Windows: интерактивная пользовательская desktop-сессия; window-control и screenshots работают через WinAPI/`mss`, ввод/клики/автоматизация 1С выполняются только через API клиента тестирования

Проверяйте наличие платформы 1С в стандартных каталогах: Linux `/opt/1cv8/x86_64/<version>/` и `/opt/1cv8/i386/<version>/`; Windows `C:\Program Files\1cv8\<version>\bin\` и `C:\Program Files (x86)\1cv8\<version>\bin\`; macOS `/Applications/1cv8/<version>/` или `/opt/1cv8/<version>/`. Для штатной работы нужны `1cv8c` и `ibcmd`; `ibsrv` желателен, но при его отсутствии Answer42 может использовать fallback `/F`. Если автоопределение ошиблось, задайте `ONEC_PLATFORM_DIR`. Для очень медленного старта web-клиента можно увеличить `ONEC_MCP_TEST_CLIENT_READY_TIMEOUT` и timeout MCP-клиента; по умолчанию Answer42 ждёт открытия `-TPort` 55 секунд и затем отдаёт явную ошибку с логами клиента.

## Безопасность стендов и учётных данных

В репозитории не должно быть реальных URL стендов, логинов или паролей. Для штатного запуска передавайте только `base_url`, а логин/пароль храните в локальном credentials-файле, доступном процессу Answer42 через `ONEC_MCP_CREDENTIALS_FILE`.

`start_session` всё ещё принимает `username` и `password` напрямую для разовых сценариев и обратной совместимости. Используйте это только когда осознанно готовы раскрыть значения вызывающему агенту: параметры MCP-вызова могут попасть в историю чата, логи клиента или отладочный вывод. Если вместо реального пароля передан редактированный плейсхолдер из звёздочек (`***`, `********` и т.п.), Answer42 остановит запуск с явной ошибкой: нужно указать настоящий пароль или сохранить корректные креды через `credentials_save()`.

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

Конфигурация менеджера тестирования собирается из XML-исходников `src/cf/` через `ibcmd config import` (приоритетный способ) или через Конфигуратор/DESIGNER (fallback, если `ibcmd` отсутствует):

```bash
python scripts/build_cf.py src/cf build/MCPTestManager.cf
```

PowerShell:

```powershell
python scripts/build_cf.py src/cf build/MCPTestManager.cf
```

Тестовая конфигурация для E2E собирается из XML-исходников `src/client_cf/`:

```bash
python scripts/build_cf.py src/client_cf build/MCPTestClient.cf
```

PowerShell:

```powershell
python scripts/build_cf.py src/client_cf build/MCPTestClient.cf
```

`start_session` автоматически собирает `build/MCPTestManager.cf`, если файл отсутствует или XML-исходники `src/cf/` новее. Встроенный demo-клиент аналогично собирает `build/MCPTestClient.cf` из `src/client_cf/`. E2E-сценарий также пересобирает `build/MCPTestClient.cf`, чтобы тесты не использовали устаревший артефакт.

E2E можно запускать целиком или по независимым сценариям:

```bash
python3 scripts/e2e_stable.py                         # full
E2E_SCENARIO=smoke python3 scripts/e2e_stable.py       # быстрый smoke
E2E_SCENARIO=dynamic python3 scripts/e2e_stable.py     # legacy: таблицы/dynamic-list/отчёт
E2E_SCENARIO=dynamic-tables python3 scripts/e2e_stable.py
E2E_SCENARIO=dynamic-lists python3 scripts/e2e_stable.py
E2E_SCENARIO=dynamic-reports python3 scripts/e2e_stable.py
E2E_SCENARIO=coverage python3 scripts/e2e_stable.py    # diagnostic/negative tools
```

Для реального распараллеливания сценарий умеет сам запустить пять разные сессий (`smoke`, `dynamic-tables`, `dynamic-lists`, `dynamic-reports`, `coverage`), а не копии одного и того же теста. Встроенная тестовая файловая база публикуется через один общий `ibsrv`, когда серверные компоненты доступны; без `ibsrv` demo-клиент запускается через `file-direct` (`/F`):

```bash
E2E_PARALLEL=1 E2E_SESSION_ID=e2e-split python3 scripts/e2e_stable.py
```

`start_session` также переиспользует общий автономный сервер для одинаковой файловой базы. Последний `stop_session` освобождает refcount и завершает shared `ibsrv`.

## Ограничения

- Форма пользовательской настройки **«Изменить форму»** частично недоступна для надёжной автоматизации через API клиента тестирования. В частности, команда **«Добавить поля»** в верхней панели формы настройки может быть видна на скриншоте и в диагностическом тексте, но не нажиматься как обычная `ТестируемаяКнопкаФормы`: `click_button` может не находить её, а `focus_object` может вернуть успешную фокусировку без открытия диалога добавления полей.

## Лицензия

MIT

## Copyright

Copyright (c) 2026 Kosolapov Stanislav aka proDOOMman <prodoomman@gmail.com>, Marvin (AI Assistant), 42Clouds, and contributors.

Licensed under the MIT License.
