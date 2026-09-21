FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data COOKIE_SECURE=true PORT=8080
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY LICENSE THIRD_PARTY_NOTICES.md ./
RUN mkdir -p /data && chmod 700 /data
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips='*' --no-access-log"]
