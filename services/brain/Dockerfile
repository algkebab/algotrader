FROM python:3.11-slim

WORKDIR /app

COPY services/brain/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared /app/shared
COPY services/brain/ .

CMD ["python", "main.py"]
