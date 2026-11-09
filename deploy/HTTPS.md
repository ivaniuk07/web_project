# HTTPS для навчальної демонстрації

Схема: браузер → Caddy TLS → внутрішній Django HTTP. Потрібний Docker Compose
2.24.4 або новіший для `!reset` published ports. Використовуємо overlay
`compose.https.yaml` разом із `compose.coordinator.yaml` на VM A.

У `.env.coordinator` задайте адресу координатора й зовнішній порт:

```dotenv
HTTPS_HOST=10.211.55.3
HTTPS_PORT=8443
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,10.211.55.3
```

Потім:

```sh
docker compose --env-file .env.coordinator -f compose.coordinator.yaml -f compose.https.yaml up -d --no-build
```

Якщо користуєтеся резервним node-3, додавайте також
`-f compose.coordinator.third.yaml` між основним файлом і HTTPS-overlay та
запускайте лише потрібні сервіси, як описано в `AUTOSCALING.md`.

Сайт: `https://10.211.55.3:8443/`. Overlay прибирає published port 8000
вебконтейнера, задає Secure для session/CSRF cookie, HTTPS redirect та
`SECURE_PROXY_SSL_HEADER`. Origin враховує HTTPS, IP і порт. Caddy переписує
X-Forwarded-Proto, а прямий backend недоступний із зовнішньої мережі.

Без overlay звичайні HTTP-конфігурації зберігають свої попередні налаштування.
Не вмикайте довіру до proxy-заголовка для відкритого напряму HTTP-порту.

## Локальний CA і перевірка

Caddy видає сертифікат приватної адреси власним CA. Його кореневий сертифікат
можна експортувати на A:

```sh
docker compose --env-file .env.coordinator -f compose.coordinator.yaml -f compose.https.yaml cp proxy:/data/caddy/pki/authorities/local/root.crt ./root-ca.crt
curl --cacert ./root-ca.crt https://10.211.55.3:8443/accounts/login/
```

Передайте лише `root-ca.crt` на клієнтський комп'ютер, не приватний ключ CA.
Браузер до імпорту CA показуватиме попередження про невідому локальну установу.
Для браузерної демонстрації користувач може сам імпортувати перевірений кореневий
сертифікат до своєї в'язки ключів та дозволити довіру для SSL. Автоматично
системну довіру Mac проєкт не змінює. Для відтворюваного тестування застосовуйте
окремий CA-файл (`curl --cacert`, `rehearse.py --ca`), без вимкнення верифікації.

CA і ключі залишаються в Docker-томах caddy-data/caddy-config. Не видаляйте їх
через `down -v`, інакше з'явиться інший CA й потрібен новий імпорт сертифіката.
Публічний домен із загальнодовіреним сертифікатом потребує DNS і доступу сервера
відповідно до обраного механізму видачі. Він не потрібний для цієї приватної демонстрації.

Офіційні джерела: [локальний HTTPS Caddy](https://caddyserver.com/docs/running),
[злиття Compose](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/),
[налаштування Django](https://docs.djangoproject.com/en/5.2/ref/settings/#secure-proxy-ssl-header).
