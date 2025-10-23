FROM python:3.11-slim

# OS deps required by Chromium
RUN apt-get update && apt-get install -y \
  libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libgbm1 \
  libasound2 libxkbcommon0 libgtk-3-0 libx11-xcb1 libxcomposite1 libxdamage1 \
  libxfixes3 libxrandr2 libxshmfence1 libxrender1 libxtst6 libxcb1 ca-certificates \
  fonts-liberation wget gnupg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Install Chromium for Playwright
RUN python -m playwright install chromium

COPY . .
ENV PORT=8000
CMD uvicorn main:app --host 0.0.0.0 --port $PORT
