# 1C Test Client MCP

MCP-сервер для управления **1С:Предприятием** в режиме клиента тестирования.

Проект состоит из двух частей:

1. Python MCP server — публикует MCP tools и поднимает WebSocket-сервер.
2. Внешняя обработка 1С `MCPTestManager` — запускается в служебной/пустой базе в режиме `/TESTMANAGER`, подключается к WebSocket и управляет тестируемым клиентом 1С через `ТестируемоеПриложение`.

Подробная схема: [`docs/architecture.md`](docs/architecture.md).

## Статус

Первый прототип:

- MCP tools и WebSocket bridge реализованы в `src/mcp_1c/`.
- Исходники внешней обработки 1С лежат в `src/1c/`.
- EPF успешно собирается локальной платформой 1С 8.5 под Xvfb: `build/MCPTestManager.epf`.
- Базовые команды: подключение клиента тестирования, активное окно, дерево UI, командная панель, нажатие кнопки, скриншот.

## Требования

- Python 3.11+
- Пакеты Python из `pyproject.toml`: `mcp`, `websockets`, `pydantic`
- Для скриншотов: extra `screenshot` (`mss`)
- 1С:Предприятие 8.5+ с поддержкой WebSocket-клиентов и режима тестирования
- GUI/Xvfb для запуска клиента 1С на headless-сервере

## Установка

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[screenshot,dev]'
```

## Запуск MCP-сервера

```bash
mcp-1c-test-manager --ws-host 127.0.0.1 --ws-port 8765
```

MCP transport — stdio. WebSocket URL для обработки 1С по умолчанию:

```text
ws://127.0.0.1:8765/bridge
```

## Сборка обработки 1С

Используется skill `epf-build`/скрипт сборки XML external data processor.

На Linux/headless нужен `DISPLAY`, например существующий Xvfb `:99`:

```bash
DISPLAY=:99 python3 /home/openclaw/.openclaw/workspace/cc-1c-skills/.claude/skills/epf-build/scripts/epf-build.py \
  -V8Path /opt/1cv8/x86_64/8.5.1.1150/1cv8 \
  -SourceFile src/1c/MCPTestManager.xml \
  -OutputFile build/MCPTestManager.epf
```

## Запуск менеджера тестирования 1С

Через MCP tool `launch_manager` или вручную:

```bash
/opt/1cv8/x86_64/8.5.1.1150/1cv8 ENTERPRISE \
  /F /path/to/empty-manager-infobase \
  /TESTMANAGER \
  /Execute build/MCPTestManager.epf \
  /C ws://127.0.0.1:8765/bridge
```

Для автономного сервера/служебной базы передавайте актуальные параметры подключения 1С через `launch_manager.extra_args` или запускайте 1С вручную с тем же `/TESTMANAGER /Execute ... /C ...`.

## Подключение тестируемой базы

MCP tool `connect_test_client` по умолчанию использует тестовый стенд:

- URL: `<TARGET_INFOBASE_URL>`
- user: `<USERNAME>`
- password: `<PASSWORD>`
- test client port: `1538`

Команда на стороне 1С стартует тест-клиент с `/TESTCLIENT -TPort<port>`, затем создает `ТестируемоеПриложение` и вызывает `УстановитьСоединение()`.

## MCP tools

- `bridge_status()` — статус WebSocket bridge.
- `launch_manager(onec_path, infobase, processor, extra_args)` — старт менеджера тестирования.
- `connect_test_client(base_url, username, password, port, onec_path)` — подключить тест-клиент.
- `disconnect_test_client()` — отключить тест-клиент.
- `active_window()` — активное окно.
- `ui_tree(max_depth)` — дерево UI.
- `command_bar()` — командный интерфейс/панель.
- `click_button(name, search_by_title)` — нажать кнопку.
- `screenshot(path)` — снять скриншот экрана MCP-хоста.

## Ограничения и TODO

- Сейчас поддерживается одно активное подключение обработки 1С.
- Скриншот делается Python-стороной через `mss`; для удаленного GUI нужен общий DISPLAY/VNC/Xvfb.
- Параметры запуска опубликованных HTTP/WS баз 1С могут потребовать уточнения под конкретный стенд.
- Следующий шаг — прогнать полный E2E: MCP server → 1С manager → WebSocket hello → connect test client на dev-06.
