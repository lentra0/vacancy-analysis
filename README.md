# Анализ рынка вакансий

Собираем вакансии с HeadHunter в SQLite. В базе остаются исходные описания, явно указанные навыки и история запусков. Вакансии можно выгрузить в JSON для дальнейшей обработки.

## Запуск

Скачайте файл для своей системы из [Releases](https://github.com/lentra0/vacancy-analysis/releases). В Windows запускайте из PowerShell, в Linux дайте файлу право на запуск. Chromium входит в сборку. На Linux нужны его системные библиотеки.

```powershell
.\vacancy-analysis-windows-x64.exe run --text DevOps --area 1 --limit 5
.\vacancy-analysis-windows-x64.exe export > vacancies.json
```

```sh
chmod +x vacancy-analysis-linux-x64
./vacancy-analysis-linux-x64 run --text DevOps --area 1 --limit 5
./vacancy-analysis-linux-x64 export > vacancies.json
```

Для запуска из исходников нужен Python 3.11 или новее. После установки используйте `python -m vacancy_analysis` с теми же параметрами.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m vacancy_analysis run --text DevOps --area 1 --limit 5
python -m vacancy_analysis export > vacancies.json
```

В PowerShell виртуальное окружение активируется командой `.\.venv\Scripts\Activate.ps1`. Если в Ubuntu не хватает библиотек Chromium, выполните `python -m playwright install --with-deps chromium`.

## Параметры

| Где | Параметр | Что задаёт |
| --- | --- | --- |
| Перед командой | `--db ПУТЬ` | Файл SQLite. По умолчанию `data/vacancies.sqlite3` |
| `run`, `watch` | `--text ЗАПРОС` | Поисковый запрос. Обязательный |
| `run`, `watch` | `--area ID` | Регион HH. Например, `1` для Москвы |
| `run`, `watch` | `--source web\|api` | Сайт через Chromium или официальный API. По умолчанию `web` |
| `run`, `watch` в режиме `web` | `--limit N` | Сколько карточек можно открыть за запуск. По умолчанию 25 |
| `run`, `watch` в режиме `web` | `--pages N` | Сколько страниц выдачи просмотреть. По умолчанию 3 |
| `run`, `watch` в режиме `web` | `--period ДНИ` | Период выдачи HH. Допустимы 1, 3, 7 и 30. По умолчанию 1 |
| `run`, `watch` в режиме `web` | `--delay СЕКУНДЫ` | Пауза между открытиями страниц. По умолчанию 5 |
| `run`, `watch` в режиме `web` | `--profile КАТАЛОГ`, `--headed` | Сохранить профиль Chromium или показать окно браузера |
| `run` в режиме `api` | `--from ДАТА`, `--to ДАТА` | Начало и конец окна в ISO 8601 с часовым поясом. Задаются вместе и включают режим `api` по умолчанию |
| `watch` | `--interval СЕКУНДЫ` | Пауза после каждого запуска. Первый сбор начинается сразу |
| `login` | `--profile КАТАЛОГ` | Профиль для ручного входа в HH. По умолчанию `data/hh-profile` |
| `runs` | `--limit N` | Сколько последних запусков показать. По умолчанию 10 |
| `export` | `--limit N` | Сколько свежих карточек выгрузить. По умолчанию все |

## Регулярный сбор

`watch` берёт свежую выдачу и пропускает вакансии, которые уже сохранены для того же запроса и региона. `run` перечитывает карточки и обновляет их в базе. Дубли в выдаче определяются по ID. Если HH ограничит доступ или покажет капчу, сбор остановится и выведет причину.

Если нужен вход в аккаунт, откройте браузер командой `login`, войдите в HH и нажмите Enter в терминале. Используйте тот же каталог профиля при сборе.

```sh
python -m vacancy_analysis login
python -m vacancy_analysis watch --text DevOps --area 1 \
  --profile data/hh-profile --interval 7200
python -m vacancy_analysis runs --limit 3
```

Для API передайте токен через `HH_TOKEN`, а идентификатор приложения и контакт через `HH_USER_AGENT`. Сбор по точному окну доступен в этом режиме. [Документация API HH](https://api.hh.ru/openapi/redoc)

```sh
python -m vacancy_analysis run --source api --text DevOps --area 1 \
  --from 2026-11-23T12:00:00+03:00 --to 2026-11-23T13:00:00+03:00
```

`run` возвращает код `0` при успехе, `2` при частичном результате и `1` при ошибке. `export` печатает массив карточек с полями `external_id`, `title`, `description_raw`, `published_at` и `metadata`, а также ссылкой, источником и временем сбора.

## Тесты и сборка

```sh
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p test_hh_web.py -v
```

Тест веб-сборщика читает две страницы выдачи и карточки из `tests/fixtures`, затем проверяет результат команды `export`. Другие тесты проверяют дубли, остановку при капче, частичные сбои и работу SQLite. Для прогона через Chromium установите его и задайте `HH_TEST_CHROMIUM=1`. Страницы в этом режиме также подставляются из тестов.

Сборку выполняйте на целевой системе x64. Результат появится в `dist/`.

```sh
python -m pip install -r requirements-build.txt
python scripts/build.py
```

[Схема проекта и таблицы базы](docs/architecture_vacancy_analysis.md)
