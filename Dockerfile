# CrewAI E-commerce Demo Container
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for Playwright (comprehensive list for Debian/Ubuntu)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    gnupg \
    # Playwright browser dependencies
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
    # Additional dependencies often needed
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxext6 \
    libxshmfence1 \
    libglib2.0-0 \
    libfontconfig1 \
    fonts-liberation \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers - install deps separately for better error handling
RUN playwright install-deps chromium || true
RUN playwright install chromium

# Copy application code
COPY main.py .
COPY policies/ policies/

# Create workspace directories
RUN mkdir -p workspace/data/scraped workspace/data/reports workspace/data/traces

# Default command
CMD ["python", "main.py", "--products", "laptop,monitor,keyboard"]
