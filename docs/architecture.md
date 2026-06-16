# Архитектура 1C Test Client MCP

## Компоненты

```text
MCP client (LLM/agent)
        │ stdio MCP
        ▼
Python MCP server (`mcp_1c.server`)
        │ JSON-RPC-like requests
        ▼
WebSocket server (`ws://127.0.0.1:8765/bridge`)
        │
        ▼
1C external processor `MCPTestManager.epf`
        │ 1C test manager API (`ТестируемоеПриложение`)
        ▼
1C test client (`/TESTCLIENT -TPort1538`)
        │
        ▼
Target infobase, e.g. `<TARGET_INFOBASE_URL>`
```

## Транспорт Python ↔ 1С

Обработка 1С подключается к WebSocket-серверу Python и отправляет hello-сообщение:

```json
{"type":"hello","role":"bridge","version":"0.1.0","platform":"1c-enterprise"}
```

Команды отправляются из Python в 1С:

```json
{"type":"request","id":"uuid","method":"active_window","params":{}}
```

Ответы возвращаются из 1С:

```json
{"type":"response","id":"uuid","ok":true,"result":{}}
{"type":"response","id":"uuid","ok":false,"error":"..."}
```

## MCP tools первого прототипа

- `bridge_status` — URL и состояние подключения обработки 1С.
- `launch_manager` — запуск 1С менеджера тестирования с `/Execute <epf>` и `/C <websocket-url>`.
- `connect_test_client` — запуск/подключение тест-клиента к базе.
- `disconnect_test_client` — разрыв соединения.
- `active_window` — состояние активного окна.
- `ui_tree` — дерево UI-объектов активного окна.
- `command_bar` — командный интерфейс/панель, если доступно.
- `click_button` — поиск и нажатие кнопки по имени или заголовку.
- `screenshot` — скриншот экрана на стороне Python-хоста.

## Ограничения текущего прототипа

- Поддерживается одно активное WebSocket-подключение обработки 1С.
- Скриншот делается системным способом через Python `mss`, потому что в справке API тестируемого приложения не найден прямой метод снимка экрана.
- Команда запуска тест-клиента может потребовать правки параметров платформы под конкретный режим публикации/автономного сервера.
- Исходники обработки созданы как XML external data processor; сборка EPF выполняется локальной платформой 1С.
