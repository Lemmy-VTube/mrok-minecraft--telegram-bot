FROM python:3.13.7

ADD https://astral.sh/uv/install.sh /uv-installer.sh

RUN sh /uv-installer.sh && rm /uv-installer.sh

ENV PATH="/root/.local/bin/:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock README.md /app/

RUN uv sync --locked

COPY . /app/.

CMD [ "uv", "run", "main.py" ] 