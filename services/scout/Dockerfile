FROM python:3.11-slim

WORKDIR /app

COPY services/scout/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared /app/shared
COPY services/scout/ .

CMD ["python", "main.py"]
