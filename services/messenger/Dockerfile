FROM python:3.11-slim

WORKDIR /app

COPY services/messenger/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared /app/shared
COPY services/messenger/ .

CMD ["python", "main.py"]
