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
          не подтверждены; подтверждён второй тип: грид-страницы имеют `page_type='grid'`
          (живой образец 2026-07-27) — это НЕ легаси, ворнинг для них отдельный (грид-тулзы)
  - [x] Справочник как MCP resource `wiki-mcp://yfm-cheatsheet` (по фактам смока, не по докам):
        `> [!NOTE]` → `{% note %}`, `<details>` → `{% cut %}`, табы, `#|` — только для
        многострочных ячеек (pipe-таблицы работают); шпаргалка проходит собственный валидатор (тест)
  - [x] Wording в description write-тулзов БЕЗ принуждения (`YFM_CONTENT_NOTE`) + строка
        в instructions сервера — клиенты часто не показывают ресурсы агенту сами
  - [x] Inline-валидатор warnings-only (модуль `yfm.py`, fence-aware, без зависимостей):
        уровень-1 «сломано» (незакрытые `{% %}`-блоки/`#|`-таблицы/код-фенсы),
        уровень-2 «отрендерится не так» — по фактам смока только GFM-алерты и сырой HTML
        (pipe-таблицы и task-листы не флагаются). Однобуквенные теги (`<a>`, `<b>`, …)
        омонимичны дженерикам `Vec<a>` и сравнениям «a<b and b>c»: они флагаются только
        при явных признаках разметки — закрывающий тег или `attr=value`; inline-код вырезается.
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

Живая разведка 2026-08-02 (`scripts/token_probe.py`, `scripts/contract_sweep.py` — 29 проверок
по всем методам клиента):

- [x] Фикс `page_search` — модель была написана под контракт, которого нет: `modified_at`
      ISO-строка (не int) → любой непустой поиск падал валидацией; сниппет в `content` (не `body`);
      API читает `limit` из тела (не `page_size`) → всегда max 10 результатов; envelope =
      results + курсоры (всегда null), остальные поля-призраки выпилены
- [x] Компактные ответы: slim-модели + `exclude_none`-сериализация, не обнуление полей.
      Сделано: None-дроп через model_serializer(wrap) на базе, descendants → {id, slug},
      юзеры обрезаны до WikiUser (id/username/display_name). Дерево 22 страниц:
      3.4k симв по проводу вместо ~13k (~4x).
      Замер: 100 страниц дерева = 60.7k симв по проводу (33.9k text-дубль с indent=2 + 26.8k
      structured), slim-проекция = 8.5k. Дерево descendants живьём содержит ТОЛЬКО id+slug
      (`title` не существует, `fields`-параметр не работает) → slim-элемент {id, slug}.
      outputSchema статична на тулзу — тяжёлые поля опциональны.
      Breaking для схемы — фиксировать в CHANGELOG (прецедент — `default_sort` в 0.5.0)
- [x] Ужать `extra="allow"` → `ignore` на моделях фиксированной формы, НО сначала объявить
      живые extras (иначе молча потеряем данные): `WikiPage` ← access_lists/access_policy/owner
      (запрашиваемы через `fields`!), `PageComment` ← author/inline_text/is_deleted/reactions/
      resolve_status (объявленный `user` живьём не приходит), `WikiAttachment` ← is_downloadable,
      `RecoverPageResponse` ← pages_count/slug. `grid_update_cells` отвечает ключом `cells`,
      не `results` — данные мимо `GridMutationResponse`. Гриды и `WikiResource.item`
      остаются `allow` (extras там — сами данные).
      Сделано вместе с предыдущим пунктом: все живые extras объявлены, мёртвые
      PageComment.user/updated_at выпилены, DynamicWikiModel для гридов;
      contract_sweep возвращает allow в рантайме (model_rebuild) для дрифт-детекции,
      свип 29/29 без необъявленных extras
- [x] Дубль text-блока: спека рекомендует дублировать (SHOULD) — дефолт не трогаем;
      настройка `TOOL_RESULT_TEXT: pretty|compact|none` (дефолт pretty = поведение FastMCP).
      Сделано проще запланированного: `WikiFastMCP.call_tool` пост-обрабатывает пару
      (unstructured, structured) от convert_result — без Annotated[CallToolResult, Model],
      схема и валидация structuredContent не затронуты
- [x] Опционально: срезать pydantic-`title` из схем хуком `__get_pydantic_json_schema__`
      (−20% на схему; tools/list сейчас 63.8k симв на диалог, из них 33.5k outputSchema).
      Сделано: outputSchema 33.5k → 29.1k, оставшиеся 2 title — DictOutput-обёртки FastMCP
- [x] `fetch_all: bool` для курсорных тулзов: цикл в tool-слое по `next_cursor`, жёсткий потолок
      (~500 элементов), `truncated: bool` в ответе. Сделано для всех пяти курсорных тулзов
      (`_drain_cursor`: ≤50 запросов, защита от повторяющегося курсора).
      Курсорная ходьба проверена живьём (descendants: 15 элементов за 3 страницы;
      comments: 4 за 2). У поиска курсоры всегда null — `fetch_all` туда не тянем
- [x] Выпилить `page_type` из схемы `page_create` (breaking): смок 2026-07-27 доказал, что API
      игнорирует поле целиком (любое значение → `wysiwyg`, даже мусор без ошибки) —
      параметр в схеме только вводит агента в заблуждение; из `WikiClient.page_create` тоже
- [x] Защита от дрейфа API на постоянной основе: workflow `api-drift.yml` еженедельно гоняет
      `contract_sweep.py` на живой организации (opt-in через секрет `DRIFT_WIKI_TOKEN` +
      переменные `DRIFT_*`; без них скипается). Свип в рантайме возвращает моделям
      `extra="allow"` (model_rebuild), чтобы видеть новые необъявленные ключи API

### v1.0.0 — page_clone + стабилизация

- [x] ~~`page_move`~~ → `page_clone` (2026-08-08). План был «паритет с qstyle: `POST /v1/pages/{id}`
      с `{"slug": new_slug}`» — живые пробы доказали, что это тихий no-op: 200, поле `slug`
      молча игнорируется (документированное тело апдейта — title/content/redirect/
      access_policy/owner), эндпоинта `/move` нет вовсе (404) — move есть только в веб-UI,
      а «move» qstyle сломан точно так же. Свип поймал это четырьмя фейлами до релиза —
      ради этого он и писался. Вместо move — `page_clone` поверх реального
      `POST /pages/{id}/clone` (отложенная операция, клиент поллит до success): копия с
      новым id, дети/комментарии/история остаются у оригинала, занятый slug — отказ
      (`SLUG_OCCUPIED`). Подробности и пробы — docs/api-notes.md «Страницы»
- [x] Стабилизация перед 1.0: ревизия «что ещё ломать» (после 1.0 breaking = major bump);
      классификатор в pyproject `4 - Beta` → `5 - Production/Stable`; README-таблица сравнения
      (YFM «planned» → done, строка про page_clone)

### Отклонено (2026-07-27)

- TTL-кэш slug→page_id — экономит только ~100–300ms на write-вызов (токены — нет, резолв
  внутренний), а stale-запись после переезда страницы = write в чужую страницу (id стабилен,
  слаги переиспользуются). Цена ошибки несоразмерна выигрышу
- MCP prompts — поддержка в клиентах неровная, «найди и суммаризируй» не даёт ничего сверх
  умения агента chain'ить search→read. Вернуться, если появится содержание
  (например, промпт вокруг YFM-справочника после v0.7.0)

## M7 — Документированный API (v1.3.0, план согласован 2026-08-11, выпущено 2026-08-18)

Контекст: Яндекс выпустил полный справочник API (поиск задокументирован!) и хостед
MCP-сервер `mcp.wiki.yandex.net` (`wiki-mcp-server` 1.28.1 — 31 тулза, поиска нет).
Дока и провод расходятся; живая сверка «заявление доки → факт» — `scripts/docs_probe.py`,
итоги в `docs/api-notes.md`. Docs-часть (api-notes, README-сравнение с официальным
сервером) сделана 2026-08-11 без релиза; ниже — кодовая часть.

- [x] `page_search`: серверные фильтры + подсветка (M) — сделано 2026-08-11:
      `slug_prefix`→`filters.cluster`, `result_type`→`filters.type`, новые
      `created_between`/`modified_between`, `highlight`; клиентского сита больше нет
  - живы на проводе (2026-08-11): `filters.type` (`page`/`file`), `filters.cluster`
    (раздел), `filters.created_at`/`modified_at` (интервал `{from, to}`, обе границы
    обязательны — только `from` → 400 `SEARCH_BAD_REQUEST`), `highlight=true`
    (совпадения в `content` оборачиваются в `<em>`)
  - документированные, но мёртвые `cursor` (1–500) и `order_by` — НЕ выставлять:
    `next_cursor` всегда null, порядок не меняется (см. api-notes)
  - решено (проба 2026-08-11): `cluster` берёт глубокий префикс (`tech-doc/x` —
    все результаты под ним), несуществующий кластер → 200 и 0 результатов;
    `slug_prefix` → `filters.cluster` и `result_type` → `filters.type` целиком
    переезжают на сервер — фильтры применяются ДО limit, совпадения не теряются
  - решено (проба 2026-08-18): `filters.authors` жив — матчит владельца страницы,
    `uid`/`cloud_uid` фильтруют поодиночке, несколько авторов = ИЛИ, пустой список =
    нет фильтра → выставлен как аргумент `authors`; `show_obsolete` мёртв (устаревшие
    страницы возвращаются при любом значении) → НЕ выставлять, третий мёртвый
    параметр после `cursor`/`order_by`
- [x] `page_delete_comment`: `DELETE /pages/{id}/comments/{cid}` — 200 +
      обновлённый `comments_count` (S) — сделано 2026-08-17. 404 не мапится в
      PageNotFound: конверт API сам называет виновника («No Comment matches…»),
      а маппинг бы врал при живой странице и мёртвом комментарии
- [x] `page_download_attachment`: `GET /pages/{id}/attachments/{fid}/download` —
      200 + байты (M) — сделано 2026-08-17. Формат решён: UTF-8 → текст, иначе
      base64; кап 1 MiB — выше отказ с отсылкой на `download_url` из
      `page_get_attachments` (кап бережёт контекст модели, не трафик).
      Сюрприз провода: 404 здесь — GIF-заглушка, не JSON-конверт → клиентский
      `AttachmentNotFound`
- [x] `page_delete_attachment`: `DELETE /pages/{id}/attachments/{fid}` — 204 (S) —
      сделано 2026-08-17; ack строится клиентом (как у grid_delete)
- [x] Редирект в `page_update`: `redirect={"page": {"id": N}}` ставит,
      `{"page": null}` снимает; читается через `page_get(fields=["redirect"])` —
      живьём проверено (S) — сделано 2026-08-17: аргументы `redirect_to_page_id` /
      `clear_redirect` (взаимоисключающие); чтение отдаёт
      `{page_id, redirect_target: {id, slug, title, page_type}}`
- [x] `user_get_current`: `GET /users/me` — `username`, `home_cluster` (слаг личного
      раздела), uid (S). Найден при полной сверке справочника с нашей поверхностью
      (2026-08-11): единственный документированный эндпоинт, которого у нас не было
      вообще; для агента — прямой ответ на «создай в моём разделе» и база для
      `users/<login>/...`-слагов без угадывания — сделано 2026-08-17 (read-тулза,
      доступна и в read-only режиме)
- [x] `page_edit` — частичное редактирование заменой текста (M). Это НЕ эндпоинт
      API (в доке только полный update + append) — read-modify-write на тулзовом
      уровне поверх page_get + page_update, как у официального MCP (EditPageContent),
      но крепче: page_id|slug (у них slug-only), отказ при ненайденном/неоднозначном
      совпадении ДО записи с числом вхождений и номерами строк (кап 5; без
      окружений — конвенция str_replace_editor: окружения дублируют page_get и жгут
      токены на каждом промахе), схема режет no-op (old==new) и пустой список,
      ответ — компактный ack без content. Гонка last-write-wins (у страниц нет
      ревизий) неустранима — честно названа в описании тулзы (окно то же, что у
      любого page_update). Сделано 2026-08-18: живой E2E (две замены применены и
      прочитаны назад дословно, неоднозначность отбита с номерами строк), в свипе —
      проверка дословного round-trip контента GET→replace→PUT→GET (главный живой
      риск — серверная нормализация разметки; не наблюдается). Поверхность 31 → 32
- Не берём (решение 2026-08-11): управление доступами (`POST`/`DELETE
  /pages/{idx}/access` — живой, но админ-фича; полная проверка требует второго
  пользователя); тред-эндпоинт комментариев (200, но на живой тред с ответом вернул
  `[]` — семантика мутная); `descendants?actuality=` (живой, ценность низкая —
  вернуться по запросу); сессии загрузки (`/upload_sessions/*`, 6 эндпоинтов
  multipart-загрузки больших файлов — наш документированный `attach_file` покрывает
  MCP-сценарий); апдейт колонки и pin/цвет строк гридов (есть у официального MCP,
  но в документированном grids API таких эндпоинтов нет — все 12 документированных
  у нас уже есть; вернуться, если появятся в доке или найдём их на проводе)

### Кандидаты после 1.3.0 (решение 2026-08-18, при переименовании
`page_read_attachment`)

- [ ] `page_download_attachment` — скачивание вложения в локальный путь, обратная
      `page_upload_attachment` (M): `file_id` + `save_to`, стрим на диск без капа
      (контекст модели не участвует; в ответе путь и размер). Имя освобождено
      переименованием read-тулзы и ждёт именно эту семантику. Гейт симметрично
      upload: не регистрируется при `OAUTH_ENABLED=true` («локальный путь» на общем
      сервере — чужая ФС) и не read-only
- [ ] Картинки в `page_read_attachment` через `ImageContent` (M): для `image/*`
      отдавать родной MCP-блок (data + mimeType) вместо octet-stream-блоба — клиенты
      рендерят в чате, vision-модели видят изображение («глянь диаграмму на
      странице» становится рабочим сценарием). Mime — из `Content-Type` ответа
      download-эндпоинта (нужна проба провода + прокинуть заголовок из `_request`
      наружу), fallback — magic bytes PNG/JPEG/GIF/WebP. Кап для картинок отдельный
      (порядка 1 MiB: у vision тарификация по пикселям, не по base64-символам).
      PDF это не спасает — его путь download-to-disk или `download_url`

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
- 2026-08-04: PR #7 (v0.8.0) смержен в main; выпущен релиз v0.8.0. Milestone задумывался как
  диета ответов, а стал в первую очередь починкой контракта: недокументированный поиск молча
  сменил формат между 2026-07 и 2026-08, и `page_search` падал валидацией на любом непустом
  запросе. Отсюда постоянная защита от дрейфа — `token_probe.py` (замеры), `contract_sweep.py`
  (все методы клиента живьём; необъявленные ключи роняют прогон) и еженедельный `api-drift.yml`.
  Побочно найдено и починено: пустое сообщение об ошибке при таймауте (`str(TimeoutError())`
  пустая строка), невозможность выбрать cloud-организацию per-request на сервере с обычным
  `WIKI_ORG_ID`, старт вообще без организации (падал на первом же запросе). 344 теста зелёные.
  Следующий шаг — v1.0.0: `page_move` + стабилизация.
- 2026-08-09: v1.0.0 — стабильный релиз. PR #9: `page_move` оказался невозможен (API молча
  игнорирует `slug` в апдейте, `/move` нет — свип поймал no-op четырьмя фейлами до релиза),
  вместо него `page_clone` поверх реального `POST /pages/{id}/clone`; breaking-пакет перед
  обещанием стабильности: `grid_move_row`/`grid_move_column` (единственное число),
  `page_search.limit`, типизированный `grid_delete`, гейтинг `page_upload_attachment` под
  OAuth, `openWorldHint=false` везде. PR #10: OAuth state hijack (ключ хранения теперь
  серверный), цикл импортов `normalize_slug`, свип истёкших записей в in-memory store,
  `extra="ignore"` + typo-детект в настройках, общий `select_org`. Следом: синк
  manifest.json с дрифт-тестом, покрытие 100% (statements + branches) с гейтом в CI,
  542 теста. Классификатор `5 - Production/Stable`; дальше breaking = major bump.
- 2026-08-09: v1.0.1 — хотфикс в день релиза. 1.0.0 не устанавливался вообще: `mcp` был
  объявлен как `>=1.21` без верхней границы, а `mcp` 2.0.0 (2026-07-28) выпилил `FastMCP` —
  любой свежий резолв (`uvx`, `pip install`, сборка Glama) падал на `ImportError` до первого
  ответа сервера. Граница `<2` тут не перестраховка: сервер наследует `FastMCP` и лезет в его
  внутренности (кастомные роуты, low-level сервер, auth-провайдер), переезд на 2.x (`MCPServer`) —
  отдельная работа. Корень проблемы в CI: обе джобы ставили зависимости через `uv sync`, то есть
  из `uv.lock`, поэтому диапазоны из `pyproject.toml` не проверялись нигде и никогда. Новая джоба
  `install` собирает wheel, ставит его свежим резолвом и поднимает сервер — на сломанной
  комбинации она красная.
- 2026-08-09: v1.1.0 — переезд на MCP Python SDK v2 (PR #11), снявший временный кап `<2`.
  Для клиентов не меняется ничего: один v2-сервер отвечает по всем ревизиям протокола от
  `2024-11-05` до `2026-07-28`, те же 27 тулз и оба ресурсных URI. Механическая часть —
  `FastMCP` → `MCPServer`, snake_case поля протокола, строковые URI ресурсов, версия
  аргументом конструктора, in-memory `Client` вместо удалённого тестового хелпера. Две
  немеханические: `host` теперь доезжает до `run()`/`streamable_http_app()` (иначе SDK
  слушает loopback и включает защиту от DNS rebinding — все MCP-запросы за настоящим
  хостнеймом получали 421, при живом `/healthz`), и `wiki-mcp://configuration` не смог
  сохранить `Context` (статический URI с `Context` теперь падает на регистрации) — вместо
  превращения URI в шаблон middleware кладёт входящий запрос в contextvar. Взято новое из
  v2: cache hints на статических листингах, `description` сервера, DEBUG-лог входящих
  сообщений с таймингами. 571 тест, покрытие 100%.
- 2026-08-10: v1.2.0 — доработка по итогам прикладных тестов в реальном контуре (PR #12).
  `page_get_descendants(from_root=true)` обходит всю вики, а не поддерево одной страницы:
  возможность всегда была в API (`GET /pages/descendants?slug=` пустой → 200 и полный
  перечень организации), её держал только наш `resolve_page_locator`, отвергавший пустой
  слаг до всякого HTTP — поэтому `page_search` выглядел единственным способом хоть что-то
  найти. Живая сверка (2026-08-10) подтвердила, что пустой слаг — намеренный контракт, а не
  реакция на мусор: неразрешимый слаг даёт 404, обход секции сходится с корневым обходом
  элемент в элемент, и ни один результат поиска в корневом обходе не потерялся. Сделано
  флагом, а не «опусти оба локатора»: обход — тысячи страниц, забытый аргумент не должен в
  него превращаться. Заодно честно описано, что такое `content` у результата поиска
  (~510 символов, вырезка с места совпадения, без подсветки, иногда вообще без слов
  запроса) — в описании тулзы, в output-схеме и в инструкциях сервера. 582 теста,
  покрытие 100%.
- 2026-08-11: обнаружены полный справочник API (поиск задокументирован) и официальный
  хостед MCP-сервер. Написан и прогнан `scripts/docs_probe.py` на полном и read-only
  токенах: живы — фильтры и подсветка поиска, DELETE комментариев и вложений, download
  вложений, redirect через апдейт, `actuality` у descendants; мертвы — `cursor`/`order_by`
  поиска; скоупы по-прежнему не проверяются (wiki:read-токен пишет, HTTP 200).
  Официальный MCP (`wiki-mcp-server` 1.28.1): 31 тулза, без поиска, без загрузки
  вложений, без read-only, без output-схем/аннотаций/ресурсов. Обновлены api-notes
  (EN/RU) и README-сравнение (официальный сервер в таблице, best-doctor в «Ещё стоит
  знать»). Кодовая часть — план M7 (v1.3.0).
- 2026-08-11: полная сверка справочника с нашей поверхностью (все 11 секций):
  новый пробел — только `GET /users/me` (добавлен в M7); гриды покрыты 12/12
  (апдейт-колонки и pin/цвет строк официального MCP в доке отсутствуют);
  text-replacement редактирование — не эндпоинт, а фича их сервера (записан
  кандидатом `page_edit`); `/pages/{id}/descendants` больше не «undocumented variant».
  Старт работ в ветке feat/m7-documented-api.
- 2026-08-18: `page_search.authors` экспонирован (жив на проводе, матчит владельца;
  пустая идентичность режется схемой — провод её молча игнорирует), `show_obsolete`
  пробой признан мёртвым (третий после cursor/order_by). Свип расширен пятью новыми
  контрактами M7 + round-trip вложения байт-в-байт — весь зелёный живьём. `page_edit`
  реализован (см. пункт выше): поверхность 31 → 32 тулзы.
- 2026-08-18 (ревью M7): пятнадцать находок исправлены до мержа. Крупное:
  кап скачивания переехал в `_request` (`max_bytes`, проверка по `Content-Length`
  до чтения тела; кап 128 KiB вместо 1 MiB; ответ — EmbeddedResource, чтобы не
  слать payload дважды), `page_edit` пишет с `allow_merge=True` и потерял
  idempotent-хинт (вставочная замена матчит свой выход), `filters.cluster`
  матчится дословно — нормализация ушла в клиент (проба: сегментные границы
  держатся, страница кластера включается), `authors: []` и пустые id — отказ
  вместо тихого поиска по всей Вики, delete-ack'и несут «улики», NUL-детектор
  отделяет текст от UTF-16-моджибейка. Поверх ревью: `read(n)` aiohttp —
  частичное чтение, capped-read стал циклом (иначе тихое обрезание тела по
  чанкам); error-body на capped-запросах — свой потолок 64 KiB с усечением
  вместо отказа. 646 тестов, свип зелёный живьём.
- 2026-08-18: `page_download_attachment` → `page_read_attachment` до релиза
  (потом было бы breaking): имя называло транспорт, а тулза читает содержимое
  в диалог, ничего не сохраняя. Клиентский метод оставлен `page_download_attachment`
  — он честно качает байты с download-эндпоинта. Старое имя прибережено для
  настоящего download-to-disk (кандидат выше, вместе с ImageContent для картинок).
- 2026-08-17: M7 (кодовая часть, без `page_edit`) завершён в ветке feat/m7-documented-api:
  пять новых тулз/аргументов — `page_delete_comment`, `page_download_attachment`
  (UTF-8 → текст, иначе base64, кап 1 MiB с отсылкой на `download_url`),
  `page_delete_attachment`, `user_get_current` (read, доступна в read-only),
  редирект в `page_update` (`redirect_to_page_id`/`clear_redirect`). Поверхность
  27 → 31 тулза. Сюрприз провода: 404 на download — GIF-заглушка вместо JSON-конверта
  (замаплено в новый `AttachmentNotFound`); 404 delete-эндпоинтов оставлены как есть —
  конверт сам называет виновника, маппинг в PageNotFound врал бы. Живой E2E на реальном
  контуре: комментарий добавлен/удалён, вложение загружено/скачано/удалено,
  редирект поставлен/прочитан/снят, скретч-страница удалена. 610 тестов.
