# Етап 7: два окремі хости

Перевірена конфігурація: дві ARM64 VM у Parallels Desktop. Ubuntu — координатор
(10.211.55.3), Kali — другий обчислювальний вузол (10.211.55.5).
Обидва воркери запускають той самий образ `subtitle-studio:local`.
Ці адреси залежать від мережі VM. Для іншого середовища замініть їх у всіх налаштуваннях.

| Хост | Сервіси | Мережевий доступ |
| --- | --- | --- |
| A, Ubuntu | Django, dispatcher, PostgreSQL, Redis 1, worker node-1, NFS | 8000 HTTP, 5432 PostgreSQL, 6379 Redis 1, 2049 NFS |
| B, Kali | Redis 2, worker node-2, NFS-клієнт | 6379 Redis 2 |

PostgreSQL зберігає задачі, навантаження та heartbeat. Dispatcher обирає вузол,
маршрутизатор відправляє повідомлення в його Redis, Celery читає власну чергу.
На обох вузлах `/app/media` посилається на одну папку NFS. Передаються
ідентифікатор задачі, шлях файлу й токен спроби. Відео через Redis не передається.

## Підготовка

Потрібні Linux, Docker Engine з Compose v2, спільна приватна мережа,
NFS-сервер на A та NFS-клієнт на B. Для Ubuntu пакети:

```sh
sudo apt-get update
sudo apt-get install docker.io docker-compose-v2 nfs-kernel-server nfs-common
sudo systemctl enable --now docker
```

Для Kali назва пакета Compose — `docker-compose`, NFS-сервер на B не потрібний.
У командах нижче `sudo` потрібний лише без налаштованого доступу до Docker.
На обох хостах розмістіть однакову версію проєкту, наприклад `/opt/subtitles-project`.

Ця конфігурація призначена для навчальної демонстрації у приватній мережі.
Обмежте мережевими правилами PostgreSQL і Redis потрібними хостами, NFS — лише B.
Сервіси прив'язуються до `PRIVATE_BIND_IP`. HTTP використовує Django runserver.
Перед публічним розгортанням потрібні окремі налаштування вебсервера, TLS і мережевого захисту.

## Одне спільне сховище

На A створіть каталог із власником UID/GID 10001 — користувачем контейнера:

```sh
sudo mkdir -p /srv/subtitles/media
sudo chown 10001:10001 /srv/subtitles/media
sudo mkdir -p /etc/exports.d
```

Запишіть у `/etc/exports.d/subtitles.exports`, замінивши адресу B:

```text
/srv/subtitles/media 10.211.55.5(rw,sync,no_subtree_check,root_squash)
```

```sh
sudo exportfs -ra
sudo systemctl enable --now nfs-kernel-server
```

На B:

```sh
sudo mkdir -p /srv/subtitles/media
sudo mount -t nfs -o vers=4,proto=tcp 10.211.55.3:/srv/subtitles/media /srv/subtitles/media
findmnt /srv/subtitles/media
```

Для збереження після перезапуску додайте до `/etc/fstab` на B один рядок:

```text
10.211.55.3:/srv/subtitles/media /srv/subtitles/media nfs defaults,vers=4,_netdev,nofail,x-systemd.automount 0 0
```

Після зміни виконайте `sudo systemctl daemon-reload`.
Звичайна локальна папка на B не замінює спільне сховище. Воркер B перед стартом
перевіряє наявність маркера, створеного координатором.

## Налаштування та однаковий код

На A:

```sh
cp deploy/coordinator.env.example .env.coordinator
chmod 600 .env.coordinator
```

На B:

```sh
cp deploy/worker.env.example .env.worker
chmod 600 .env.worker
```

Замініть усі `REPLACE_...` та документаційні IP. Згенеруйте випадкові hex-секрети
(наприклад `python3 -c 'import secrets; print(secrets.token_hex(32))'`).
PostgreSQL має однакові DB/user/password на обох хостах. Кожен Redis має свій пароль.
`WORKER_NODES` — однаковий JSON на обох хостах із зовнішніми адресами Redis A і B.
`LOCAL_BROKER_URL` використовує внутрішнє ім'я `redis` і пароль Redis власного хоста.
`DJANGO_ALLOWED_HOSTS` на A містить адресу, через яку відкриваєте сайт.
Справжні `.env.*` виключені з Git і Docker build context.

На A зберіть образ і перенесіть його на B. Для різних архітектур CPU збирайте
однаковий вихідний код окремо на кожній архітектурі.

```sh
sudo docker build -t subtitle-studio:local .
sudo docker save -o /tmp/subtitle-studio.tar subtitle-studio:local
```

Перенесіть архів звичним способом на B, потім:

```sh
sudo docker load -i /tmp/subtitle-studio.tar
```

В обох env залиште `WHISPER_MODEL=tiny` для завантаження моделі через інтернет.
Для автономної демонстрації можна один раз розмістити завантажену модель у
`/srv/subtitles/media/.models/tiny` і задати на обох хостах
`WHISPER_MODEL=/app/media/.models/tiny`. Файли мають читатися UID 10001.
Саме цей варіант використано у фактичній перевірці.

У цих ARM64 VM імпорт NumPy спочатку завершувався SIGILL. Перевірений
обхід — додати `OPENBLAS_CORETYPE=ARMV8` в обидва env-файли та перестворити
контейнери через `up -d --no-build`. Це налаштування середовища для Parallels,
код розпізнавання спільний для обох вузлів. Див. [діагностику NumPy/OpenBLAS](https://github.com/scipy/scipy/issues/26098).

## Порядок запуску

На B спочатку запустіть лише Redis:

```sh
sudo docker compose --env-file .env.worker -f compose.worker.yaml up -d redis
```

На A:

```sh
sudo docker compose --env-file .env.coordinator -f compose.coordinator.yaml up -d --no-build
sudo docker compose --env-file .env.coordinator -f compose.coordinator.yaml exec -T web python manage.py check_deployment --all-brokers --write-marker coordinator
```

Потім на B:

```sh
sudo docker compose --env-file .env.worker -f compose.worker.yaml up -d --no-build worker
sudo docker compose --env-file .env.worker -f compose.worker.yaml exec -T worker python manage.py check_deployment --all-brokers --read-marker coordinator
```

Обидві перевірки мають показати однакові `marker.nonce`, `marker_sha256` та базу
`subtitles`. Вони реально підключаються до PostgreSQL та Redis і перевіряють
читання й запис медіа. Паролі у звіт не виводяться.
Відкрийте `http://10.211.55.3:8000/` та зареєструйте користувача.
Це окрема база демонстраційного розгортання, зі своєю історією задач.

Для порівняння файлів завершеної задачі виконайте на A (сервіс `web`) і B
(сервіс `worker`) команду `python manage.py check_deployment --job UUID` через
`docker compose ... exec -T`. Значення SHA-256 для source, subtitles та video
мають збігатися. UUID видно в адресі сторінки задачі.

## Відключення та повернення вузла

Завантажте два короткі відео майже одночасно. У сторінках задач перевірте `node-1`
та `node-2`, а в `docker compose ... logs worker` — прийняття й завершення задач.
Для перевірки збою можна призупинити VM B у Parallels, залишивши A працювати.
Після `NODE_TIMEOUT_SECONDS` (20 с за замовчуванням) dispatcher перестає обирати B
і може перепризначити його незавершені задачі на A. Після відновлення VM heartbeat
оновлюється, і вузол знову бере участь у балансуванні.

Відновлення починає нову спробу з новим токеном, а проміжний прогрес Whisper
не переноситься. Готовий SRT можна використати повторно, якщо він уже збережений
у записі задачі. Старий токен не може опублікувати результат поверх нової спроби.
Під час повернення старий процес іноді ще витрачає ресурси до наступної перевірки
скасування. Це захист результату від дублювання, а не гарантія одного запуску CPU.
Якщо завис лише дочірній процес при живому heartbeat батьківського воркера,
відновлення чекає загального ліміту обробки плюс запасу 60 с.

PostgreSQL, dispatcher і NFS розміщені на A. Збій усього хоста A зупиняє сервіс.
Реплікація цих компонентів не входить до цього етапу.
Після перезапуску VM перевірте NFS і повторіть команди `up -d --no-build`.
Не використовуйте `down -v`, якщо потрібно зберегти базу та черги.

Точні ідентифікатори та результати перевірки наведені в `STAGE7_EVIDENCE.md`.
