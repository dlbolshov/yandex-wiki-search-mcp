# План доработок

Рабочий план развития проекта после релиза 0.3.0. Форк стал самостоятельным проектом:
совместимость диффа с апстримом больше не ограничение.

Как пользоваться:

- Этапы идут в рекомендованном порядке, но M5/M6 частично параллелятся с остальными.
- Внутри этапа пункты независимы, если не отмечено иное.
- После каждого этапа: `task format`, `task check`, `task test`, запись в `CHANGELOG.md` (Unreleased).
- При изменении схем/поведения тулзов обновлять `README.md`, `README_ru.md`, `manifest.json` (см. CLAUDE.md).

Оценка: S — до часа, M — до полудня, L — больше.

Ветки и релизы (решение 2026-07-19):

- M1 + M2 — одна ветка/PR → релиз v0.4.0
- M3 + M4 — одна ветка/PR → релиз v0.5.0
- M5 — отдельная ветка/PR, без релиза (едет со следующим)
- M6 — ветка/PR + тег на каждую фичу (v0.6.0, v0.7.0, …)
- Релизный ритуал: CHANGELOG `[Unreleased]` → `[X.Y.Z]`, bump в `pyproject.toml`, `manifest.json`,
  `server.json` (3 места) + `uv lock`, commit `Release vX.Y.Z`, тег `vX.Y.Z` → CI публикует
  PyPI/ghcr/MCPB/GitHub Release/MCP Registry автоматически.

## M1 — Гигиена кода (S)

- [x] Мёртвый код (решения после повторного ревью):
  - [x] `WikiMCPError` (`mcp_wiki/mcp/errors.py`) — удалён вместе с файлом: FastMCP стрингифицирует
        любые исключения, отдельная иерархия ничего не добавляет к `ValueError`
  - [x] `set_non_needed_fields_null` (`mcp_wiki/mcp/utils.py`) — удалён: зачаток идеи «компактных
        ответов» (M6), но реализация лишь обнуляла поля — в JSON оставались `null`, экономии нет
  - [x] `UploadLocation` — переехал в `wiki/proto/types/pages.py`, применён к
        `page_append_content.location` и `page_upload_attachment.append_location`
        в клиенте, `WikiProtocol` и MCP-тулзах
  - [x] `CommentID` — применён к `page_add_comment.parent_id`/`thread_id` (валидация `gt=0`)
- [x] `__main__.py`: сайд-эффекты импорта убраны — сервер и Settings создаются внутри `main()`
- [x] Добавлен `mcp_wiki/py.typed` + `[tool.setuptools.package-data]`
- [x] Добавить `.env.example` со всеми env-переменными и комментариями
- [x] `WikiPage.content: Any | None` → `Any` (`mcp_wiki/wiki/proto/types/pages.py`)
- [x] `WikiClient._build_headers` синхронный (обновлены все вызовы и тест)

## M2 — Секреты и логирование (S-M)

- [x] `SecretStr` в `Settings`: `wiki_token`, `wiki_iam_token`, `oauth_client_secret`, `redis_password`
      + `oauth_encryption_keys` (сверх плана — тоже секрет); `.get_secret_value()` только в точках
      использования (lifespan `WikiClient`, OAuth-провайдер, Redis-стор, парсинг ключей)
- [x] `YandexAuth.token` — `field(repr=False)` (`mcp_wiki/wiki/proto/common.py`)
- [x] Настройка `LOG_LEVEL` + `logging.basicConfig` в stderr при старте; проброшена и в FastMCP
- [x] Debug-лог HTTP-запросов в `WikiClient` через aiohttp `TraceConfig` (не трогая call-sites):
      метод, путь, статус, длительность — без заголовков и тел
- [x] Стартовый лог конфигурации в `main()`: транспорт, base_url, org, read_only, auth-режим — без секретов

## M3 — Рефактор WikiClient (M-L)

Фундамент для M4 (HTTP-логирование уже сделано в M2 через TraceConfig — рефактор его не затрагивает).

- [ ] Единый `_request()`-хелпер: заголовки, обработка статусов, парсинг обоих error-envelope
      (`message` строка+`details` / список+`level`), `WikiApiError` для всех эндпоинтов
      (сейчас — только `page_search` и частично `page_append_content`, остальные кидают сырой
      `aiohttp.ClientResponseError`)
- [ ] `PageNotFound` последовательно: 404 у grid-эндпоинтов сейчас не обрабатывается
- [ ] Создание `ClientSession` перенести из `__init__` в `prepare()`; поддержать `async with WikiClient(...)`
- [ ] Отдельный (увеличенный) таймаут для upload-методов — общий `total=30s` ломает большие файлы
- [ ] Неблокирующее чтение файла в `page_upload_attachment` (`asyncio.to_thread` для `read`/`stat`)
- [ ] Вынести anchor-fallback (`_append_content_to_anchor_source` + обработка `ANCHOR_NOT_FOUND`)
      из клиента в отдельный модуль с собственными тестами
- [ ] Обсудить: ретраи с backoff для идемпотентных GET (API не отдаёт Retry-After — только сеть/5xx)

## M4 — Слой тулзов: схемы и типы (M)

Лучше делать после M3.

- [ ] Типизированные возвраты тулзов (модели уже есть в `wiki/proto/types/pages.py`) →
      FastMCP сгенерирует `outputSchema` и structured content вместо `-> Any`
- [ ] Pydantic-модели аргументов гридов: `GridCellPatch`, `GridColumnSpec`, `GridSortEntry`
      вместо `list[dict[str, Any]]` + ~120 строк ручной валидации в `page_write.py`;
      LLM-клиент получит настоящие JSON-схемы аргументов
- [ ] Общая пара параметров `page_id`/`slug` в `params.py` (сейчас продублирована ~12 раз)
- [ ] Хелпер `get_wiki(ctx)` вместо `ctx.request_context.lifespan_context.wiki` в каждом теле
- [ ] `_resolve_page_id`/`_resolve_page_slug` → общий модуль (сейчас приватный импорт
      из `page_read.py` в `page_write.py`)
- [ ] `ToolAnnotations` для write-тулзов: `destructiveHint` (`page_delete`, `grid_delete`,
      `grid_delete_rows`, `grid_delete_columns`), `idempotentHint` где уместно
- [ ] Обновить README/README_ru/manifest.json после изменения схем

## M5 — Тесты и CI (M)

- [ ] Тесты OAuth-слоя (сейчас не покрыт совсем):
  - [ ] `YandexOAuthAuthorizationServerProvider`: authorize → callback → exchange → refresh
  - [ ] `InMemoryOAuthStore`: TTL, single-use состояний/кодов, revoke-цепочка
  - [ ] `RedisOAuthStore` (fakeredis или мок `aiocache`), `crypto`/`serializers` — roundtrip с ротацией ключей
- [ ] Реализовать `revoke_token` в OAuth-провайдере (сейчас `NotImplementedError` → 500 на revocation endpoint)
- [ ] Тесты валидаторов `Settings` (сейчас `model_construct` в conftest обходит валидацию)
- [ ] Coverage gate в CI: `--cov --cov-fail-under=N` (стартовать с фактического уровня, не задирать)
- [ ] Ruff: добавить наборы `UP`, `SIM`, `RUF`, `PTH`, `ASYNC`, `S` (bandit), `TRY`, `PERF`;
      решить судьбу `E501` (line-length vs осознанный ignore)
- [ ] mypy: ужесточить конфиг (сейчас `ignore_missing_imports` на всё) или оставить один из mypy/ty
- [ ] `dependabot.yml`: uv, github-actions, docker

## M6 — Функциональные идеи (обсудить каждую перед реализацией)

- [ ] Экономия токенов LLM: компактный режим ответов — усечение `body`-сниппетов в `page_search`,
      минимальный набор полей (id/slug/title) в `page_get_descendants` по умолчанию;
      реализовать через `model_dump(include=...)`/урезанные модели, а не обнуление полей
      (см. удалённый `set_non_needed_fields_null` из M1)
- [ ] TTL-кэш slug→page_id для резолва в write-тулзах (ключ обязан учитывать org и токен/пользователя;
      `aiocache` уже в зависимостях)
- [ ] `fetch_all: bool` автопагинация для курсорных тулзов с жёстким лимитом
- [ ] `/healthz` route для HTTP-деплоя (custom route, как OAuth callback)
- [ ] Версия сервера из `importlib.metadata` в `FastMCP` (клиент увидит её в initialize)
- [ ] `stateless_http`/`json_response` вынести из хардкода в настройки
- [ ] MCP prompts (например, «найди и суммаризируй по теме») — дифференциация от аналогов

## Лог выполнения

- 2026-07-19: план создан (после ревью кодовой базы v0.3.0).
- 2026-07-19: пересмотрены решения по мёртвому коду (`UploadLocation` и `CommentID` — применить,
  а не удалять); добавлен `.env.example`.
- 2026-07-19: M1 завершён — ruff/ty/mypy чисто, 111 тестов зелёные, CHANGELOG (Unreleased) обновлён.
- 2026-07-19: M2 завершён — SecretStr (+oauth_encryption_keys), repr-гигиена YandexAuth, LOG_LEVEL,
  TraceConfig-лог HTTP, стартовый лог; живой смок на реальном .env — токен замаскирован.
  Ветка chore/m1-m2-hygiene-secrets-logging.
