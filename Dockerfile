FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN python -m pip install --no-cache-dir -r requirements.txt --disable-pip-version-check --no-warn-script-location

COPY . /app

CMD ["python", "main.py"]
