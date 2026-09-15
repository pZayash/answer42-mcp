# pZayash/answer42-mcp — форк Answer42

Форк [Answer42](https://gitlab.com/platform42/answer42-mcp) (MIT, 42Clouds /
S. Kosolapov). `beta` — зеркало upstream, `kpsr` — локальные патчи поверх
релизного тега. Лицензия и копирайт upstream сохранены (`LICENSE` не изменялся).

## Зачем форк

**Текст ошибок tool-call.** MCP-SDK 2.x (`mcp/server/mcpserver/tools/base.py`)
отдаёт MCP-клиенту текст только у `ToolError`; любое другое исключение
превращается в `UnexpectedToolError("Error executing tool <name>")` без деталей,
а исходный текст остаётся только в логе сервера.

Upstream бросает обычные `RuntimeError`/`ValueError` (в том числе
`open_navigation_link` → `RuntimeError(str(exc) + suggestions)`), поэтому агент
при любом сбое видит голое `Error executing tool <name>`, а собственный
`scripts/e2e_stable.py` upstream падает на ассерте ожидаемого текста.

Патч (ветка `kpsr`, 1 файл): в существующем враппере вызова тулов достаём
исходный текст из цепочки `__cause__`/`__context__` и поднимаем `ToolError` —
сообщение снова доходит до клиента.

Было: `Error executing tool open_navigation_link` (26 символов).
Стало: `Не удалось перейти по навигационной ссылке e1cib/…: …` + `client_diagnostics` (JSON).

## Сборка и установка

Устанавливать из чекаута в **латинском пути** (кириллица в пути ломает
editable-установку: `.pth` с не-ASCII путём не подхватывается):

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[screenshot,windows-window-control]"
```

`MCPTestManager.cf` / `MCPTestClient.cf` собираются локально из XML при первом
`start_session` (нужны `ibcmd` и установленная платформа 1С 8.3.27+/8.5+).

## Обновление с upstream

```bash
git fetch upstream --tags
git switch kpsr
git rebase <новый-тег>        # обычный случай
git push --force-with-lease origin kpsr
```

Если патч перестал применяться — смотреть `src/mcp_1c/server.py`,
функция `_install_tool_call_logger` (`_logged_call_tool`) и `_tool_error_text`.

## Проверка

```bash
answer42 ... open_navigation_link '{"session_id":"ui","navigation_link":"e1cib/list/Catalog.Missing"}'
# ожидается текст ошибки, а не голая строка "Error executing tool ..."
```
