# CrewAI E-commerce Demo Container
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY policies/ policies/

# Create workspace directories
RUN mkdir -p workspace/data/scraped workspace/data/reports workspace/data/traces

# Default command
CMD ["python", "main.py", "--products", "laptop,monitor,keyboard"]
