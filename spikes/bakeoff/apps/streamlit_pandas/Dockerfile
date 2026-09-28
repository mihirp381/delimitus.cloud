FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
RUN pip install --no-cache-dir streamlit==1.64.0 pandas==3.0.6
RUN useradd -r -u 10001 app
WORKDIR /app
COPY probes.py app.py ./
USER app
EXPOSE 8080
CMD ["sh", "-c", "streamlit run app.py --server.port ${PORT} --server.address 0.0.0.0 --server.headless true --server.enableXsrfProtection ${XSRF:-true} --server.enableCORS ${CORS:-true} --browser.gatherUsageStats false"]
