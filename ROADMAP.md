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
- M5 + ретраи из M3 — одна ветка/PR, без релиза (едет со следующим)
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

- [x] Единый `_request()`-хелпер: заголовки, обработка статусов, парсинг обоих error-envelope
      (`build_api_error` в `errors.py`), `WikiApiError` для всех эндпоинтов
- [x] 404 последовательно: `PageNotFound` для page-эндпоинтов (вкл. descendants),
      новый `GridNotFound` для grid-эндпоинтов
- [x] `ClientSession` создаётся в `prepare()`; поддержан `async with WikiClient(...)`
- [x] Отдельный таймаут для upload-запросов (`upload_timeout`, по умолчанию 300s)
- [x] Неблокирующее чтение файла в `page_upload_attachment` (`asyncio.to_thread`)
- [x] Anchor-fallback вынесен в `wiki/custom/anchors.py` + `tests/wiki/custom/test_anchors.py`
- [x] Ретраи с backoff (решение 2026-07-19, сделано в ветке M5): мини-ретрай в `_request()` без зависимостей —
      connection-ошибки (`ClientConnectionError`/`ClientPayloadError`) + 502/503/504/429, `retryable`
      по умолчанию = GET, явный `retryable=True` для `page_search` и `_upload_part`, 2 ретрая с equal
      jitter (≤0.9s суммарно), таймауты не ретраим, `Retry-After` уважаем с потолком 3s (больше — сразу
      ошибка, чтобы тулза не залипала); knob `WIKI_MAX_RETRIES` (default 2, 0 выключает)

## M4 — Слой тулзов: схемы и типы (M)

Лучше делать после M3.

- [x] Типизированные возвраты всех 26 тулзов → FastMCP генерирует `outputSchema` и structured content
- [x] Pydantic-модели аргументов гридов: `GridCellPatch`, `GridColumnSpec`, `GridSortEntry`
      в `params.py`; `default_sort` теперь `[{"column": ..., "direction": ...}]` (breaking для схемы тулза)
- [x] Общая пара `OptionalPageID`/`OptionalPageSlug` в `params.py`
- [x] Хелпер `get_wiki(ctx)` + `ToolContext` alias (`mcp/tools/common.py`)
- [x] `resolve_page_id`/`resolve_page_slug` → `mcp/tools/common.py`, приватный кросс-импорт убран
- [x] `ToolAnnotations` для всех write-тулзов: `destructiveHint` для удалений,
      `idempotentHint` для update/move, additive-хинты для create/append
- [x] Обновлены README/README_ru (`default_sort`); manifest.json не требует правок (имена тулзов не менялись)

## M5 — Тесты и CI (M)

- [x] Тесты OAuth-слоя (сейчас не покрыт совсем):
  - [x] `YandexOAuthAuthorizationServerProvider`: authorize → callback → exchange → refresh
  - [x] `InMemoryOAuthStore`: TTL, single-use состояний/кодов, revoke-цепочка
  - [x] `RedisOAuthStore` (выбран fakeredis: гоняет полный путь aiocache → serializer → redis-клиент),
        `crypto`/`serializers` — roundtrip с ротацией ключей
- [x] Реализовать `revoke_token` в OAuth-провайдере (сейчас `NotImplementedError` → 500 на revocation endpoint):
      диспатч по типу токена, в `OAuthStore` добавлен `revoke_access_token`; refresh отзывается каскадно
- [x] Тесты валидаторов `Settings` (сейчас `model_construct` в conftest обходит валидацию)
- [x] Coverage gate в CI: `--cov-branch --cov-fail-under=80` (факт после M5 — 83% branch / 88% line,
      было 73% line) + Codecov (PR-комменты, бейдж); `task test-cov` починен (без `--cov` он не собирал
      coverage вообще) и зеркалит CI-гейт + HTML-отчёт
- [x] Ruff: добавлены `UP`, `SIM`, `RUF`, `PTH`, `ASYNC`, `S` (bandit), `TRY`, `PERF`;
      `E501` оставлен в ignore (переносами правит ruff format, падать на URL глупо);
      ignore `TRY003` (осмысленные сообщения в raise), `RUF029`/`RUF067` (framework-контракты);
      прод-asserts переведены в явные raise, в тестах asserts узаконены per-file-ignores
- [x] mypy: ужесточён (`disallow_untyped_defs`, `python_version=3.11`, точечные overrides для
      aiocache/aioresponses вместо одеяла); ty оставлен вторым мнением — пересмотреть на 1.0
- [x] `dependabot.yml`: uv, github-actions, docker
- [x] (сверх плана) CI разделён: `lint`-job (ubuntu, py3.11) + матрица тестов 3 ОС × 3 Python;
      `concurrency` с cancel-in-progress
- [x] (сверх плана) Фикс утечки в `RedisOAuthStore`: mapping refresh→access писался без TTL —
      один вечный ключ в Redis на каждый логин; теперь TTL = 31 день (как у refresh) + регрессионный тест

## M6 — Функциональные идеи (план согласован 2026-07-27)

### v0.7.0 — YFM-справочник + мелочи для HTTP-деплоя

- [ ] YFM-хелперы (концепция дообсуждена 2026-07-27: «дефолт — маркдаун, ноль принуждения»;
      YFM — надмножество CommonMark, рендерер фиксирован платформой, ванильный md уже валиден):
  - [x] Живой смок в личном разделе (`scripts/yfm_smoke.py`, прогнан 2026-07-27). Факты:
        • `page_type` при создании ИГНОРИРУЕТСЯ — любое значение (вкл. мусор) → OK, всё становится `wysiwyg`
        • roundtrip контента побайтово идентичен — сервер ничего не нормализует
        • рендерятся: `{% note %}`, `{% cut %}`, табы (живой виджет), `#|`-таблицы,
          GFM pipe-таблицы (сюрприз: работают!), task-листы (с косметическим двойным маркером)
        • сломаны: `> [!NOTE]` (цитата с литеральным `[!NOTE]`), сырой HTML (`<details>` —
          экранируется в голый текст)
        • `{%` в код-фенсе не парсится (фенс-защита подтверждена); append (bottom) ок
        • вложенный slug НЕ создаёт родителя — в дереве фантомный узел без страницы
        • образца legacy-страницы не нашлось (всё `wysiwyg`) — значения поля для старых страниц
          не подтверждены
  - [x] Справочник как MCP resource `wiki-mcp://yfm-cheatsheet` (по фактам смока, не по докам):
        `> [!NOTE]` → `{% note %}`, `<details>` → `{% cut %}`, табы, `#|` — только для
        многострочных ячеек (pipe-таблицы работают); шпаргалка проходит собственный валидатор (тест)
  - [x] Wording в description write-тулзов БЕЗ принуждения (`YFM_CONTENT_NOTE`) + строка
        в instructions сервера — клиенты часто не показывают ресурсы агенту сами
  - [x] Inline-валидатор warnings-only (модуль `yfm.py`, fence-aware, без зависимостей):
        уровень-1 «сломано» (незакрытые `{% %}`-блоки/`#|`-таблицы/код-фенсы),
        уровень-2 «отрендерится не так» — по фактам смока только GFM-алерты и сырой HTML
        (pipe-таблицы и task-листы не флагаются). HTML-регексп требует атрибутный синтаксис —
        проза «a<b и b>c» и дженерики `List<int>` не флагаются; inline-код вырезается.
        `yfm_warnings` в ответах page_create/page_update (поле `PageWriteResponse`,
        схема-аддитивно) и page_append_content (ключ в dict). Запись не блокируется.
        Кап MAX_WARNINGS=10 + строка «N suppressed» (токен-экономия)
  - [x] page_type-защита (защитный вариант: образца legacy нет, значения не подтверждены):
        ворнинг только если `page_type` пришёл и ≠ `wysiwyg`, и только на slug-пути
        (`resolve_page_id_and_type` в common.py); лишнего GET на id-пути нет
  - Отдельная тулза `yfm_validate` — НЕ в v0.7.0 (риск путаницы агента, inline покрывает флоу;
    вернуться после живых тестов). Конвертер Markdown→YFM — НЕ делать (молчаливая трансформация
    чужого контента, edge cases, идемпотентность). Готовые линтеры (@diplodoc/yfmlint,
    @diplodoc/transform) — Node-only, зависимость от Node убивает uvx/MCPB-установку.
    Закрывает «planned» из README-сравнения
- [x] `/healthz` route (custom route, как OAuth callback). Liveness-only: всегда 200, без похода
      в Wiki API — падение апстрима не должно ронять под в restart loop, а healthcheck — спамить API
- [x] Версия сервера из `importlib.metadata`: FastMCP версию не принимает и без неё репортил версию
      библиотеки `mcp` — ставим через `server._mcp_server.version` (прецедент приватного доступа —
      `_custom_starlette_routes` для OAuth callback); fallback `"dev"` при `PackageNotFoundError`
- [x] `stateless_http`/`json_response` → настройки (дефолты текущие: true/true) + `.env.example`

### v0.8.0 — Экономия токенов LLM

- [ ] Компактные ответы: slim-модели (усечение `body`-сниппетов в `page_search`, id/slug/title
      в `page_get_descendants`), не обнуление полей (см. удалённый `set_non_needed_fields_null` из M1).
      Важно: outputSchema статична на тулзу — схема одна, тяжёлые поля опциональны,
      `verbose=True` их заполняет. Проверить протечку `extra="allow"` в structured content
      (неизвестные поля API сейчас, вероятно, утекают в ответы и жгут токены).
      Breaking для схемы — фиксировать в CHANGELOG (прецедент — `default_sort` в 0.5.0)
- [ ] `fetch_all: bool` для курсорных тулзов: цикл в tool-слое по `next_cursor`, жёсткий потолок
      (~500 элементов), `truncated: bool` в ответе. Строго вместе с/после компактных моделей,
      иначе умножает токен-жир
- [ ] Выпилить `page_type` из схемы `page_create` (breaking): смок 2026-07-27 доказал, что API
      игнорирует поле целиком (любое значение → `wysiwyg`, даже мусор без ошибки) —
      параметр в схеме только вводит агента в заблуждение; из `WikiClient.page_create` тоже

### v1.0.0 — page_move + стабилизация

- [ ] `page_move` (паритет с qstyle/yandex-wiki-mcp): тот же эндпоинт, что `update_page` —
      `POST /v1/pages/{id}` с `{"slug": new_slug}` (проверено по их исходникам 2026-07-27).
      Принимать page_id ИЛИ текущий slug (наш паттерн OptionalPageID/OptionalPageSlug);
      idempotentHint. Проверить на живой вики: судьба поддерева при смене слага (иерархия
      слаговая — дети должны переехать?), коллизия слагов (ожидаем 4xx → `WikiApiError`),
      нормализация new_slug. В description — предупреждение: смена слага ломает внешние ссылки
- [ ] Стабилизация перед 1.0: ревизия «что ещё ломать» (после 1.0 breaking = major bump);
      классификатор в pyproject `4 - Beta` → `5 - Production/Stable`; README-таблица сравнения
      (YFM «planned» → done, строка про page_move)

### Отклонено (2026-07-27)

- TTL-кэш slug→page_id — экономит только ~100–300ms на write-вызов (токены — нет, резолв
  внутренний), а stale-запись после переезда страницы = write в чужую страницу (id стабилен,
  слаги переиспользуются). Цена ошибки несоразмерна выигрышу
- MCP prompts — поддержка в клиентах неровная, «найди и суммаризируй» не даёт ничего сверх
  умения агента chain'ить search→read. Вернуться, если появится содержание
  (например, промпт вокруг YFM-справочника после v0.7.0)

## Лог выполнения

- 2026-07-19: план создан (после ревью кодовой базы v0.3.0).
- 2026-07-19: пересмотрены решения по мёртвому коду (`UploadLocation` и `CommentID` — применить,
  а не удалять); добавлен `.env.example`.
- 2026-07-19: M1 завершён — ruff/ty/mypy чисто, 111 тестов зелёные, CHANGELOG (Unreleased) обновлён.
- 2026-07-19: M2 завершён — SecretStr (+oauth_encryption_keys), repr-гигиена YandexAuth, LOG_LEVEL,
  TraceConfig-лог HTTP, стартовый лог; живой смок на реальном .env — токен замаскирован.
  Ветка chore/m1-m2-hygiene-secrets-logging.
- 2026-07-19: PR #2 (M1+M2) смержен в main; выпущен релиз v0.4.0.
- 2026-07-19: M3+M4 завершены в ветке refactor/m3-m4-client-and-tool-schemas — клиент на едином
  `_request()`, 26/26 тулзов с outputSchema, грид-аргументы на pydantic-моделях; 118 тестов зелёные.
  Ретраи из M3 отложены (требуется обсуждение).
- 2026-07-19: PR #3 (M3+M4 + фиксы регрессий и pre-existing багов, 127 тестов) смержен в main;
  выпущен релиз v0.5.0.
- 2026-07-19: по ретраям принято решение (мини-ретрай в `_request()`, без зависимостей) — в ветку M5.
- 2026-07-26: ветка `chore/m5-tests-ci-and-retries`; ретраи из M3 реализованы (консервативная область:
  write-запросы не ретраятся, т.к. 5xx может прийти уже после применения записи; `Retry-After` с
  потолком 3s; `WIKI_MAX_RETRIES`). 13 новых тестов, всего 140 зелёных.
  Разбор расширения ретраев на мутации (2026-07-26) — решено НЕ делать:
  - `ClientPayloadError` означает, что ответ уже начал приходить → сервер отработал; ретрай мутации
    здесь гарантированно дублирует запись;
  - `ServerDisconnectedError` неразличимо схлопывает «коннект был мёртв» и «бэкенд применил запись
    и умер до ответа» — развести их на клиенте нечем;
  - остаются только `ClientConnectorError`/`ClientConnectionResetError`, где «запрос не ушёл»
    доказуемо, но они означают недоступность API, и повтор через 0.3s её не лечит → пользы ~0.
  Конкретные риски дубля, если бы делали: `page_append_content`/`page_add_comment` — тихий дубль
  контента; `page_delete` — потеря `recovery_token` из первого ответа (откатить удаление нечем);
  `page_create` — конфликт слага на успешно созданной странице (LLM пойдёт создавать вторую);
  `grid_*` защищены `revision`, `page_update` идемпотентен по контенту.

  Проверено по aiohttp 3.13.3: `TCPConnector.keepalive_timeout = 15s`, т.е. idle-коннекты клиент
  закрывает сам и «простой между вызовами тулзов» почти не создаёт обрывов — основную работу
  в ретраях делают 5xx/429, а не транспортные ошибки. Там же: `ServerTimeoutError` наследуется
  и от `ClientConnectionError`, и от `asyncio.TimeoutError` — в `_request()` стоит явная проверка,
  чтобы таймауты не начали ретраиться, если в `ClientTimeout` когда-нибудь добавят `sock_read`.
- 2026-07-26: M5 завершён — `revoke_token` + фикс TTL у mapping в Redis, 65 новых тестов
  (OAuth-слой целиком + валидаторы Settings), всего 206 зелёных, покрытие 73% → 88%.
  CI: lint/test разделены, coverage gate 80% (branch), Codecov, dependabot. Ruff/mypy ужесточены.
  Для Codecov нужен секрет `CODECOV_TOKEN` в настройках репы (codecov.io → логин через GitHub →
  токен репозитория → Settings → Secrets → Actions).
- 2026-07-27: PR #5 (M5 + ретраи, 11 чеков) смержен в main. Следом на main: фикс гонки сохранения
  uv-кэша между lint и test-джобой (`cache-suffix: lint` — у них совпадал ключ кэша);
  выпущен релиз v0.6.0 (minor, а не patch: в релизе новые фичи — ретраи, `WIKI_MAX_RETRIES`,
  `revoke_token`).
- 2026-07-27: M6 обсуждён и пересобран в план релизов (см. секцию M6): v0.7.0 YFM-справочник +
  healthz/версия/stateless-настройки, v0.8.0 компактные ответы + fetch_all, v1.0.0 page_move +
  стабилизация. Отклонены кэш slug→page_id и MCP prompts. По исходникам qstyle/yandex-wiki-mcp
  подтверждено: move = `POST /v1/pages/{id}` с полем `slug` — наш эндпоинт `update_page` его уже
  умеет, нужна отдельная тулза с проверкой поведения на живой вики.
