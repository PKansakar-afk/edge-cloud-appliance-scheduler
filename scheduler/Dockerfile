FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY scheduler/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common common
COPY scheduler scheduler
CMD ["python", "-m", "scheduler.service"]