# Похожие проекты (обзор 2026-09-05)

## Самоизменяющиеся агенты

**razzant/ouroboros** — https://github.com/razzant/ouroboros (~1.3k★, Python, MIT).
Прямой прецедент. Первое поколение жило в Colab и общалось только через Telegram
(ветка `legacy-google-colab`); текущее — desktop/headless + web UI. 161 день публичной
работы: 1085 self-modification коммитов, 94 % написаны агентом (arXiv:2608.08311).
Берём: супервизор **вне** изменяемого репо; коммит на каждое self-change; fingerprint
диффа до/после ревью; `/panic` до агента; лимиты расходов снаружи; «улучшить себя» —
такая же задача в общей очереди. Избегаем: инцидент «3:41 AM» (20 копий, $2000,
несанкционированный push на GitHub) — не давать агенту креды реестров и git-remote.

**jennyzzt/dgm** (Darwin Gödel Machine, ~2.3k★). Архив вариантов агента, отбор по
бенчмарку. Берём: теги/архив, чтобы откат был одной командой. Избегаем: бенчмарк-цикл
слишком медленный для интерактивного бота.

**MaximeRobeyns/self_improving_coding_agent** (SICA). Мета-агент = целевой агент.
Берём: маленькая, обозримая кодовая база — self-modification работает, только если
агент способен удержать её в контексте.

## Telegram-фронты для агентов

**RichardAtCT/claude-code-telegram** (~2.8k★, Python) — сессии по (user, project dir),
SQLite, уровни подробности 0–2, whitelist, token-bucket под лимиты Telegram.

**alexei-led/ccgram** (~260★, Python) — **один forum-topic = одна tmux-сессия агента**;
топик создаётся → выбор каталога и агента; кнопки для ответов на промпты; fail-closed
привязки. Это наш ответ на «много сессий в одном чате».

**grinev/opencode-telegram-bot** (~1.1k★, TS) — закреплённое статус-сообщение,
редактируемое в реальном времени (модель, контекст, изменённые файлы); окно склейки
входящих сообщений (1.5 с) — Telegram режет длинный ввод на куски.

**cloveric/cc-telegram-bridge** (~170★, TS) — изолированные workspace на инстанс,
бюджет на бота, структурный timeline-лог.

**sipeed/picoclaw** (~26k★, Go) / OpenClaw — мультиканальный слой; открытые issue про
топики (#1270) и «message too long» (#244) — обе проблемы решаем в день 1.

## Mini App для агентов

**iosub/HERMES-hermes-telegram-miniapp**, **clawvader-tech/hermes-telegram-miniapp**
(React SPA, HMAC + Ed25519 auth, спавн агентов в tmux), **agentscope-ai/QwenPaw**.
Требования: HTTPS, `telegram-web-app.js`, серверная валидация `initData`
(HMAC-SHA256, ключ `HMAC(bot_token, "WebAppData")`, проверка `auth_date`).
Ограничения: **нет push**, живёт только открытым, долгие соединения умирают при
сворачивании, скачивание файлов — по явному тапу.

## Ограничения Bot API (проверено по core.telegram.org)

| Что | Лимит |
|---|---|
| Текст сообщения | 4096 символов |
| Отправка файла ботом | 50 МБ |
| Скачивание через `getFile` | 20 МБ (локальный Bot API server: 2000 МБ / без лимита) |
| Частота в одном чате | ~1 сообщение/с; 20/мин в группу; `editMessageText` считается |
| Топики | `createForumTopic` и др., боту нужно `can_manage_topics`, `message_thread_id` |
