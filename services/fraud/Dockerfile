FROM python:3.12-slim

WORKDIR /app

COPY services/fraud/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/fraud/ .

EXPOSE 8000

CMD ["uvicorn", "archisynapse_fraud_mvp:app", "--host", "0.0.0.0", "--port", "8000"]