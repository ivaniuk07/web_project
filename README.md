# Генератор субтитрів

Навчальний Django-сервіс приймає відео, у фоні розпізнає мовлення через Whisper
і повертає MP4 із вбудованими субтитрами та окремий SRT. Користувач має власну
історію, бачить етапи обробки та може скасувати задачу. Прості HTML-сторінки без
окремої фронтенд-збірки. Потрібна аудіодоріжка з мовленням.

Схема: Django → балансувальник обчислень → маршрутизатор черг → окремий Redis
кожного вузла → Celery + Whisper + FFmpeg. PostgreSQL і файлове сховище спільні.
Два воркери можна запустити на одному комп'ютері або на різних VM.
Детальна вихідна схема: `Architecture/architecture-two-redis.png`.

## Швидкий запуск на одному комп'ютері

Потрібні Docker Engine/Desktop і Docker Compose v2. Для першого запуску потрібен
інтернет для образів та моделі Whisper tiny. Рекомендовано 4 ГБ вільної RAM
для двох воркерів; реальне споживання залежить від відео та моделі.

```sh
cp .env.example .env
docker compose up --build -d
```

Відкрийте [сайт](http://127.0.0.1:8001/), зареєструйтеся, завантажте ролик і
натисніть «Створити відео із субтитрами». Після завершення відкриється плеєр,
нижче — завантаження MP4 і SRT. Сторінка автоматично оновлюється під час роботи.

```sh
docker compose ps
docker compose logs worker1 worker2 dispatcher
docker compose exec web python manage.py test
docker compose stop
docker compose start
```

У Docker використовуються база `subtitles`, черга `subtitles` і named volumes
postgres-data/media/model-cache/redis*-data. `stop` зберігає їх. `down -v`
видаляє дані томів, тому для звичайної зупинки його не використовуйте.

Для операторської сторінки створіть адміністратора:

```sh
docker compose exec web python manage.py createsuperuser
```

Увійдіть ним та відкрийте `/operations/` або посилання «Стан системи».
Сторінка доступна активним користувачам із `is_staff=True`. Для видачі staff
вже наявному обліковому запису використайте операторську Django shell:

```sh
docker compose exec web python manage.py shell
```

```python
from django.contrib.auth import get_user_model
get_user_model().objects.filter(username='YOUR_USERNAME').update(is_staff=True)
```

Це операторська дія в терміналі, не публічний API. Staff бачить зведення всіх
активних задач, але звичайні endpoints перегляду/завантаження зберігають перевірку власника.

## Дві окремі VM

Інструкції та конфігурації:

- [Мережа, PostgreSQL, власні Redis та NFS](deploy/MULTI_HOST.md).
- [HTTPS і локальний CA](deploy/HTTPS.md).
- [Дослід обмеженого автозапуску node-3](deploy/AUTOSCALING.md).
- [Сценарій захисту](deploy/DEFENSE.md).
- [Фактична перевірка двох VM](deploy/STAGE7_EVIDENCE.md).
- [Репетиція та додаткові функції](deploy/STAGE8_EVIDENCE.md).

Перевірена топологія: Ubuntu ARM64, 10.211.55.3 — Django, dispatcher, PostgreSQL,
Redis 1, node-1 та NFS; Kali ARM64, 10.211.55.5 — Redis 2 і node-2.
На обох хостах однаковий код і `/srv/subtitles/media`, у контейнерах `/app/media`.
Файли не передаються в повідомленнях Redis: воркер отримує UUID, шлях і токен спроби.
З HTTPS сайт доступний за `https://10.211.55.3:8443/` після налаштування довіри
до локального CA. IP залежать від вашої мережі. Це окрема БД від локального запуску.

На A скопіюйте `deploy/coordinator.env.example` у `.env.coordinator`, на B —
`deploy/worker.env.example` у `.env.worker`. Замініть документаційні IP та всі
`REPLACE_...`, задайте однакові дані PostgreSQL і WORKER_NODES. Права env-файлів — 600.
Реальні env-файли виключені з Git і Docker build context.

При HTTPS завжди використовуйте той самий набір Compose-файлів з
`-f compose.https.yaml`, включно з наступними `up` і командами автозапуску.
Overlay прибирає зовнішній HTTP-порт Django. Без нього базова конфігурація A
залишається HTTP-демонстрацією на порту 8000. Докладний порядок є в інструкціях.

## Локальна розробка без контейнера Django

Потрібні Python 3.11+ (у Docker 3.12), PostgreSQL, два Redis і FFmpeg/FFprobe із
фільтром `subtitles`/libass. `requirements.txt` перелічує Python-залежності,
які `pip` встановлює однією командою. `.env` автоматично читає Compose,
а звичайний Python-процес використовує експортовані змінні середовища.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
createdb -h 127.0.0.1 web_project
python manage.py migrate
docker compose up -d redis1 redis2
python manage.py runserver
```

За замовчуванням PostgreSQL `web_project`, користувач ОС, localhost:5432.
Для іншої конфігурації експортуйте POSTGRES_* перед командами. Локальний сайт
[127.0.0.1:8000](http://127.0.0.1:8000/) використовує `media/` і чергу
`subtitles-local`, відокремлену від Docker. В інших терміналах з активним `.venv`:

```sh
python manage.py dispatch_jobs
```

```sh
WORKER_NODE_NAME=node-1 CELERY_BROKER_URL=redis://localhost:6379/0 celery -A config worker -l info -Q subtitles-local --pool=threads --concurrency=1 --hostname=local-node1@%h
```

```sh
WORKER_NODE_NAME=node-2 CELERY_BROKER_URL=redis://localhost:6380/0 celery -A config worker -l info -Q subtitles-local --pool=threads --concurrency=1 --hostname=local-node2@%h
```

На macOS можна встановити повну збірку `brew install ffmpeg-full` і експортувати
`FFMPEG_BINARY=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg` у кожному терміналі.
У цих ARM64 Parallels VM перевірено `OPENBLAS_CORETYPE=ARMV8` для усунення
SIGILL під час імпорту NumPy. Залежить від конкретного віртуального CPU.

```sh
python manage.py check
python manage.py test
python manage.py makemigrations --check --dry-run
```

## Налаштування

Повний приклад для Compose — `.env.example`, для VM — `deploy/*.env.example`.

| Змінна | Типово | Призначення |
| --- | --- | --- |
| DJANGO_SECRET_KEY | локальний fallback | Задайте випадковий секрет для демонстраційного сервера |
| DJANGO_DEBUG | true | Вимкніть на VM |
| DJANGO_ALLOWED_HOSTS | localhost,127.0.0.1 | Дозволені host/IP без порту |
| POSTGRES_DB / USER / PASSWORD / HOST / PORT | web_project / користувач ОС / порожній / 127.0.0.1 / 5432 | База станів |
| MEDIA_ROOT | media/ | Спільні джерела, SRT і MP4 |
| WHISPER_MODEL | tiny | Назва моделі або локальна папка моделі |
| FFMPEG_BINARY | ffmpeg | Шлях до FFmpeg із libass; ffprobe потрібний у PATH |
| MAX_UPLOAD_BYTES | 104857600 | 100 МіБ |
| MAX_VIDEO_SECONDS | 600 | Максимум 10 хвилин |
| ALLOWED_VIDEO_EXTENSIONS | mp4,mov,mkv,webm,avi | Перелік розширень |
| MAX_ACTIVE_JOBS_PER_USER | 3 | queued/running, включно з ще не запущеними upload |
| JOB_TIMEOUT_SECONDS | 900 | Загальний бюджет спроби |
| MEDIA_PROBE_TIMEOUT / FFMPEG_TIMEOUT | 20 / 120 | Ліміти перевірки/підготовки аудіо |
| TRANSCRIBE_TIMEOUT / VIDEO_RENDER_TIMEOUT | 600 / 600 | Whisper / рендер |
| CELERY_BROKER_URL | redis://localhost:6379/0 | Власний Redis воркера |
| CELERY_TASK_DEFAULT_QUEUE | subtitles-local | Черга цього розгортання |
| WORKER_NODE_NAME | node-1 | Унікальна назва вузла |
| WORKER_NODES | два локальні вузли | JSON name/broker_url/queue/enabled |
| NODE_TIMEOUT_SECONDS / NODE_RETRY_SECONDS | 20 / 10 | Втрата heartbeat / cooldown Redis |
| DISPATCH_INTERVAL_SECONDS / DISPATCH_RESERVATION_SECONDS | 2 / 15 | Цикл dispatcher / повтор незавершеної публікації |
| MAX_DISPATCH_ATTEMPTS | 5 | Межа спроб передачі |
| ETA_FALLBACK_SECONDS | 60 | Приблизна тривалість без історичних вимірювань |
| DJANGO_SECURE_COOKIES / DJANGO_TRUST_PROXY / DJANGO_SECURE_SSL_REDIRECT | false | Вмикає HTTPS-overlay |
| DJANGO_CSRF_TRUSTED_ORIGINS | порожній | Точні scheme/host/port |
| HTTPS_HOST / HTTPS_PORT | із env / 8443 | Приватний TLS endpoint |
| EXTRA_REDIS_PASSWORD | лише явне значення | Пароль Redis резервного node-3 на VM A |

## Пояснення архітектури й SOLID

| Компонент | Відповідальність |
| --- | --- |
| Django views/forms | Авторизація, HTTP, перевірка введення та приватні файли |
| services.py | Життєвий цикл задачі й захист переходів токеном |
| LeastLoadedBalancer | Вибір доступного вузла за queued/running і чергування при рівності |
| RedisQueueRouter | Публікація в Redis обраного вузла |
| dispatch_jobs | Повтор спроб, відновлення після втрати вузла |
| Redis | Окрема черга повідомлень кожного вузла |
| Celery | Виконання фонових задач і heartbeat |
| Whisper / FFmpeg | Розпізнавання / аудіо та текст у кадрі |
| PostgreSQL | Користувачі, задачі, вузли, навантаження, токени та час |
| NFS / media volume | Єдині файли для Django та всіх воркерів |
| monitoring.py | Оцінка очікування та зведення навантаження |
| Caddy | TLS перед Django |

SRP: HTTP, вибір вузла, публікація та обробка файлів розділені. OCP: додавання
вузла через WORKER_NODES не змінює алгоритм вибору. LSP: реалізації
SpeechRecognizer/TaskRouter дотримуються контрактів сегментів/публікації.
ISP: ці Protocol містять лише потрібні recognize/publish. DIP: обробка й
Dispatcher приймають ці вузькі залежності, що дозволяє підставляти інші реалізації.

Прогноз очікування приблизний: використовує до 50 успішних вимірювань за вузлами
та черги за припущення concurrency=1. Відсутність історії дає fallback, втрата
всіх вузлів — повідомлення, що оцінка недоступна. Це час до початку, не гарантія
тривалості конвертації. На `/operations/` оператор бачить навантаження без паролів.

Зміна токена при скасуванні/перепризначенні забороняє старій спробі публікувати
результат. Відновлення починає нову спробу, не переносить проміжний прогрес
Whisper; готовий SRT може бути використаний повторно. Повтор CPU-обчислень
можливий до перевірки токена. PostgreSQL і NFS на A залишаються єдиними точками
відмови. Локальна конфігурація використовує Django runserver для навчальної демонстрації.
