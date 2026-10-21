## Docker

Проєкт можна запускати локально в Docker без встановлення Python-залежностей у систему.
API-ключ не вбудовується в образ: файл `.env` виключений з build context і монтується
в контейнер лише для читання.

### 1. Підготовка

Переконайтеся, що Docker Desktop запущений. Якщо `.env` ще немає:

```bash
cp -n .env.example .env
```

Заповніть у `.env` щонайменше `YOUTUBE_API_KEY` і `VIDEO_IDS`.

### 2. Збір образу

```bash
docker compose build
```

### 3. Запуск Dashboard

```bash
docker compose up -d dashboard
```

Dashboard буде доступний за адресою:

```text
http://127.0.0.1:8501
```

Перевірити стан:

```bash
docker compose ps
```

Подивитися логи:

```bash
docker compose logs -f dashboard
```

### 4. Запуск інкрементного збору

Collector є одноразовою командою: після завершення збору контейнер завершує роботу.

```bash
docker compose run --rm collector
```

Для історичного backfill:

```bash
docker compose run --rm collector   python -m youtube_collector.main --mode backfill
```

### 5. Тести в контейнері

```bash
docker compose run --rm tests
```

### 6. Збереження даних

Каталоги `./data` і `./logs` монтуються з хоста, тому SQLite-база та журнал
зберігаються після видалення контейнера. Dashboard і collector використовують
ті самі локальні дані.

### 7. Зупинка

```bash
docker compose down
```

Для повної перебудови образу після зміни залежностей:

```bash
docker compose build --no-cache
```

### Docker-структура

- `Dockerfile` — спільний образ застосунку.
- `compose.yaml` — окремі сервіси `dashboard`, `collector` і `tests`.
- `.dockerignore` — виключає секрети, локальне virtualenv, SQLite та журнали з образу.
- `.env` не копіюється в образ і монтується як read-only.
- Порт Streamlit публікується лише на `127.0.0.1:8501`.
