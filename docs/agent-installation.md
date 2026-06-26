# Инструкция для AI-агентов: установка Answer42 и skills

Эта инструкция предназначена для AI-агентов, которые устанавливают Answer42 в пользовательское окружение и подключают MCP-сервер к агентскому клиенту.

## 1. Проверьте окружение

1. Определите ОС и способ запуска агента.
2. Убедитесь, что доступна Python 3.11+.
3. Убедитесь, что установлена платформа 1С:Предприятие 8.3.27+.
4. Для Linux нужен GUI/X11 или Xvfb. Headless-сервер без X11 не подходит для скриншотов и запуска GUI-клиента 1С.
5. Для Windows запускайте Answer42 только из интерактивной desktop-сессии пользователя. Не используйте Windows service, заблокированный RDP-сеанс или неинтерактивный background-runner для UI automation.

## 2. Установите пакет

Linux:

```bash
pipx install 'answer42[screenshot,linux-window-control]'
```

Если `pipx` недоступен:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install 'answer42[screenshot,linux-window-control]'
```

Windows:

```powershell
pipx install "answer42[screenshot,windows-window-control]"
```

Если `pipx` недоступен:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install "answer42[screenshot,windows-window-control]"
```

Проверьте CLI:

```bash
answer42 --help
```

## 3. Установите bundled skills

PyPI-пакет не должен сам записывать skills в профили агентов. После установки пакета выполните:

```bash
answer42 install-skills
```

По умолчанию устанавливаются `answer42` и `answer42-rag` в OpenClaw:

```text
~/.openclaw/workspace/skills
```

Другие варианты:

```bash
answer42 install-skills --agent openclaw
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

Для агентов без стабильного стандарта skill-директорий presets best-effort. Если конкретная установка использует другой путь, укажите `--target-dir` явно.

## 4. Подготовьте credentials

Не передавайте логин и пароль в каждом `start_session`, если можно сохранить их в локальном credentials-файле.

Рекомендуемый вариант — файл вне репозитория, доступный процессу MCP-сервера через `ONEC_MCP_CREDENTIALS_FILE`:

```bash
export ONEC_MCP_CREDENTIALS_FILE=/secure/path/credentials.json
```

Минимальный формат:

```json
{
  "entries": [
    {
      "url": "https://example.invalid/infobase",
      "username": "<USERNAME>",
      "password": "<PASSWORD>"
    }
  ]
}
```

Рекомендуемые права на Linux:

```bash
chmod 600 /secure/path/credentials.json
```

Если MCP уже подключён, можно сохранить запись через tool:

```text
credentials_save(
  url="<TARGET_INFOBASE_URL>",
  username="<USERNAME>",
  password="<PASSWORD>"
)
```

Для проверки используйте:

```text
credentials_check(base_url="<TARGET_INFOBASE_URL>")
```

`credentials_check`, `credentials_save` и `credentials_remove` не возвращают пароль наружу. `credentials_list` рекомендуется скрывать в агентском клиенте, чтобы агент не видел список URL-шаблонов.

## 5. Зарегистрируйте MCP-сервер

### OpenClaw

Минимальный пример:

```bash
/mcp set Answer42={"command":"answer42","args":[],"env":{"ONEC_MCP_CREDENTIALS_FILE":"/secure/path/credentials.json"},"enabled":true,"requestTimeoutMs":120000,"connectionTimeoutMs":30000,"toolFilter":{"exclude":["credentials_list"]}}
```

Если `answer42` установлен в virtualenv и не находится в `PATH`, укажите полный путь к executable, например:

```json
{
  "command": "/path/to/.venv/bin/answer42",
  "args": [],
  "env": {
    "ONEC_MCP_CREDENTIALS_FILE": "/secure/path/credentials.json"
  },
  "enabled": true,
  "requestTimeoutMs": 120000,
  "connectionTimeoutMs": 30000,
  "toolFilter": {
    "exclude": ["credentials_list"]
  }
}
```

После изменения MCP config перезапустите или перезагрузите агентский runtime/gateway, если клиент не подхватил конфигурацию автоматически.

## 6. Проверьте установку

Безопасные проверки:

```bash
answer42 --help
answer42 install-skills --dry-run
```

Через MCP tools:

```text
credentials_check(base_url="<TARGET_INFOBASE_URL>")
session_status()
```

Smoke-проверка живой базы:

```text
start_session(
  session_id="smoke",
  base_url="<TARGET_INFOBASE_URL>",
  idle_timeout_minutes=15
)
active_window(session_id="smoke")
stop_session(session_id="smoke")
```

Если `start_session` сообщает, что передан пароль из звёздочек (`***`, `********`), значит агент получил плейсхолдер вместо реального пароля. Попросите пользователя сохранить корректные credentials или передать настоящий пароль безопасным способом.

## 7. Правила работы после установки

- Работайте через first-class MCP tools Answer42, а не через X11/WinAPI-клики.
- Не запускайте обычный клиент 1С вместо `/TESTMANAGER`, если пользователь явно не просил диагностику вне Answer42.
- Для ссылочных полей предпочитайте стандартные формы выбора 1С.
- После действий проверяйте user messages, error info и фактическое состояние формы.
- Не заявляйте выполнение UI-задачи без реального состояния, скриншота или PDF evidence.
- Завершайте сессии через `stop_session`.
