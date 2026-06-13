FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg2 curl ca-certificates \
    xvfb xauth python3-tk libtk8.6 \
    fonts-liberation fonts-noto-cjk fonts-wqy-zenhei \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libgbm1 libasound2 \
    libpango-1.0-0 libpangocairo-1.0-0 \
    libcairo2 libgdk-pixbuf-2.0-0 \
    libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libxshmfence1 libx11-xcb1 \
    libxext6 libxrender1 libxss1 libx11-6 \
    libexpat1 libglib2.0-0 libharfbuzz0b \
    dbus nodejs npm && \
    rm -rf /var/lib/apt/lists/*

RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | \
    gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > \
    /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends google-chrome-stable && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /var/run/dbus /tmp/.X11-unix && \
    chmod 1777 /tmp/.X11-unix

WORKDIR /app

COPY browser-service/requirements.txt /tmp/browser-requirements.txt
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/browser-requirements.txt -r /tmp/requirements.txt && \
    mkdir -p /usr/local/lib/python3.12/site-packages/seleniumbase/drivers && \
    chmod -R 777 /usr/local/lib/python3.12/site-packages/seleniumbase/drivers && \
    seleniumbase install chromedriver && \
    playwright install chromium --with-deps && \
    npm install -g @playwright/mcp@latest

COPY scripts/pre_download_uc_driver.py /tmp/pre_download_uc_driver.py
RUN mkdir -p /app/logs /app/data /app/workspace && \
    Xvfb :99 -screen 0 1920x1080x24 & sleep 1 && \
    DISPLAY=:99 python3 /tmp/pre_download_uc_driver.py && \
    kill %1 2>/dev/null ; true

COPY browser-service/ /app/browser-service/
COPY src/ /app/src/
COPY config/ /app/config/
COPY scripts/ /app/scripts/
COPY data/ /app/data/
COPY templates/ /app/templates/

RUN mkdir -p /app/scrapers /app/logs /app/workspace /app/data/akamai-cookies

EXPOSE 8001 8111 9222 9223

CMD ["sh", "-c", "\
    rm -f /tmp/.X98-lock /tmp/.X11-unix/X98 && \
    LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH} \
    PYTHONPATH=/app exec uvicorn browser-service.server:app --host 0.0.0.0 --port 8001 --workers 1 --log-level info \
"]
