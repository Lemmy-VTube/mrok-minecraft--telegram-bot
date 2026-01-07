FROM python:3.13.7

# Устанавливаем системные зависимости для работы с systemd и Java
RUN apt-get update && apt-get install -y \
    systemd \
    dbus \
    procps \
    sudo \
    openjdk-17-jre-headless \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем uv
ADD https://astral.sh/uv/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh
ENV PATH="/root/.local/bin/:$PATH"

WORKDIR /app

# Копируем файлы зависимостей
COPY pyproject.toml uv.lock README.md /app/

# Устанавливаем зависимости
RUN uv sync --locked

# Создаем необходимые директории
RUN mkdir -p /app/logs /app/backups

# Копируем исходный код
COPY . /app/.

# Устанавливаем переменные окружения
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Команда запуска
CMD [ "uv", "run", "main.py" ] 