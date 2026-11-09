# Дослідження обмеженого автоматичного запуску

Реалізовано операторський експеримент із заздалегідь налаштованим резервним
вузлом node-3. Кожний додатковий вузол має власний Redis і чергу. Скрипт
`deploy/autoscale.py` працює на хості з Docker CLI, окремо від Django.
Вебконтейнер не отримує Docker socket чи права запускати контейнери.

Алгоритм: прочитати `capacity_snapshot`; перевірити формат; переконатися, що
node-1 і node-2 доступні й кожен виконує хоча б одну задачу, а enqueued-черга
непорожня; запустити тільки `redis3` і `worker3`, якщо worker3 ще не працює.
Звичайні завантаження, для яких не натиснули запуск, не створюють навантаження.
У backlog враховуються і вже призначені queued-задачі.

Межа цього експерименту — 3 вузли. `--max-nodes 2` забороняє новий запуск,
але не вимикає вже запущений вручну третій вузол. Підтримується лише реєстр
node-1/node-2/node-3; розширення пулу вимагає явних конфігурацій нових сервісів
і зміни дозволеного пулу. Це обмежений прототип, не універсальний кластерний контролер.

Типово dry-run. `--apply` дозволяє дію. Один controller на робочу копію
забезпечується файловим lock у `.superpowers/autoscale.lock`. Для запуску
скрипта Python 3 достатньо стандартної бібліотеки. Помилка Docker/телеметрії
зупиняє перевірку без наступних спроб мутації; сирий stderr із секретами не друкується.

## Дві VM: резервний контейнер на координаторі

На A додайте в приватний `.env.coordinator` випадковий hex-пароль
`EXTRA_REDIS_PASSWORD`. В однаковому `WORKER_NODES` обох VM додайте:

```json
{"name":"node-3","broker_url":"redis://:REPLACE_WITH_EXTRA_PASSWORD@10.211.55.3:6381/0","queue":"subtitles"}
```

Інші записи зберігайте. Після зміни registry перестворіть web/dispatcher/воркери.
Використовуйте однаковий набір Compose-файлів, особливо якщо ввімкнено HTTPS.
Початковий запуск двох вузлів на A, без резервного:

```sh
docker compose --env-file .env.coordinator -f compose.coordinator.yaml -f compose.coordinator.third.yaml -f compose.https.yaml up -d --no-build postgres redis web dispatcher worker proxy
```

На B повторіть звичайний `up -d --no-build` зі своїм env/Compose.
Один dry-run на A з лімітом два вузли:

```sh
python3 deploy/autoscale.py --env-file .env.coordinator -f compose.coordinator.yaml -f compose.coordinator.third.yaml -f compose.https.yaml --max-nodes 2 --once
```

Без HTTPS приберіть `-f compose.https.yaml` в усіх командах цієї інструкції.
Спостереження з реально дозволеним запуском:

```sh
python3 deploy/autoscale.py --env-file .env.coordinator -f compose.coordinator.yaml -f compose.coordinator.third.yaml -f compose.https.yaml --max-nodes 3 --apply
```

Запустіть два довших відео та ще одне коротке. Зафіксуйте `started-node-3`,
heartbeat нового вузла, потім подайте нову задачу іншим користувачем, якщо
поточний уже досяг ліміту трьох задач. Вона має отримати node-3, поки базові
вузли зайняті. Вже опубліковані черги здорових вузлів не переносяться: додаткова
потужність обслуговує нові/ще не призначені задачі. Повторний цикл поверне
`at-limit`. Зупинка controller: Ctrl+C.

Після завершення всіх задач за потреби вручну зупиніть резервний вузол:

```sh
docker compose --env-file .env.coordinator -f compose.coordinator.yaml -f compose.coordinator.third.yaml -f compose.https.yaml stop worker3 redis3
```

Автоматичного зменшення немає: воно потребувало б drain-протоколу для вже
призначених задач. Ліміт задає число вузлів, не гарантує додаткових фізичних CPU:
worker1 і worker3 на одній VM ділять її ресурси.

## Один комп'ютер

Та сама схема працює з `compose.yaml` + `compose.third.yaml`. У `.env` задайте
три вузли з Redis redis1/redis2/redis3. Спочатку запускайте лише базові сервіси:

```sh
docker compose -f compose.yaml -f compose.third.yaml up -d --build postgres redis1 redis2 web dispatcher worker1 worker2
python3 deploy/autoscale.py -f compose.yaml -f compose.third.yaml --apply
```

## Висновок щодо VM

Автоматичне створення нових VM потребує API гіпервізора, шаблонного диска,
мережевих адрес, provisioning, доступу до PostgreSQL/NFS і контролю лімітів CPU/RAM.
Це можливий наступний експеримент. У цьому проєкті автоматизовано запуск
підготовленої пари контейнерів; окремі Ubuntu/Kali VM залишаються створеними
заздалегідь. Реальні результати досліду збережено в `STAGE8_EVIDENCE.md`.
