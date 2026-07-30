FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent_commerce ./agent_commerce
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "agent_commerce.api:app", "--host", "0.0.0.0", "--port", "8090"]
