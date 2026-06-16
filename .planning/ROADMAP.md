# Roadmap — 1C Test Client MCP

## Phase 1 — Transport skeleton

Goal: создать MCP-сервер, WebSocket-транспорт и обработку 1С, которые умеют обмениваться JSON-командами.

Requirements: MCP-01..MCP-03, BRIDGE-01..BRIDGE-03

Success criteria:
1. Python package содержит MCP tools и WebSocket bridge.
2. 1С обработка содержит модуль подключения к WebSocket и диспетчер команд.
3. Документирован JSON-протокол.

## Phase 2 — Test client connection and introspection

Goal: подключать тестируемую базу как клиент тестирования и читать состояние UI.

Requirements: TEST-01..TEST-03, UI-01..UI-03

Success criteria:
1. Команда `connect_test_client` использует `/TESTCLIENT -TPort` и `ТестируемоеПриложение`.
2. Команда `active_window` возвращает базовые свойства активного окна.
3. Команда `ui_tree` сериализует дерево подчиненных объектов с ограничением глубины.

## Phase 3 — Actions, screenshots, documentation

Goal: добавить первое управляющее действие, скриншот и документацию для запуска прототипа.

Requirements: ACT-01, SHOT-01, DOC-01..DOC-02

Success criteria:
1. Команда `click_button` ищет и нажимает кнопку.
2. Команда `screenshot` сохраняет PNG на стороне MCP-хоста.
3. README содержит команды установки, запуска MCP и запуска менеджера 1С.
