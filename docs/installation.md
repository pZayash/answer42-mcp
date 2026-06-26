# Установка Answer42

## Python-пакет

```bash
git clone https://gitlab.com/platform42/answer42-mcp.git answer42
cd answer42
python3 -m venv .venv
. .venv/bin/activate
# Linux/X11
pip install -e '.[screenshot,linux-window-control]'

# Windows
# pip install -e ".[screenshot,windows-window-control]"
```

## Сборка конфигураций 1С

Сборка через `ibcmd config import` (приоритетный способ, не требует Конфигуратора):

```bash
python scripts/build_cf.py src/cf build/MCPTestManager.cf
```

Сборка тестовой конфигурации:

```bash
python scripts/build_cf.py src/client_cf build/MCPTestClient.cf
```

Скрипт сам добавляет `src` в `sys.path`, поэтому `PYTHONPATH=src` не нужен. Если `ibcmd` отсутствует, скрипт автоматически переключается на Конфигуратор/DESIGNER через `1cv8`. На PowerShell синтаксис `PYTHONPATH=src python ...` не работает; если когда-нибудь нужно задать переменную окружения вручную, используйте `$env:PYTHONPATH="src"; python ...`.

`start_session` автоматически собирает `build/MCPTestManager.cf`, если файл отсутствует или XML-исходники `src/cf/` новее. Встроенная demo-база клиента автоматически собирает `build/MCPTestClient.cf` из `src/client_cf/`.

На Linux без серверных компонентов Answer42 может запускать менеджер и клиент напрямую через файловую базу (`/F`). На Windows это также поддерживается; запускайте MCP из интерактивной пользовательской сессии, потому что UI automation и screenshots требуют desktop session.

## MCP-сервер в OpenClaw

OpenClaw хранит MCP-серверы в `mcp.servers`. Добавьте `Answer42` как stdio server:

```bash
/mcp set Answer42={"command":"/home/openclaw/.openclaw/workspace/answer42/.venv/bin/answer42","args":[],"cwd":"/home/openclaw/.openclaw/workspace/answer42","env":{"ONEC_MCP_CREDENTIALS_FILE":"/secure/path/credentials.json"},"enabled":true,"requestTimeoutMs":120000,"connectionTimeoutMs":30000,"toolFilter":{"exclude":["credentials_list"]}}
```

`ONEC_MCP_CREDENTIALS_FILE` указывает на локальный JSON с логинами и паролями. В штатном режиме агент вызывает `start_session(base_url="...")` без `username`/`password`, а Answer42 сам подставляет креды внутри MCP-процесса. Для сохранения или обновления записи используйте tool `credentials_save(url="...", username="...", password="...")`, для удаления — `credentials_remove(url="...")`, для проверки наличия записи — `credentials_check(base_url="...")`; эти tools не возвращают секреты. `credentials_list` рекомендуется скрывать через `toolFilter.exclude`, чтобы агент не видел список URL-шаблонов и логинов. Если в `start_session`/`connect_test_client` передан пароль вида `***`, Answer42 считает это плейсхолдером и выдаёт явную ошибку вместо попытки авторизации.

После изменения MCP config перезапустите или перезагрузите OpenClaw gateway, если hot reload не подхватил конфигурацию.

## Skills

Репозиторий содержит skills для агентов:

- `skills/answer42/SKILL.md`
- `skills/answer42-rag/SKILL.md`

Установка в workspace skills directory:

```bash
mkdir -p ~/.openclaw/workspace/skills
cp -a skills/answer42 ~/.openclaw/workspace/skills/
cp -a skills/answer42-rag ~/.openclaw/workspace/skills/
```

## Smoke checks

```bash
python -m pytest -q
python scripts/build_cf.py src/cf build/MCPTestManager.cf
python scripts/build_cf.py src/client_cf build/MCPTestClient.cf
```

Полный E2E:

```bash
# Linux/X11: DISPLAY=:99 python scripts/e2e_stable.py
# Windows:  python scripts/e2e_stable.py
```
