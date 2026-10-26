FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
COPY benchmarks/requirements.txt /app/benchmarks/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r benchmarks/requirements.txt
COPY . /app
ENV DJANGO_SETTINGS_MODULE=benchmarks.settings

