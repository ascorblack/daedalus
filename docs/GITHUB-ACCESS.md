# Доступ агента к GitHub

Репозитории (private, владелец `ascorblack`):

- https://github.com/ascorblack/daedalus — бот (этот репозиторий).
- https://github.com/ascorblack/protocore-exp — копия `ascorblack-labs/protocore-community`
  (`origin` → protocore-exp, `upstream` → community, чтобы тянуть обновления ядра).

## Fine-grained PAT: создаётся только руками

GitHub не даёт API для выпуска personal access token, поэтому `gh` его создать не может.
Нужен **один** fine-grained токен, ограниченный двумя репозиториями:

1. https://github.com/settings/personal-access-tokens/new
2. Token name: `daedalus-agent`. Expiration: по вкусу (90 дней или custom; при истечении
   агент перестанет пушить — бот уведомит в General).
3. Resource owner: `ascorblack`.
4. Repository access: **Only select repositories** → `daedalus`, `protocore-exp`.
5. Repository permissions:
   - Contents: **Read and write** (push веток)
   - Pull requests: **Read and write** (открывать PR, читать ревью)
   - Metadata: Read (ставится автоматически)
   - Workflows: Read and write — только если агенту разрешено менять `.github/workflows`
   - Issues: Read and write — опционально (агент может вести себе задачи)
6. Generate → скопировать `github_pat_…`.

Куда положить: в файл на volume, например `/srv/state/secrets/github_token`
(`chmod 600`), и передавать в контейнер как `GITHUB_TOKEN`. Он **никогда** не попадает в
git и не пишется тулами агента (путь `secrets/` закрыт path-policy).

Проверка внутри контейнера:

```bash
gh auth status
gh repo view ascorblack/daedalus --json name      # ok
gh repo view ascorblack/tg-bot --json name         # должен быть 404 — токен не видит другие репо
```

Merge в `main` делает владелец (кнопка в Telegram → бот мержит через API тем же токеном, или
руками на GitHub). Прямой push в `main` закрыть branch protection'ом — тогда «Одобрить» в
Telegram остаётся единственным путём.
