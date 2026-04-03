FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc curl && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# #60/#83: Create data directory for DB volume mount
RUN mkdir -p /app/data

# Expose dashboard port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/api/health || exit 1

# Mount volume for DB persistence: docker run -v tradingtest_data:/app/data ...
VOLUME ["/app/data"]

# Run both bot + dashboard
CMD ["python", "-m", "src.main"]
