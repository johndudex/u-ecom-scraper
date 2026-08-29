FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg2 curl ca-certificates \
    tini \
    xvfb xauth python3-tk libtk8.6 \
    fonts-liberation fonts-noto-cjk fonts-wqy-zenhei \
    fonts-noto-color-emoji fonts-freefont-ttf fonts-unifont \
    fonts-ipafont-gothic fonts-tlwg-loma-otf \
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

COPY browser_service/requirements.txt /tmp/browser-requirements.txt
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/browser-requirements.txt -r /tmp/requirements.txt && \
    mkdir -p /usr/local/lib/python3.12/site-packages/seleniumbase/drivers && \
    chmod -R 777 /usr/local/lib/python3.12/site-packages/seleniumbase/drivers && \
    seleniumbase install chromedriver && \
    playwright install chromium --with-deps && \
    npm install -g @playwright/mcp@latest && \
    python -m cloakbrowser install || echo "cloak binary download deferred to first launch"

# pre_download_uc_driver.py was removed (UC-driver pre-download is obsolete after
# the cloakbrowser stealth migration). Keep the dirs the rest of the image expects.
RUN mkdir -p /app/logs /app/data /app/workspace

COPY browser_service/ /app/browser_service/
COPY src/ /app/src/
COPY config/ /app/config/
COPY scripts/ /app/scripts/
COPY data/ /app/data/
COPY templates/ /app/templates/

# CloakBrowser stealth safety net: a .pth that, when STEALTH_BROWSER=cloak is set,
# wraps playwright.BrowserType.launch to drive cloak's stealth Chromium binary.
# Lives in site-packages so it loads at every python startup (incl. generated
# scrapers), independent of code generation.
COPY browser_service/cloak_stealth_patch.py /usr/local/lib/python3.12/site-packages/cloak_stealth_patch.py
RUN printf 'import cloak_stealth_patch\n' > /usr/local/lib/python3.12/site-packages/cloak_stealth.pth

RUN mkdir -p /app/scrapers /app/logs /app/workspace /app/data/akamai-cookies

EXPOSE 8001 8111 9222 9223

# tini is PID 1: reaps orphaned grandchildren (killed launchers' Chrome trees —
# uvicorn as PID 1 never reaped them) and forwards signals. The preamble's
# ulimit/exports are inherited by tini and everything it supervises.
CMD ["sh", "-c", "\
    rm -rf /tmp/.X98-lock /tmp/.X11-unix/X98 && \
    rm -rf /tmp/chrome-profiles/mcp/SingletonLock /tmp/chrome-profiles/mcp/SingletonSocket /tmp/chrome-profiles/mcp/SingletonCookie && \
    rm -rf /tmp/chrome-profiles/scraper/SingletonLock /tmp/chrome-profiles/scraper/SingletonSocket /tmp/chrome-profiles/scraper/SingletonCookie && \
    rm -f /app/core.* /core.* && \
    ulimit -c 0 && \
    export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH} && \
    export PYTHONPATH=/app && \
    exec tini -- uvicorn browser_service.server:app --host 0.0.0.0 --port 8001 --workers 1 --log-level info --no-access-log \
"]
