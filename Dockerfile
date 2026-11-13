FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "psycopg[binary]>=3.1"
COPY . .
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "juvelcall", "serve"]
