# План реализации

Цель одна — финальный дизайн из `DESIGN.md` с решениями из `DECISIONS.md`. Единицы ниже —
порядок сборки, а не «версии»: каждая единица заканчивается проверяемым критерием, и
только потом начинается следующая. Никаких временных упрощений, которые потом
«доделаем».

Языки: код, комментарии, промпты, скиллы, коммиты — английский. Ответы агента —
на языке запроса.

## 0. Каркас репозитория

```
daedalus/
├── pyproject.toml            uv, python ≥3.12, зависимости: protocore (git: protocore-exp), aiogram, fastapi,
│                             uvicorn, httpx, aiosqlite, pydantic-settings, croniter, watchfiles
├── daedalus/
│   ├── __main__.py           `python -m daedalus` — единственная точка входа приложения
│   ├── config.py             Settings (pydantic-settings): .env + config.toml на volume
│   ├── app.py                композиция: stores → registry → transport → api → scheduler
│   ├── host/                 «хост» в терминах ядра
│   │   ├── engine_factory.py построить QueryEngine для сессии (rc, prompt sections, live control)
│   │   ├── session_runner.py одна сессия = один asyncio.Task; события → sinks
│   │   ├── live_control.py   reload_/persist_live_control: steer / follow-up / смена модели
│   │   ├── prompts/          system-prompt секции (persona, workspace rules, self-develop rules)
│   │   └── skills.py         ISkillStore над каталогом skills/
│   ├── providers/            ILLMProvider: openai_compat.py, deepseek.py, openrouter.py, vllm.py, chain.py
│   ├── tools/                по файлу на тул, автоскан каталога при старте
│   ├── stores/               sqlite.py (sessions, runs, events, usage, schedules, approvals), blobs.py
│   ├── transport/telegram/   aiogram: topics.py, status.py, files.py, commands.py, approvals.py
│   ├── api/                  FastAPI: initdata_auth.py, routes/{sessions,events,usage,diffs,schedules,settings}
│   ├── scheduler/            cron/one-shot задачи, постоянные workspace, summary handoff
│   ├── selfdev/              git.py (ветки/коммиты/PR через gh), approvals.py, rebuild_client.py
│   └── usage/                учёт расходов, монитор баланса
├── launcher/                 супервизор (PID 1). Копируется в образ, не редактируется агентом
├── miniapp/                  Vite + React 19 SPA
├── skills/                   SKILL.md-каталог (загружается ISkillStore)
├── GOVERNANCE.md             правила, которые агент читает всегда и не может править тулами
├── deploy/
│   ├── Dockerfile
│   ├── compose.yaml          daedalus + telegram-bot-api (local Bot API server)
│   └── config.example.toml
├── tests/                    unit + smoke (smoke гоняет супервизор перед рестартом)
└── docs/
```

Состояние на volume `/srv/state`: `daedalus.sqlite`, `blobs/`, `snapshots/`, `secrets/`,
`config.toml`, `good/` (журнал рабочих ревизий). Workspaces — `/srv/workspaces/<session_id>/`.

**Критерий:** `uv run python -m daedalus --check` поднимает конфиг, открывает SQLite, регистрирует
тулы и провайдеры, не трогая Telegram. `uv run pytest` зелёный.

## 1. Хост-слой: провайдеры

`providers/openai_compat.py` — базовый `OpenAICompatibleProvider(ILLMProvider)`:

- `stream_with_tools`: SSE `chat/completions` → `ProviderDelta` (text, thinking/`reasoning_content`,
  tool_use_start/input/stop с парными событиями, finish, итоговая cumulative usage);
- `complete_structured`: `response_format=json_object` + fallback-парсер, снимающий ```json-ограду;
- `complete_text`, `count_tokens` (tiktoken-подобная оценка, точность не критична — ядро
  сверяет с usage провайдера);
- ошибки → `LLMRateLimitError` (429), `LLMTimeoutError`, `LLMContextWindowExceeded` (по коду/тексту
  ошибки), `LLMProviderError`, `LLMStreamIdleError` (нет байт дольше `rc.llm_stream_idle_seconds`);
- `LLMRequest.extra["reasoning_effort"]` и `thinking_enabled` → параметры провайдера
  (для DeepSeek: `thinking={"type":"enabled"|"disabled"}` + `reasoning_effort`).

Конфигурации: `deepseek.py` (base_url, модель по умолчанию `DeepSeek-V4-Flash-0731`, thinking on,
effort medium, прайс-лист для стоимости, `GET /user/balance`), `openrouter.py`
(заголовки атрибуции, usage `include`), `vllm.py` (без ключа, tool-parser особенности).
`chain.py` — `IProviderChain` по списку из конфига (`[providers.chain] = ["deepseek", "openrouter"]`).

Usage: каждый ответ провайдера сохраняется **целиком** в `usage_events` (raw json + нормализованные
поля `LLMResponseUsage` + стоимость), привязка к run/session/schedule.

**Критерий:** живой прогон против DeepSeek: стриминг текста, thinking-дельты, один tool call,
usage с cache-hit полями в SQLite; тест с записанным SSE-фикстуром для каждого провайдера.

## 2. Хост-слой: тулы, скиллы, хранилища

Тулы (`tools/*.py`, `@tool` из ядра, автоскан модулей `tools/` при старте — «добавить тул» =
«создать файл»): `exec` (bash в workspace, потоковый вывод в статус, таймаут из rc, без
ограничений содержимого), `read`, `write`, `edit` (точная замена), `find`, `search` (ripgrep),
`web_fetch`, `web_search`, `send_file` (документ/фото в топик текущей сессии), `ask_user`
(из ядра), `remember`/`recall`/`forget` (ядро, `IMemory` над SQLite), `spawn_task` (новая
сессия + топик, `IAgentDispatch`), `schedule`, `self.propose` (открыть PR), `self.rebuild`,
`self.rollback`, `skill` (`SkillStore`).

Path policy ядра закрывает для записи: `GOVERNANCE.md`, `/opt/launcher`, `/srv/state/secrets`.

Хранилища `stores/sqlite.py`: `ISessionStore`, `IRunStore`, `IEventStream` (таблица events +
asyncio-подписчики в процессе), `IBlobStore` (файлы на диске, sha256-имена), `IMemory`,
`IWorkspace`. Схема — миграции простыми пронумерованными SQL-файлами.

**Критерий:** сессия из CLI (`python -m daedalus run --prompt "..."`, без Telegram) выполняет
задачу с exec/read/write, события и usage лежат в SQLite, `AskUser` останавливает цикл и
возобновляется ответом из stdin.

## 3. Сессии, follow-up, снапшоты

`host/session_runner.py`: `Session` = запись в SQLite + workspace + один `QueryEngine` на ран.
Follow-up и steer — через duck-typed `engine.reload_live_control` / `persist_live_control`
(ядро читает `_follow_up_queue` / `_steer_queue`, `QueuedPrompt`), очередь в таблице
`live_control`. Смена модели/thinking на лету — `engine.apply_live_controls`.

Снапшоты: после каждого шага `engine.snapshot()` → `/srv/state/snapshots/<run_id>.json`;
при старте процесса все раны в незавершённом состоянии восстанавливаются
`resume_from_snapshot` и продолжают работу.

**Критерий:** kill -9 процесса посреди тула, старт заново — ран продолжается с того же места;
follow-up, отправленный во время работы, попадает в контекст на следующем шаге.

## 4. Супервизор и контейнер

`launcher/` — маленький Python-процесс без зависимостей (PID 1 в контейнере):

- запускает `python -m daedalus`, перезапускает при падении с backoff;
- unix socket `/run/daedalus.sock` с командами `rebuild`, `rollback <n>`, `panic`, `status`;
- `rebuild`: сообщает боту «сохрани снапшоты» → `git fetch && git checkout origin/main` в
  `/srv/daedalus` и `/srv/protocore-exp` → если изменились `Dockerfile`/`pyproject`/`uv.lock` —
  `docker build` + пересоздание контейнера через docker API хоста (socket смонтирован
  **только** в супервизор-контейнер, не в контейнер агента) — иначе `uv sync` + preflight
  (`compileall`, `import daedalus`, парсинг конфига, `pytest tests/smoke`) → exec;
- провал preflight → `git checkout good/<last>` → рестарт → запись причины в `/srv/state/good/FAILED`
  → бот при старте публикует её в General;
- лимиты расходов (USD/сутки, токенов/задача) читаются из `config.toml`; при превышении
  супервизор посылает боту `stop` для всех сессий и сообщает в General; агент не может их
  изменить (файл в `secrets/`-подобной зоне, закрытой для тулов);
- `panic` — SIGKILL всему дереву процессов, синхронно.

`deploy/Dockerfile`: ubuntu:24.04, uv, python3.12, git, gh, ripgrep, node (сборка Mini App),
docker-cli. `deploy/compose.yaml`: сервисы `launcher` (образ бота, тома `/srv/*`, docker.sock),
`telegram-bot-api` (`aiogram/telegram-bot-api`, `--local`, том с файлами общий с ботом —
файлы >20 МБ читаются напрямую с диска).

**Критерий:** `docker compose up`; `/restart` из General проходит цикл preflight → exec;
намеренно сломанный коммит откатывается автоматически и причина приходит в General.

## 5. Telegram-транспорт

aiogram 3, long polling через local Bot API server. Модули:

- `topics.py`: `/new <name>` → `createForumTopic` → сессия + workspace; привязка топика↔сессии
  fail-closed (сообщение в непривязанный топик — подсказка, не запуск); закрытие топика по
  завершении/`/close`; General = операторский канал;
- `commands.py`: `/new`, `/status`, `/model <name>`, `/thinking on|off|effort`, `/restart`,
  `/rollback [n]`, `/panic`, `/schedule …`, `/usage`, `/settings` — все до цикла агента;
- `status.py`: одно статус-сообщение на ран, редактирование не чаще 1 р/с (модель, тул,
  токены, стоимость, изменённые файлы), итоговый ответ — разбиение по границам кода/строк,
  >4096 → документ; `reply_parameters` для тредирования;
- `files.py`: любое сообщение с файлами/фото/caption/альбом → `inbox/` workspace сессии,
  агенту — текст + пути; окно склейки входящих 1.5 с; картинки → image-блоки, если
  провайдер их принимает; исходящие через `send_file`;
- `approvals.py`: `AskUser` → inline-кнопки (multiSelect, custom), ответ возобновляет ран;
  self-change → карточка с описанием + PR-ссылка + кнопки Одобрить / Отказать /
  Отказать с причиной (ForceReply на текст причины) → merge через gh / follow-up агенту;
- whitelist единственного `owner_user_id`; всё остальное игнорируется молча.

**Критерий:** живой сценарий: создать топик, дать задачу с файлом, отправить follow-up во
время работы, получить файл обратно, ответить на AskUser кнопкой, одобрить self-change.

## 6. Self-develop через PR

`selfdev/git.py`: агент работает в ветке `agent/<slug>` в `/srv/daedalus` или `/srv/protocore-exp`;
`self.propose(title, body)` → push ветки, `gh pr create` → карточка одобрения в General.
Режим `self_change.approval = manual | auto` (auto: merge сразу после зелёных проверок PR).
Одобрение → `gh pr merge --squash` → `self.rebuild` → супервизор (§4). Ядро обновляется
merge'ем `upstream/main` в ветку тем же путём. `GITHUB_TOKEN` — из `/srv/state/secrets`.
Branch protection на `main` обоих репо — единственный путь в main лежит через PR.

Системная секция промпта `self-develop rules`: где лежит какой репо, что тесты обязательны,
что `GOVERNANCE.md` и супервизор неприкосновенны, что после merge нужно вызвать `self.rebuild`.

**Критерий:** задача из бота «добавь тул `uptime`» → PR → кнопка → merge → rebuild → тул виден
в следующей сессии. Второй сценарий — «обнови ядро из апстрима».

## 7. Планировщик

Таблица `schedules(id, name, cron | run_at, prompt, files, model, recurring, workspace,
last_summary, topic_mode)`. `scheduler/` — asyncio-цикл на `croniter`, при срабатывании —
новая сессия с workspace `/srv/workspaces/sched-<id>/` (постоянный для recurring), в
контекст — `last_summary`; по завершении агент пишет `SUMMARY.md`, он же становится
`last_summary`. Топик `[cron] <name>` — один на задачу или на срабатывание (настройка).
Создание: тул `schedule` (копирует прикреплённые файлы из текущего workspace),
команда `/schedule`, Mini App. Пропущенные срабатывания во время рестарта — догоняются один раз.

**Критерий:** повторяющаяся задача дважды подряд видит свой workspace и summary предыдущего
запуска; одноразовая удаляется после выполнения.

## 8. Расходы и монитор баланса

`usage/`: агрегаты по сессии/дню/задаче из `usage_events`; `/usage` в Telegram; вкладка в
Mini App с разбивкой по всем полям провайдера. `balance_monitor`: раз в минуту
`GET /user/balance`; пороги `[thresholds]` в конфиге (список USD); пересечение вниз →
сообщение в General, повторно — только после подъёма выше порога.

**Критерий:** пороги настраиваются из Mini App и `/settings`; уведомление приходит один раз на
пересечение.

## 9. Mini App

`api/`: валидация `initData` (HMAC-SHA256, ключ `HMAC(bot_token, "WebAppData")`, `auth_date`
не старше N минут, `user.id == owner_user_id`); эндпоинты: сессии (список/состояние/создать/
остановить), события сессии (пагинация + SSE пока открыто), usage, диффы PR (через gh API,
кнопки одобрения дублируются), расписания, настройки (модель, thinking, пороги, режим
одобрения).

`miniapp/`: Vite + React 19, `telegram-web-app.js`, стиль Grok Bot: список сессий как
«ботов» с аватаром и анимированным статусом (idle/thinking/working/waiting/blocked/done),
единая лента (сообщения, события, tool-карточки, файлы), диффы, логи, usage, планировщик,
настройки; тёмная тема, цвета из `themeParams` Telegram. Любое открытие — восстановление с
сервера; ничего не зависит от долгоживущего соединения. Кнопка меню бота → Mini App.

**Критерий:** с телефона: открыть, увидеть все сессии и их статус, прочитать транскрипт,
одобрить PR, поменять модель, создать расписание.

## 10. Governance, скиллы, документация

`GOVERNANCE.md` (EN) — неизменяемые правила; `skills/` стартовый набор: `self-develop`,
`telegram-output` (как форматировать под 4096/код/файлы), `scheduling`. `README.md` для
внешнего пользователя: как поднять на своём сервере со своими моделями за 10 минут
(`.env` + `config.toml` + `docker compose up`). Перед открытием репо — аудит истории и
файлов на секреты и посторонние упоминания.

**Критерий:** чистая установка по README на пустом сервере проходит до первого ответа бота.

## Тестирование

- unit: провайдеры (SSE-фикстуры), тулы, stores, initData, разбиение сообщений, склейка входящих;
- smoke (`tests/smoke`, ≤30 с): импорт, конфиг, миграции, регистрация тулов — это гейт супервизора;
- live-сценарии из критериев §1, §3–§9 выполняются вручную через Telegram и фиксируются в
  `docs/ACCEPTANCE.md` с датой.
