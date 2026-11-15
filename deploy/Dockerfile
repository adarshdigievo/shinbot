FROM node:22.23.1-bookworm-slim AS assets
WORKDIR /build/runtime
COPY runtime/package*.json ./
RUN npm ci --ignore-scripts
COPY runtime/tsconfig.json ./
COPY runtime/src ./src
RUN npm run build
WORKDIR /build
COPY scripts/copy_web_assets.mjs scripts/
RUN node scripts/copy_web_assets.mjs
RUN npm --prefix /build/runtime prune --omit=dev --ignore-scripts

FROM node:22.23.1-bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends util-linux ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=assets /build/runtime/package*.json ./
COPY --from=assets /build/runtime/node_modules ./node_modules
COPY --from=assets /build/runtime/dist ./dist
RUN mkdir /data && chown node:node /data
USER node
ENV SHINBOT_RUNTIME_DATA=/data
# -F keeps node as the process that receives the stop signal, so it can close cleanly.
CMD ["flock", "-n", "-F", "/data/runtime.lock", "node", "dist/server.js"]

FROM python:3.13.12-slim-bookworm AS python-base
COPY --from=ghcr.io/astral-sh/uv:0.11.2 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY deploy/compose.release.yaml deploy/compose.release.yaml
ENV UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN uv venv /opt/venv

FROM python-base AS control
RUN uv sync --frozen --no-install-project --no-dev --extra security && groupadd -g 1000 shinbot && useradd -u 1000 -g shinbot -m shinbot && mkdir /data && chown shinbot:shinbot /data
COPY src ./src
COPY README.md LICENSE ./
COPY --from=assets /build/src/shinbot/web/vendor ./src/shinbot/web/vendor
RUN uv sync --frozen --no-dev --extra security
USER shinbot
ENV SHINBOT_DATA=/data
CMD ["uvicorn", "shinbot.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765", "--no-access-log"]

FROM node:22.23.1-bookworm-slim AS computer-tools
WORKDIR /opt/skills
COPY computer/package*.json ./
RUN npm ci --ignore-scripts --omit=dev

FROM python-base AS computer
COPY --from=computer-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=computer-tools /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY --from=computer-tools /opt/skills /opt/skills
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
RUN uv sync --frozen --no-install-project --no-dev --extra computer && apt-get update && apt-get install -y --no-install-recommends git curl util-linux ca-certificates libstdc++6 libatomic1 && rm -rf /var/lib/apt/lists/* && groupadd -g 1001 workspace && useradd -u 1001 -g workspace -m workspace && mkdir /data /workspace && chown workspace:workspace /workspace
COPY src ./src
COPY README.md LICENSE ./
RUN uv sync --frozen --no-dev --extra computer
ENV SHINBOT_WORKSPACE=/workspace SHINBOT_EXECUTOR_DATA=/data
CMD ["uvicorn", "shinbot.services.computer:app", "--host", "0.0.0.0", "--port", "8082", "--no-access-log"]

FROM node:24.11.0-bookworm-slim AS browser-tools
WORKDIR /opt/shinbot-browser
COPY browser/package*.json ./
RUN npm ci --ignore-scripts --omit=dev && node node_modules/agent-browser/scripts/postinstall.js

FROM python-base AS browser
ENV XDG_CACHE_HOME=/opt/browser-cache PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
COPY --from=browser-tools /opt/shinbot-browser /opt/venv/share/shinbot/browser
RUN uv sync --frozen --no-install-project --no-dev --extra browser && playwright install --with-deps chromium && apt-get update && apt-get install -y --no-install-recommends xvfb tigervnc-scraping-server tigervnc-tools websockify && rm -rf /var/lib/apt/lists/* && groupadd -g 1000 browser && useradd -u 1000 -g browser -m browser && mkdir /data && chown browser:browser /data && chmod -R a+rX /opt/playwright
RUN playwright install-deps firefox && python -m camoufox fetch official/stable/156.0.1-beta.36 && chmod -R a+rX /opt/browser-cache && chown -R browser:browser /opt/browser-cache
COPY src ./src
COPY README.md LICENSE ./
RUN uv sync --frozen --no-dev --extra browser
COPY deploy/browser-start.sh /usr/local/bin/browser-start
RUN chmod 755 /usr/local/bin/browser-start
USER browser
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright DISPLAY=:99 SHINBOT_BROWSER_DATA=/data
CMD ["browser-start"]

FROM browser AS desktop
USER root
COPY --from=computer-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=computer-tools /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY --from=computer-tools /opt/skills /opt/skills
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
RUN apt-get update && apt-get install -y --no-install-recommends xfce4-session xfwm4 xfdesktop4 xfce4-panel xfce4-settings xfce4-terminal thunar dbus-x11 xdotool scrot git curl util-linux && rm -rf /var/lib/apt/lists/* && groupadd -g 1001 workspace && useradd -u 1001 -g workspace -m workspace && mkdir -p /workspace
RUN uv sync --frozen --no-dev --extra browser --extra computer
COPY deploy/desktop-browser.sh /usr/local/bin/shinbot-desktop-browser
COPY deploy/open-browser.sh /usr/local/bin/shinbot-open-browser
COPY deploy/shinbot-browser.desktop /usr/share/applications/shinbot-browser.desktop
COPY deploy/shinbot-browser-helper.desktop /usr/share/xfce4/helpers/shinbot-browser.desktop
RUN chmod 755 /usr/local/bin/shinbot-desktop-browser /usr/local/bin/shinbot-open-browser && sed -i 's/^WebBrowser=.*/WebBrowser=shinbot-browser/' /etc/xdg/xfce4/helpers.rc && printf '[Default Applications]\nx-scheme-handler/http=shinbot-browser.desktop\nx-scheme-handler/https=shinbot-browser.desktop\n' > /etc/xdg/mimeapps.list && update-alternatives --install /usr/bin/x-www-browser x-www-browser /usr/local/bin/shinbot-open-browser 100
ENV SHINBOT_DESKTOP=1 SHINBOT_DESKTOP_STATE=/run/shinbot-desktop SHINBOT_BROWSER_DATA=/data/browser SHINBOT_EXECUTOR_DATA=/data/computer SHINBOT_WORKSPACE=/workspace
CMD ["python", "-m", "shinbot.services.desktop_runner"]
