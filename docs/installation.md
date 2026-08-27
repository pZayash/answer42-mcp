# Установка Answer42

## Python-пакет

Рекомендуемая установка из PyPI:

```bash
pipx install 'answer42[screenshot,linux-window-control]'
# или без pipx:
python3 -m venv .venv
. .venv/bin/activate
pip install 'answer42[screenshot,linux-window-control]'
```

Windows запускать из интерактивной desktop-сессии:

```powershell
pipx install "answer42[screenshot,windows-window-control]"
# или:
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install "answer42[screenshot,windows-window-control]"
```

Установка для разработки из git checkout:

```bash
git clone https://gitlab.com/platform42/answer42-mcp.git answer42
cd answer42
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[screenshot,dev,linux-window-control]'
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

## Установка skills

PyPI-пакет не может сам записывать skills в профили агентов. После установки пакета выполните:

```bash
answer42 install-skills
```

По умолчанию устанавливаются `answer42` и `answer42-rag` для OpenClaw в `~/.openclaw/workspace/skills`.

Для других агентов или явного пути:

```bash
answer42 install-skills --agent claude
answer42 install-skills --agent codex
answer42 install-skills --agent opencode
answer42 install-skills --agent pi
answer42 install-skills --agent hermes
answer42 install-skills --target-dir /path/to/skills
answer42 install-skills --list-agents
```

## MCP-сервер в OpenClaw

OpenClaw хранит MCP-серверы в `mcp.servers`. Добавьте `Answer42` как stdio server:

```bash
/mcp set Answer42={"command":"answer42","args":[],"env":{"ONEC_MCP_CREDENTIALS_FILE":"/secure/path/credentials.json"},"enabled":true,"timeout":600,"connectionTimeoutMs":30000,"toolFilter":{"exclude":["credentials_list"]}}
```

Для OpenClaw новых инсталляций задавайте внешний MCP request timeout для Answer42 с запасом: рекомендуемое значение — **600 секунд** (`timeout: 600` или эквивалентный `requestTimeoutMs: 600000`). Не ставьте его равным самым длинным внутренним timeout'ам tools: например, если агент вызывает `wait_client_idle(timeout=180)` или `tabular_document_save(watchdog_timeout=180)`, внешний MCP timeout должен быть больше этого значения и покрывать накладные расходы транспорта, сериализацию результата, диагностику и медленные ответы web-клиента 1С. Для тяжёлых отчётов, сохранения больших табличных документов или очень медленных web-баз допустимо увеличить внешний timeout до 900–1200 секунд, но обычные интерактивные действия лучше держать с меньшими `watchdog_timeout`, чтобы зависания диагностировались структурированным ответом, а не обрывались внешним MCP timeout.

`ONEC_MCP_CREDENTIALS_FILE` указывает на локальный JSON с логинами и паролями. В штатном режиме агент вызывает `start_session(base_url="...")` без `username`/`password`, а Answer42 сам подставляет креды внутри MCP-процесса. Для сохранения или обновления записи используйте tool `credentials_save(url="...", username="...", password="...")`, для удаления — `credentials_remove(url="...")`, для проверки наличия записи — `credentials_check(base_url="...")`; эти tools не возвращают ни логин, ни пароль, ни маску пароля. `credentials_list` рекомендуется скрывать через `toolFilter.exclude`, чтобы агент не видел список URL-шаблонов. Если в `start_session`/`connect_test_client` передан пароль вида `***`, Answer42 считает это плейсхолдером и выдаёт явную ошибку вместо попытки авторизации.

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
