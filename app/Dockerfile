FROM python:3.12-alpine

RUN adduser -D -u 1000 appuser

WORKDIR /app
COPY main.py .

USER 1000
EXPOSE 8080

ENTRYPOINT ["python3", "main.py"]
