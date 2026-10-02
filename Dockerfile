FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ src/
COPY data/ data/
COPY demo.py .
ENV MOCK_LLM=0 PYTHONUNBUFFERED=1
EXPOSE 8000
