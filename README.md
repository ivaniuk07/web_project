# Генератор субтитрів

Навчальний Django-сервіс приймає відео, у фоні розпізнає мовлення через Whisper
і повертає MP4 із вбудованими субтитрами та окремий SRT. Користувач має власну
історію, бачить етапи обробки та може скасувати задачу. Прості HTML-сторінки без
окремої фронтенд-збірки. Потрібна аудіодоріжка з мовленням.

Схема: Django → балансувальник обчислень → маршрутизатор черг → окремий Redis
кожного вузла → Celery + Whisper + FFmpeg. PostgreSQL і файлове сховище спільні.
Перший воркер працює на Ubuntu VM, другий — на Mac.
[Схема архітектури](Architecture/architecture-two-redis.png).

## Запуск на Ubuntu VM та Mac

Перед першим запуском налаштуйте приватну мережу, NFS і файли середовища за
[інструкцією для двох хостів](deploy/MULTI_HOST.md). Використовуйте однакову
версію коду на обох машинах. Для HTTPS налаштуйте
[довіру до локального CA](deploy/HTTPS.md).

На Mac запустіть Redis другого вузла:

```sh
docker compose --env-file .env.worker -f compose.worker.yaml up -d redis
```

На Ubuntu запустіть координатор і запишіть маркер спільної папки:

```sh
sudo docker compose --env-file .env.coordinator -f compose.coordinator.yaml -f compose.https.yaml up -d --build --wait
sudo docker compose --env-file .env.coordinator -f compose.coordinator.yaml -f compose.https.yaml exec -T web python manage.py check_deployment --write-marker coordinator
```

На Mac запустіть воркер і перевірте його журнал:

```sh
docker compose --env-file .env.worker -f compose.worker.yaml up -d --build worker
docker compose --env-file .env.worker -f compose.worker.yaml logs -f worker
```

Відкрийте [сайт](https://10.211.55.3:8443/), зареєструйтеся, завантажте ролик і
натисніть «Створити відео із субтитрами». Для перегляду вузлів і навантаження на
сторінці `/operations/` створіть staff-акаунт на Ubuntu:

```sh
sudo docker compose --env-file .env.coordinator -f compose.coordinator.yaml -f compose.https.yaml exec web python manage.py createsuperuser
```

## REST API і Postman

Після встановлення `requirements.txt` виконайте `python manage.py migrate`.
Отримайте токен для наявного користувача командою `python manage.py drf_create_token YOUR_USERNAME`.
У Docker додайте `exec web` до тієї самої команди Compose, яку використовуєте на Ubuntu.

Імпортуйте [колекцію Postman](deploy/Titry.postman_collection.json). Задайте для `base_url` HTTPS-адресу
координатора з [інструкції HTTPS](deploy/HTTPS.md). Задайте `api_token` у локальному
оточенні Postman. Колекція передає його в заголовку `Authorization: Token <token>`.

| Запит | Результат |
| --- | --- |
| `POST /api/jobs/` | `multipart/form-data` з файлом `video` та необов’язковою темою `subtitle_style`. Відповідь `201 Created` містить UUID, а заголовок `Location` веде до статусу. Диспетчер автоматично підбирає завдання з черги. |
| `GET /api/jobs/<uuid>/` | Статус, етап, вузол, тривалість відео, час і посилання `artifacts.srt` та `artifacts.mp4`, коли відповідні файли доступні. |
| `GET /api/jobs/<uuid>/subtitles/` | Файл SRT за токеном власника. |
| `GET /api/jobs/<uuid>/video/` | Готовий MP4 за токеном власника. |
| `GET /api/operations/` | Для staff. Доступність, завдання в обробці, черга та кількість успішно завершених завдань по вузлах. |

`dispatch_wait_seconds` вимірює час від призначення вузла до старту обробки
поточної спроби. `processing_seconds` рахується від старту до завершення або
поточного моменту. `estimated_wait_seconds` є прогнозом для завдань у черзі.
Поля часу мають значення `null`, якщо відповідних даних ще немає.

У запиті завантаження можна передати `subtitle_language` (наприклад, `uk` або `en`).
Порожнє або пропущене поле вмикає автоматичне визначення.

Для демонстрації надішліть два завантаження поспіль, збережіть обидва UUID та перевірте
їхні статуси. Третій ролик покаже чергу, якщо воркери ще обробляють перші два.
Колекція автоматично підставляє UUID останнього завантаження у запити статусу й файлів.
Для перевірки іншого завдання змініть `job_id`. Завантажуйте MP4 після статусу `succeeded`.

Для повторюваного порівняння використайте [сценарій вимірювання](deploy/BENCHMARK.md).

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
| MAX_UPLOAD_BYTES | 1073741824 | 1 ГіБ |
| MAX_VIDEO_SECONDS | 3600 | Максимум 1 година |
| ALLOWED_VIDEO_EXTENSIONS | mp4,mov,mkv,webm,avi | Перелік розширень |
| MAX_ACTIVE_JOBS_PER_USER | 3 | queued/running, включно з ще не запущеними upload |
| JOB_TIMEOUT_SECONDS | 7200 | Загальний бюджет спроби в секундах |
| MEDIA_PROBE_TIMEOUT / FFMPEG_TIMEOUT | 20 / 600 | Ліміти перевірки та підготовки аудіо в секундах |
| TRANSCRIBE_TIMEOUT / VIDEO_RENDER_TIMEOUT | 3600 / 3600 | Ліміти Whisper і рендеру в секундах |
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

## Пояснення архітектури й SOLID

| Компонент | Відповідальність |
| --- | --- |
| Django views/forms | Авторизація, HTTP, перевірка введення та приватні файли |
| services.py | Життєвий цикл задачі й захист переходів токеном |
| LeastLoadedBalancer | Вибір доступного вузла за прогнозом завершення нового відео й чергування при рівності |
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

Диспетчер і прогноз очікування використовують до 50 останніх успішних завдань
доступних вузлів. Швидкість вузла оцінюється як відношення сумарного часу обробки
до сумарної тривалості роликів. Вибір враховує залишок активної обробки, чергу й
прогноз для нового відео. Модель припускає один процес обробки на вузол.
Для вузла без вимірювань береться спільна оцінка або швидкість 1:1. Старі записи
без тривалості використовують середній час вузла або `ETA_FALLBACK_SECONDS`.
На `/operations/` оператор бачить навантаження без паролів.

Зміна токена при скасуванні/перепризначенні забороняє старій спробі публікувати
результат. Відновлення починає нову спробу, не переносить проміжний прогрес
Whisper; готовий SRT може бути використаний повторно. Повтор CPU-обчислень
можливий до перевірки токена. PostgreSQL і NFS на A залишаються єдиними точками
відмови. Локальна конфігурація використовує Django runserver для навчальної демонстрації.
