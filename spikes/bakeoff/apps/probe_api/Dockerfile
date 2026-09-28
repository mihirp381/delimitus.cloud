FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
RUN pip install --no-cache-dir fastapi==0.141.1 uvicorn==0.54.0 websockets==17.1
RUN useradd -r -u 10001 app
WORKDIR /app
COPY probes.py main.py ./
USER app
EXPOSE 8080
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --ws websockets"]
