FROM python:3.14-slim
ENV PORT=8080
RUN useradd -r -u 10001 app
WORKDIR /site
COPY index.html style.css ./
USER app
EXPOSE 8080
CMD ["sh", "-c", "python -m http.server ${PORT} --bind 0.0.0.0 --directory /site"]
