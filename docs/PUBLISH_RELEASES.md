# Публикация GitHub Releases — StockPilot v0.1.0–v0.5.0

Релизы v0.1.0, v0.2.0, v0.3.0 и v0.4.0 уже опубликованы владельцем репозитория. Для v0.5.0 исходный код слит в `main`, но запись GitHub Release создаётся отдельно: GitHub не публикует её автоматически по одному коммиту.

## Почему публикация не завершилась автоматически

14 ноября 2026 года были проверены попытки GitHub Actions создать оба выпуска:

- Workflow `Publish StockPilot 0.1.0 and 0.2.0 releases` вернул **HTTP 403 Resource not accessible by integration** при POST `/releases`.
- Следующая попытка создать теги через `git push` отклонена GitHub: **refusing to allow a GitHub App to create or update workflow .github/workflows/ci.yml without workflows permission**.

Это ограничение прав используемой автоматизации, а не ошибка сборки приложения. Не выдавайте себе новый токен в переписке и не добавляйте токены в исходный код.

## Вариант 1 — создать Releases через интерфейс GitHub (проще)

Перейдите на страницу: https://github.com/Onmaynec/StockPilot/releases/new

### StockPilot v0.1.0

1. В **Choose a tag** введите `v0.1.0` и выберите создание нового тега.
2. В качестве **Target** укажите **полный коммит** `5abd53f8c61439447bb8699740f3f7cbad21403e`. Это код первой версии *до* обновления v0.2.0.
3. Заголовок: `StockPilot v0.1.0 — первый MVP`.
4. Текст описания возьмите из [docs/RELEASE_0.1.0.md](RELEASE_0.1.0.md).
5. **Не отмечайте** «Set as a pre-release» и «Save as draft». Нажмите **Publish release**.

### StockPilot v0.2.0

1. Снова откройте https://github.com/Onmaynec/StockPilot/releases/new .
2. Новый тег `v0.2.0`, **Target** `1cc94c2fb0d7303f15f38998e012a4c0b302e2fa` (проверенный итоговый коммит v0.2.0).
3. Заголовок: `StockPilot v0.2.0 — Operations Update`.
4. Текст описания возьмите из [docs/RELEASE_0.2.0.md](RELEASE_0.2.0.md).
5. Опубликуйте обычный Release; отметьте v0.2.0 как последний (Latest).

### StockPilot v0.3.0

1. Откройте https://github.com/Onmaynec/StockPilot/releases/new .
2. Укажите новый тег `v0.3.0`, а в качестве Target выберите коммит **`1f7346e`** (полный идентификатор: `1f7346e80a03fc8333fd1eee87e65013d5e56edc`).
3. Название: `StockPilot v0.3.0 — Inventory Accuracy & Reliability`.
4. Описание: [docs/RELEASE_0.3.0.md](RELEASE_0.3.0.md), включая результаты CI и ограничения.
5. Опубликуйте как обычный Release и выставьте **Latest**. Сборка является архивом исходного кода; готового exe-установщика нет.

### StockPilot v0.4.0

1. Перейдите на https://github.com/Onmaynec/StockPilot/releases/new .
2. Создайте тег `v0.4.0`. **Target**: `d55ab40` (полный SHA: `d55ab40246622101f49f83725297d26208e19607`) — точный коммит слияния PR #8.
3. Название: `StockPilot v0.4.0 — Security & Operations`.
4. Текст: [docs/RELEASE_0.4.0.md](RELEASE_0.4.0.md), включая важное требование сменить `.env` перед обновлением.
5. Опубликуйте **обычный Release** (не Draft и не Prerelease), пометьте как Latest.

### StockPilot v0.5.0

1. Откройте https://github.com/Onmaynec/StockPilot/releases/new .
2. Укажите новый тег `v0.5.0`, Target — `106e6fa` (полный SHA `106e6fa224fb55b068c5d02ebf3636c63cf353f3`). Это итоговый merge-коммит PR #9, а не позднейший документальный коммит.
3. Заголовок: `StockPilot v0.5.0 — Warehouse Administration`.
4. Описание: [docs/RELEASE_0.5.0.md](RELEASE_0.5.0.md). Включены новые функции и подтверждённые результаты тестирования; нагрузочная AT-10 остаётся открытой.
5. Опубликуйте обычный Release (не draft/prerelease), отметьте как Latest.

В разделе https://github.com/Onmaynec/StockPilot/releases должны появиться **пять карточек**, а GitHub автоматически предоставит архивы `Source code (zip)` и `Source code (tar.gz)`. Готовых бинарных установщиков в этих версиях нет.

## Вариант 2 — автоматизация одним запуском

Есть ручной [workflow](../.github/workflows/publish-releases.yml). Для его запуска необходимо:

1. В GitHub Settings → Secrets and variables → Actions создайте repository secret `STOCKPILOT_RELEASE_TOKEN` с **fine-grained personal access token** для этого репозитория, выданным владельцем, с **Contents: Read and write** и **Workflows: Read and write**. Токен должен иметь минимально необходимый срок действия.
2. В Actions выберите **Publish StockPilot releases** → **Run workflow** на `main`.
3. Проверьте пять записей в Releases и теги `v0.1.0`/`v0.2.0`/`v0.3.0`/`v0.4.0`/`v0.5.0`.

**Не вставляйте токен в чат, README, Issues или GitHub Actions YAML.** Если workflow завершится ошибкой, сохраняйте лог без секретов и проверяйте права токена. После публикации удалите или отзовите временный токен.

Повторный запуск не должен дублировать существующие Releases: каждое создание предваряется `gh release view`.
