# Розгортання на Ubuntu VM та Mac

| Хост | Сервіси | Приклад IP у мережі Parallels |
| --- | --- | --- |
| A, Ubuntu VM | Django, диспетчер, PostgreSQL, Redis 1, node-1, NFS і Caddy | `10.211.55.3` |
| B, Mac | Redis 2 і node-2 у Docker Desktop | `10.211.55.2` |

У Redis передаються UUID завдання, шлях до файлу й токен спроби.
Вебконтейнер та обидва воркери бачать спільні файли в `/app/media`.
Використовуйте на обох хостах одну версію коду й актуальні IP-адреси.

## Підготовка

На Ubuntu потрібні Docker Engine, Compose v2 і NFS-сервер:

```sh
sudo apt-get install docker.io docker-compose-v2 nfs-kernel-server
sudo systemctl enable --now docker nfs-kernel-server
```

На Mac запустіть Docker Desktop. Сервіси прив'язуються до `PRIVATE_BIND_IP`.
У приватній мережі Mac потрібен доступ до PostgreSQL і NFS на Ubuntu,
а Ubuntu потрібен доступ до Redis другого вузла на Mac.

## Одне спільне сховище

На A створіть каталог із власником UID/GID 10001 — користувачем контейнера:

```sh
sudo mkdir -p /srv/subtitles/media
sudo chown 10001:10001 /srv/subtitles/media
sudo mkdir -p /etc/exports.d
```

Запишіть у `/etc/exports.d/subtitles.exports`, замінивши адресу B:

```text
/srv/subtitles/media 10.211.55.2(rw,sync,no_subtree_check,root_squash)
```

```sh
sudo exportfs -ra
sudo systemctl enable --now nfs-kernel-server
```

На Mac:

```sh
mkdir -p "$HOME/subtitles-media"
sudo mount -t nfs -o vers=4,tcp,resvport 10.211.55.3:/srv/subtitles/media "$HOME/subtitles-media"
mount | grep subtitles-media
```

У `.env.worker` вкажіть абсолютний шлях до цієї папки в `SHARED_MEDIA_PATH`.
Воркер перевіряє доступ до спільних файлів і маркер координатора під час запуску.

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

Скористайтеся [командами в README](../README.md#запуск-на-ubuntu-vm-та-mac).
На Mac спочатку запустіть Redis. На Ubuntu запустіть координатор і запишіть маркер
спільного сховища. Потім запустіть воркер на Mac і перевірте журнал.
У «Стані системи» на `/operations/` обидва вузли мають бути доступні.

Для перевірки однакових файлів виконайте `python manage.py check_deployment --job UUID`
через `exec -T web` на Ubuntu та `exec -T worker` на Mac.
Значення SHA-256 для source, subtitles та video мають збігатися.

## Відключення та повернення вузла

Завантажте два короткі відео майже одночасно. У сторінках задач перевірте `node-1`
та `node-2`, а в `docker compose ... logs worker` — прийняття й завершення задач.
Для перевірки збою можна зупинити сервіс worker на Mac, залишивши Ubuntu працювати.
Після `NODE_TIMEOUT_SECONDS` (20 с за замовчуванням) dispatcher перестає обирати B
і може перепризначити його незавершені задачі на A. Після повторного запуску воркера heartbeat
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
Після перезапуску перевірте монтування NFS і повторіть команди запуску.
Не використовуйте `down -v`, якщо потрібно зберегти базу та черги.

Точні ідентифікатори та результати перевірки наведені в `STAGE7_EVIDENCE.md`.
