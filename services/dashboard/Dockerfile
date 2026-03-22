FROM python:3.11-slim

WORKDIR /app

COPY services/dashboard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared /app/shared
COPY scripts /app/scripts
COPY services/dashboard/ .

CMD ["python", "main.py"]
