FROM python:3.11-slim

WORKDIR /app

COPY services/risk-manager/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared /app/shared
COPY services/risk-manager/ .

CMD ["python", "main.py"]
