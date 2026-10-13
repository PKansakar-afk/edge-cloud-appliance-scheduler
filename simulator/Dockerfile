FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY simulator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common common
COPY eval/build_households.py eval/build_households.py
COPY simulator simulator
COPY data/processed/jobs.csv data/processed/jobs.csv
CMD ["python", "-m", "simulator.simulator"]