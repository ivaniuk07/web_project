# Генератор субтитрів

Проста HTML-сторінка дозволяє завантажити відео, створити субтитри та завантажити SRT. Відео зберігаються в `media/videos/`, результати — у `media/subtitles/`. Перевіряються розширення, розмір до 100 МБ і наявність аудіодоріжки під час обробки.

## Запуск

Потрібен Python 3.11 або новіший і запущений PostgreSQL.
За замовчуванням Django підключається до бази `web_project` на `127.0.0.1:5432`
від імені користувача операційної системи. Параметри можна перевизначити змінними
`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
createdb -h 127.0.0.1 web_project
python manage.py migrate
python manage.py runserver
```

Відкрити http://127.0.0.1:8000/.

```bash
python manage.py check
python manage.py test
```

## Наступні версії

1. Користувачі, модель задачі та історія у PostgreSQL.
2. Celery і фонова обробка зі станами.
3. Балансувальник обчислень, маршрутизатор та два окремі Redis для двох воркерів.
4. Розгортання на різних комп’ютерах, підтримка більшої кількості вузлів.
5. Скасування, ліміти, перевірки та сценарій захисту.

Цільова архітектура зображена в `Architecture/architecture-two-redis.png`. Шаблон відповідає за HTML, форма — за перевірку вводу, view — за прийом запиту та збереження файлу. Складніші компоненти додаватимуться разом із відповідними функціями.

## Етапи 1 і 2

FFmpeg та FFprobe мають бути доступні в PATH. Для готового MP4 потрібна збірка FFmpeg із libass, її встановлення описано нижче. Для розпізнавання використовується Whisper tiny на CPU. Перший запуск завантажує модель з інтернету.

На сторінці після завантаження відео натисніть «Створити відео із субтитрами», дочекайтеся завершення запиту й завантажте MP4 або SRT. На цьому етапі обробка синхронна. Фонові задачі передбачені етапом 5. FFmpeg готує аудіо, Whisper розпізнає мовлення, окрема функція формує часові позначки SRT, а FFmpeg вбудовує текст у MP4.

Обробити файл із термінала також можна так:

```bash
python manage.py transcribe_video video.mp4 --output media/subtitles/video.srt
```

### Docker Compose

```bash
cp .env.example .env
docker compose up --build -d
```

Docker-версія відкривається на http://127.0.0.1:8001/. У ній є Django, PostgreSQL, два Redis і два підготовлені Celery-воркери. Воркери підключені до різних Redis і очікують появи задач у наступних етапах. Автоматичний розподіл роботи буде реалізований у етапі 6.

У Docker PostgreSQL використовує окрему базу `subtitles`, а локальний запуск — `web_project`. Параметри Docker записані в `.env`. Дані PostgreSQL та відео/SRT зберігаються в постійних томах `postgres-data` і `media`. Том медіа монтується до Django й обох воркерів. Локальний запуск зберігає файли у своїй папці `media`.

```bash
docker compose ps
docker compose logs worker1 worker2
docker compose exec web python manage.py test
docker compose stop
docker compose start
```

Для тесту з відео на хості:

```bash
docker compose cp video.mp4 web:/app/media/video.mp4
docker compose exec web python manage.py transcribe_video /app/media/video.mp4 --output /app/media/subtitles/video.srt
```

Для пояснення SOLID: форма перевіряє введення, view керує HTTP, `extract_audio` працює з FFmpeg, `WhisperRecognizer` займається мовленням, `render_srt` форматує результат. `generate_subtitles` приймає інтерфейс `SpeechRecognizer`, тому інший механізм розпізнавання можна підставити без зміни процесу конвертації.

## Відео із субтитрами

Кнопка «Створити відео із субтитрами» створює MP4 із текстом у кадрі. Готовий ролик можна переглянути у плеєрі й завантажити. Під ним доступний окремий SRT. MP4 зберігається в `media/rendered/`, SRT — у `media/subtitles/`. Для відео з уже готовим SRT натисніть кнопку обробки ще раз, щоб створити MP4.

Для накладання тексту потрібен FFmpeg із фільтром `subtitles` та libass. Docker-образ містить відповідний FFmpeg і шрифт DejaVu. На Apple Silicon macOS можна встановити повну збірку й указати її перед запуском:

```bash
brew install ffmpeg-full
export FFMPEG_BINARY=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
python manage.py runserver
```

Для іншого розташування FFmpeg змініть `FFMPEG_BINARY`. Максимальний час кодування MP4 налаштовується через `VIDEO_RENDER_TIMEOUT` (типово 600 секунд). Кодування виконується синхронно під час запиту.
