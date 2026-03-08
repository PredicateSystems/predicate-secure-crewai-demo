# CrewAI E-commerce Demo Container
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    # Playwright dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (chromium only for smaller image)
RUN playwright install chromium --with-deps

# Copy application code
COPY main.py .
COPY policies/ policies/

# Create workspace directories
RUN mkdir -p workspace/data/scraped workspace/data/reports workspace/data/traces

# Default command
CMD ["python", "main.py", "--products", "laptop,monitor,keyboard"]
