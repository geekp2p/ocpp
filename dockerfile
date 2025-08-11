FROM python:3.11-slim

# Install required system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the source code
COPY server.py .

# Expose ports for FastAPI (8000) and OCPP WebSocket (9000)
EXPOSE 8000 9000

# Run the app
CMD ["python", "server.py"]
