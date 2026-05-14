FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y git docker.io && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data /tmp/ci-builds

EXPOSE 8008

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8008", "--log-level", "info"]
