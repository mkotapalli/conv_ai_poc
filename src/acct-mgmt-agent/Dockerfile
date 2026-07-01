FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
ENV CONFIG_FILE=/app/config/application.properties
EXPOSE 8080
EXPOSE 8000

CMD ["python", "dual_server.py"]
