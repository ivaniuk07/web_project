# Етап 8 та додаткові бали — фактична перевірка

Репетиція: 08.11.2026 19:02:25–19:02:58, Europe/Kyiv (UTC+2).
База коду: Git `0851a9679839d66e2c819fedb29d5a839da7adc1` зі змінами етапу 8.
Зміни не комітилися й не пушилися без окремого дозволу.
Сценарій: `deploy/rehearse.py`, реальні HTTP-запити через Caddy, без mock
Redis/Celery/Whisper. Користувачі й демонстраційні задачі залишені у БД.

## Перевірена конфігурація

| Роль | Середовище | Сервіси |
| --- | --- | --- |
| A | Ubuntu 26.04 ARM64, 10.211.55.3 | Django, PostgreSQL 17, dispatcher, Redis1/node-1, NFS, Caddy 2.11.9 |
| B | Kali ARM64, 10.211.55.5 | Redis2/node-2, NFS-клієнт |
| Резерв | контейнер на A | Окремий Redis3 (6381) і node-3, підняті контролером |

Whisper tiny на CPU, OPENBLAS_CORETYPE=ARMV8; Celery concurrency=1 на вузол.
Порівняно реальний код у контейнерах A/B: однакова SHA-256
`c2688f871e02a4b55ecf438c79aea6800a4e15e833ab65701b18ae5a24dc0ccb`. Алгоритм fingerprint описаний у звіті етапу 7.
Python-пакети: Django 5.2.17, celery 5.6.3, faster-whisper 1.2.1, ctranslate2 4.8.5, numpy 2.5.4, av 16.1.0.
Файли спільні через NFS /srv/subtitles/media → /app/media.

## Основний сценарій

- HTTPS-сторінка входу повернула 200 з перевіркою CA, без -k чи вимкнення verification.
- Вхід без CSRF: 403. Коректний вхід: успішний, session cookie має Secure.
- Реєстрація другого користувача через HTTP-форму: успішна.
- /operations/ для staff: 200; для звичайного користувача: 403.
- Завантажено два коротких ролики з мовленням. Зафіксовано одночасні RUNNING
  на node-1 і node-2. Потім SUCCEEDED, перегляд результатів і завантаження MP4/SRT.
- Обидві задачі збереглися в історії. Інший користувач отримав 404 за їхніми URL.
- POST logout завершив сесію; наступний запит історії перенаправив на вхід.

| Job UUID | Вузол | Стан |
| --- | --- | --- |
| 774b0178-f82e-44e7-b31a-fafa6fdcda8e | node-1 | succeeded |
| f23feaad-136c-4b8e-9901-4e2f72b7469c | node-2 | succeeded |
| c5444aba-59f4-4ad1-b7f1-ef27cc4a02f4 | node-3 | succeeded |

SRT усіх трьох задач має текст і часові позначки. FFprobe підтвердив MP4
із відеопотоком, тривалістю 4.133016 с; SRT кожного — 98 символів.
Джерелом був один і той самий тестовий ролик, тому хеші результатів можуть збігатися.

## Ліміти, помилки й скасування

| Перевірка | Результат |
| --- | --- |
| Пошкоджений broken.mp4 | Відхилено формою до створення задачі |
| Справжній ролик із розширенням .txt | Відхилено |
| Валідний ролик понад 600 с | Відхилено |
| 104857601 байт при ліміті 104857600 | Відхилено |
| Три активні upload, четвертий | Відхилено, перші три скасовано |
| Задача вже RUNNING | Скасовано, MP4/SRT endpoints повертають 404 |
| Задача опублікована в Redis, поки обидва воркери зайняті | QUEUED → CANCELLED, готовий MP4 недоступний |

Скасування running: `62eecc29-2f4f-4fb2-9da1-7fdb971adc5e`.
Скасування опублікованої queued: `64d761e9-2a41-45e3-952f-ef84f722d43d`.
Зіставлення записів після репетиції підтвердило finished_at для всіх
термінальних задач; успішні спроби мають started_at ≤ finished_at.

## Приблизний час очікування

При двох RUNNING і ще одній запущеній queued-задачі `c2155bac-2279-4965-a582-3f1cfeb47733`
сторінка показала «приблизно 6 с» до початку.
Оцінка використовує облік часу, останні 50 успішних вимірювань за вузлами,
черги та ту саму політику count/LRU, що й балансувальник. Різна тривалість
роликів і поточне навантаження можуть суттєво змінити очікування. Це прогноз,
не точний таймер. Без вибірки береться fallback 60 с; без живих вузлів оцінки немає.

## Реальний автозапуск і межа

Під навантаженням з двома доступними зайнятими вузлами виконано host-side controller:

```text
{"decision": "at-limit", "max_nodes": 2}
{"decision": "would-start-node-3", "max_nodes": 3}
{"decision": "started-node-3", "max_nodes": 3}
THIRD_NODE_READY {"nodes": [{"name": "node-1", "available": true, "running": 1, "queued": 0, "heartbeat_at": "2026-11-08T17:02:43.194272+00:00"}, {"name": "node-2", "available": true, "running": 1, "queued": 1, "heartbeat_at": "2026-11-08T17:02:42.992950+00:00"}, {"name": "node-3", "available": true, "running": 0, "queued": 0, "heartbeat_at": "2026-11-08T17:02:43.874352+00:00"}], "waiting": 1, "pending": 0, "running": 2, "awaiting_submission": 0}
{"decision": "at-limit", "max_nodes": 3}
```

Після heartbeat node-3 нова задача `c5444aba-59f4-4ad1-b7f1-ef27cc4a02f4` реально виконана node-3.
Один і той самий код воркера, окремий Redis, спільні PostgreSQL/NFS.
max-nodes=2 заборонив запуск; dry-run нічого не створив; apply запустив лише
резервну пару; повторний цикл не створив четвертого вузла.

Контролер не встановлено як постійну системну службу. Його можна запускати
циклічно вручну за інструкцією AUTOSCALING.md. Автоматичне створення нових VM
лише досліджено: потрібні API гіпервізора/provisioning/мережа/ліміти ресурсів.
Наявний робочий експеримент запускає підготовлені контейнери з максимумом 3.

## TLS і мережевий доступ

Endpoint: https://10.211.55.3:8443/. Caddy internal CA, default_sni для IP-клієнтів,
які не надсилають SNI. Локальний CA SHA-256:
`61614a11076514ae1b324171f01b51cc551f6a6c10d6e524571ffffc256e0f7a`.

`curl --cacert root-ca.crt` повернув HTTP 200; HTTP-клієнт репетиції перевірив
сертифікат і вхід. Прямий http://10.211.55.3:8000 відхилив TCP-підключення
(curl exit 7), `docker port web` не показав жодного published port.
Django довіряє proxy-заголовку тільки з явним opt-in у TLS-overlay.
Браузеру потрібен окремий імпорт довіри до цього CA; системну довіру Mac
автоматично не змінено. Приватні ключі залишаються в Caddy-томі.

## Журнали воркерів

Час журналів — Europe/Kyiv. Celery task ID позначає спробу, а не Job UUID;
успіх конвертації додатково перевірено в PostgreSQL та файлах.

### node-1

```text
[2026-11-08 19:00:40,162: INFO/MainProcess] node-1@26f425c4fe7f ready.
[2026-11-08 19:02:25,883: INFO/MainProcess] Task subtitles.process_video[bc1d8a38-02ec-4816-b202-2833b148c1fe] received
[2026-11-08 19:02:27,333: INFO/ForkPoolWorker-1] Task subtitles.process_video[bc1d8a38-02ec-4816-b202-2833b148c1fe] succeeded in 1.4480634589999681s: None
[2026-11-08 19:02:29,161: INFO/MainProcess] Task subtitles.process_video[2defe312-efb8-4486-ad55-739c9252b861] received
[2026-11-08 19:02:29,245: INFO/ForkPoolWorker-1] Task subtitles.process_video[2defe312-efb8-4486-ad55-739c9252b861] succeeded in 0.08158466699933342s: None
[2026-11-08 19:02:30,313: INFO/MainProcess] Task subtitles.process_video[b565c289-888d-4f90-8fee-a0f7a847c3cf] received
[2026-11-08 19:02:58,323: INFO/ForkPoolWorker-1] Task subtitles.process_video[b565c289-888d-4f90-8fee-a0f7a847c3cf] succeeded in 28.009019596000144s: None
[2026-11-08 19:02:58,327: INFO/MainProcess] Task subtitles.process_video[4f8d4be4-30fe-4949-8d01-c89a7067240c] received
[2026-11-08 19:02:58,334: INFO/ForkPoolWorker-1] Task subtitles.process_video[4f8d4be4-30fe-4949-8d01-c89a7067240c] succeeded in 0.006059082999854581s: None
```

### node-2

```text
[2026-11-08 19:02:01,475: INFO/MainProcess] node-2@171d2e06ad8d ready.
[2026-11-08 19:02:25,986: INFO/MainProcess] Task subtitles.process_video[1adef069-8442-4814-8089-3903602d1256] received
[2026-11-08 19:02:27,337: INFO/ForkPoolWorker-1] Task subtitles.process_video[1adef069-8442-4814-8089-3903602d1256] succeeded in 1.3498946259996956s: None
[2026-11-08 19:02:30,242: INFO/MainProcess] Task subtitles.process_video[322d780c-a889-4423-aaae-dc5724328966] received
[2026-11-08 19:02:47,749: INFO/ForkPoolWorker-1] Task subtitles.process_video[322d780c-a889-4423-aaae-dc5724328966] succeeded in 17.506578716999684s: None
[2026-11-08 19:02:47,751: INFO/MainProcess] Task subtitles.process_video[47ead961-9acc-48e7-aaad-89177f4d1c65] received
[2026-11-08 19:02:48,900: INFO/ForkPoolWorker-1] Task subtitles.process_video[47ead961-9acc-48e7-aaad-89177f4d1c65] succeeded in 1.1490828750002038s: None
```

### node-3

```text
[2026-11-08 19:02:43,877: INFO/MainProcess] node-3@00c2ca8a0143 ready.
[2026-11-08 19:02:46,417: INFO/MainProcess] Task subtitles.process_video[84bb8e86-2369-410a-8049-04cf01c7403a] received
[2026-11-08 19:02:50,232: INFO/ForkPoolWorker-1] Task subtitles.process_video[84bb8e86-2369-410a-8049-04cf01c7403a] succeeded in 3.81314091799959s: None
```

## Контрольні суми коротких результатів

| Job UUID | SRT SHA-256 | MP4 SHA-256 |
| --- | --- | --- |
| 774b0178-f82e-44e7-b31a-fafa6fdcda8e | `80183a9ab7add07137bb66ece5bf906790b98d97cf7e829980beee7d773009e5` | `efa4183241547667fd83d536c5795a1b65c84d20c0ba417faaae536fa93e538a` |
| f23feaad-136c-4b8e-9901-4e2f72b7469c | `80183a9ab7add07137bb66ece5bf906790b98d97cf7e829980beee7d773009e5` | `efa4183241547667fd83d536c5795a1b65c84d20c0ba417faaae536fa93e538a` |
| c5444aba-59f4-4ad1-b7f1-ef27cc4a02f4 | `80183a9ab7add07137bb66ece5bf906790b98d97cf7e829980beee7d773009e5` | `efa4183241547667fd83d536c5795a1b65c84d20c0ba417faaae536fa93e538a` |

## Автоматичні перевірки й обмеження

Повний набір: 82 тести, PostgreSQL, OK. Нові тести перевірили ETA за історією,
черги/heartbeat/cooldown, права staff, відсутність секретів у телеметрії,
ліміти/idempotency/fail-closed контролера, secure cookie/CSRF/proxy opt-in,
а також точність симуляції вибору вузла і час ручного відновлення.
Міграції: makemigrations --check --dry-run — No changes detected.
Compose config --quiet пройшов на обох VM. Дві нові регресії фінального рев’ю
відтворено до виправлення, після виправлення весь набір 82/82 зелений.

Сценарій захисту: deploy/DEFENSE.md. README оновлено для одного комп'ютера і VM.
Обмеження: ETA приблизна; базові PostgreSQL/NFS на A не репліковані; прогрес
Whisper не переноситься між спробами; автозапуск лише node-3 без auto-downscale;
третій контейнер ділить CPU VM A; локальний CA; Django runserver для демонстрації.
Паролі, session cookie та CSRF-токени не входять до цього звіту або Git.
