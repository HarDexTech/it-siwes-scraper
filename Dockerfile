# Website only (Render). Collectors run in GitHub Actions / on your PC.
FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt
COPY app ./app
COPY core ./core
COPY config ./config
COPY db ./db
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
