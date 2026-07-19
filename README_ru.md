# Yandex Wiki Search MCP

MCP сервер для API Yandex Wiki с **полнотекстовым поиском**: страницы, комментарии, ресурсы, вложения и восстановление удалённых страниц.
Это единственный MCP сервер для Yandex Wiki, сочетающий полнотекстовый поиск по контенту, серверный read-only режим и готовый Docker-образ.
Набор инструментов также включает полноценную поддержку динамических таблиц Yandex Wiki ("grids").

Форк [APonkratov/yandex-wiki-mcp](https://github.com/APonkratov/yandex-wiki-mcp) (`ya-yandex-wiki-mcp`), см. [Благодарности](#благодарности).

## Реализованные инструменты

- `page_search`: полнотекстовый поиск по всей Вики (главная фича)
- `page_get_grids`: список таблиц на странице
- `grid_get`: получение таблицы по `grid_id`
- `page_get`: получение страницы по `page_id` или `slug`
- `page_get_descendants`: получение поддерева страниц
- `page_get_comments`: комментарии страницы
- `page_get_resources`: ресурсы страницы, включая вложения и таблицы
- `page_get_attachments`: вложения страницы
- `grid_create`: создание таблицы на странице
- `grid_update`: обновление заголовка и/или сортировки таблицы
- `grid_delete`: удаление таблицы
- `grid_copy`: копирование таблицы на существующую страницу
- `grid_add_rows`: добавление строк в таблицу
- `grid_delete_rows`: удаление строк из таблицы
- `grid_update_cells`: обновление отдельных ячеек таблицы
- `grid_add_columns`: добавление колонок в таблицу
- `grid_delete_columns`: удаление колонок из таблицы
- `grid_move_rows`: перемещение строки внутри таблицы
- `grid_move_columns`: перемещение колонки внутри таблицы
- `page_create`: создание страницы
- `page_update`: обновление заголовка и/или полного содержимого страницы
- `page_append_content`: добавление контента в начало, конец или к якорю
- `page_add_comment`: добавление комментария или ответа
- `page_delete`: удаление страницы с получением recovery token
- `page_recover`: восстановление страницы по recovery token
- `page_upload_attachment`: загрузка локального файла по частям и прикрепление к странице

## Полнотекстовый поиск

`page_search` оборачивает недокументированный, но публичный endpoint `POST /v1/search` —
тот же бэкенд, что и строка поиска в веб-интерфейсе Вики. Это точка входа для
**обнаружения** контента: сначала поиск, затем открытие результата через `page_get` по его `slug`.

- Возвращает до **50** результатов за вызов (`page_size` ограничивается диапазоном 1–50 на стороне клиента; иначе API отвечает HTTP 400).
- Поиск **только глобальный** — серверной фильтрации по разделу или типу нет. Опциональные аргументы `slug_prefix` и `result_type` применяются **на стороне клиента после получения результатов**, поэтому используйте их вместе с `page_size=50`, чтобы не терять совпадения. `slug_prefix` сопоставляется по границам сегментов пути (`tech-doc/ml` не совпадает с `tech-doc/mlops`).
- Результаты бывают двух типов: **`page`** (относительный url, нормализуется инструментом в абсолютную ссылку `https://wiki.yandex.ru/...`) и **`file`** (абсолютная ссылка на скачивание `...?download=1`).
- Поиск точной фразы в кавычках `"..."` работает и возвращает фразовые совпадения.
- `total_documents` всегда равен числу возвращённых результатов — это **не** глобальное количество совпадений.

## Заметки об API Yandex Wiki

Наблюдения проверены вживую на боевой организации Yandex 360 (см. скрипты в [`scripts/`](scripts/)):

- `POST /v1/search` не документирован; максимум `page_size` — 50 (0, отрицательные значения или >50 → HTTP 400); серверной пагинации и фильтрации нет — `page`/`offset`/`limit` и любые параметры раздела/типа в теле игнорируются, `total_pages` всегда 1 (или 0, если результатов нет).
- **OAuth scopes API Вики не проверяет** — токен только с `wiki:read` всё равно может писать. Read-only гарантируется исключительно нерегистрацией write-инструментов (`WIKI_READ_ONLY=true`). *Благодарность: впервые публично об этом сообщил проект [slartus/mcp-yandex-wiki](https://github.com/slartus/mcp-yandex-wiki).*
- Поиск точной фразы в кавычках работает; `-минус` и булевы операторы игнорируются.
- API ревизий/истории/обратных ссылок **не существует** — сценарии "кто ссылается сюда" невозможны.
- `created_at`/`modified_at`/`comments_count`/`is_readonly` — не top-level поля страницы; получайте их через `page_get` с `fields=["attributes"]`.
- Ответы об ошибках приходят в **двух форматах конверта** (`message` как строка-или-null плюс `details`, либо как список плюс `level`); клиент разбирает оба.
- Заголовки rate limit не отдаются (`X-RateLimit-*`/`Retry-After` отсутствуют).
- `GET /pages/{id}/resources?q=` — единственный серверный *текстовый* фильтр во всём API (поиск по названиям вложений/таблиц одной страницы) — доступен через `page_get_resources`.

Орг-нейтральные скрипты в [`scripts/`](scripts/) (`probe_api*.sh`, `smoke.sh`) — живая
документация этого поведения, их можно перезапускать на своей организации
(секреты через переменные окружения или файл `$SECRETS`; вывод проб пишется в `raw/`,
который в `.gitignore`, потому что содержит реальные данные организации).

## Быстрый старт (Claude Desktop / Cursor / Windsurf)

Docker, read-only (рекомендуется для агентов):

```json
{
  "mcpServers": {
    "yandex-wiki-search": {
      "command": "docker",
      "args": ["run","--rm","-i",
        "-e","WIKI_TOKEN","-e","WIKI_ORG_ID","-e","WIKI_READ_ONLY=true",
        "ghcr.io/dlbolshov/yandex-wiki-search-mcp:latest"],
      "env": {"WIKI_TOKEN":"...","WIKI_ORG_ID":"..."}
    }
  }
}
```

`uvx` (PyPI):

```json
{
  "mcpServers": {
    "yandex-wiki-search": {
      "command": "uvx",
      "args": ["yandex-wiki-search-mcp"],
      "env": {
        "WIKI_TOKEN": "...",
        "WIKI_ORG_ID": "...",
        "WIKI_READ_ONLY": "true"
      }
    }
  }
}
```

## Почему именно этот набор

Набор инструментов основан на областях публичного API Yandex Wiki, наиболее полезных в MCP-сценариях:

- полнотекстовое обнаружение страниц и файлов
- чтение и изменение страниц
- чтение и изменение динамических таблиц
- обход поддерева документации
- работа с комментариями
- работа с ресурсами и вложениями
- безопасное удаление и восстановление
- multipart upload локальных файлов с прикреплением к странице

## Особенности grids

- При `WIKI_READ_ONLY=true` скрываются все non-read tools, а не только grid-операции.
- Там, где API требует optimistic locking, mutation tools принимают `revision`.
- `grid_copy` возвращает metadata асинхронной операции, а не готовую копию таблицы.
- Для `grid_add_columns` каждая колонка должна содержать поле `required`, потому что это требует реальный API Yandex Wiki.
- `grid_update.default_sort` проверен на реальном API и должен передаваться как список одноэлементных словарей, например `[{"status": "asc"}, {"priority": "desc"}]`.

Официальные материалы:

- обзор API: `https://yandex.ru/support/wiki/en/api-ref/about`
- примеры API: `https://yandex.ru/support/wiki/ru/api-ref/examples`
- ресурсы страниц: `https://yandex.ru/support/wiki/ru/api-ref/pagesresources/pagesresources__resources`
- индекс API по таблицам: `https://yandex.ru/support/wiki/ru/api-ref/grids/`

## Переменные окружения

Нужен один из токенов:

- `WIKI_TOKEN`
- `WIKI_IAM_TOKEN`

И ровно один идентификатор организации:

- `WIKI_ORG_ID`
- `WIKI_CLOUD_ORG_ID`

Опционально:

- `TRANSPORT=stdio|sse|streamable-http`
- `WIKI_API_BASE_URL=https://api.wiki.yandex.net`
- `WIKI_READ_ONLY=true|false`

## Локальный запуск

```bash
uv sync --dev
uv run yandex-wiki-search-mcp
```

## Docker deployment

Docker-образ требует те же базовые переменные окружения, что и локальный запуск:

- один из `WIKI_TOKEN` или `WIKI_IAM_TOKEN`
- ровно один из `WIKI_ORG_ID` или `WIKI_CLOUD_ORG_ID`
- `TRANSPORT=streamable-http` для HTTP deployment

Опционально:

- `HOST=0.0.0.0`
- `PORT=8000`
- `WIKI_API_BASE_URL=https://api.wiki.yandex.net`
- `WIKI_READ_ONLY=true|false`

## Использование готового образа (рекомендуется)

```bash
# Используя файл окружения
docker run --env-file .env -p 8000:8000 ghcr.io/dlbolshov/yandex-wiki-search-mcp:latest

# С встроенными переменными окружения
docker run -e WIKI_TOKEN=ваш_токен \
           -e WIKI_ORG_ID=ваш_org_id \
           -e TRANSPORT=streamable-http \
           -p 8000:8000 \
           ghcr.io/dlbolshov/yandex-wiki-search-mcp:latest
```

MCP endpoint будет доступен по адресу `http://localhost:8000/mcp`.

## Сборка образа локально

```bash
docker build -t yandex-wiki-search-mcp .
```

## Docker Compose

**Используя готовый образ:**

```yaml
version: '3.8'
services:
  mcp-wiki:
    image: ghcr.io/dlbolshov/yandex-wiki-search-mcp:latest
    ports:
      - "8000:8000"
    environment:
      - WIKI_TOKEN=${WIKI_TOKEN}
      - WIKI_ORG_ID=${WIKI_ORG_ID}
      - TRANSPORT=streamable-http
```

**Сборка локально:**

```yaml
version: '3.8'
services:
  mcp-wiki:
    build: .
    ports:
      - "8000:8000"
    environment:
      - WIKI_TOKEN=${WIKI_TOKEN}
      - WIKI_ORG_ID=${WIKI_ORG_ID}
      - TRANSPORT=streamable-http
```

Если позже понадобится Redis-backed OAuth storage, текущий [`compose.yaml`](compose.yaml) можно использовать как основу для Redis сервиса.

## Разработка

Перед коммитом и перед созданием или обновлением merge request нужно прогонять полный локальный набор проверок из [CONTRIBUTING.md](CONTRIBUTING.md).

## Тесты

```bash
uv run pytest
```

## Благодарности

Проект является форком [APonkratov/yandex-wiki-mcp](https://github.com/APonkratov/yandex-wiki-mcp)
(`ya-yandex-wiki-mcp`) Александра Понкратова — отличного, хорошо протестированного Python MCP
сервера для API Yandex Wiki под лицензией Apache-2.0. Форк добавляет полнотекстовый поиск
(`page_search`) и ребрендинг; исходный копирайт и лицензия сохранены
(см. [LICENSE](LICENSE) и [NOTICE](NOTICE)).
