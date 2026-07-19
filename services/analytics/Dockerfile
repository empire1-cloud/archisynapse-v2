FROM python:3.12-slim

WORKDIR /app

COPY services/analytics/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/analytics/ .

EXPOSE 8081

CMD ["uvicorn", "revenue_intelligence:app", "--host", "0.0.0.0", "--port", "8081"]
