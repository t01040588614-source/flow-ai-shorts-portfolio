# Build context is the REPO ROOT (see docker-compose.yml: api.build.context: .,
# dockerfile: backend/Dockerfile) — every COPY below is relative to the repo
# root, not this file's directory. That is required because timeline export
# needs one file from ../frontend, which plain `docker build ./backend` cannot
# reach (Docker forbids COPY from outside the build context).
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production

# - curl: HEALTHCHECK
# - nodejs: runs frontend/scripts/renderTimeline.mjs for timeline export. That
#   script is pure Node stdlib (child_process/fs/path/url) plus a spawned
#   ffmpeg — it needs a `node` binary, nothing from npm. Without Node here,
#   POST /api/timeline/render always failed inside this image (frontend_dir
#   resolves to /frontend, which never existed).
# - ffmpeg (apt, not just the Python venv's imageio-ffmpeg): the Node renderer
#   burns subtitle/text clips with ffmpeg's `drawtext` filter, which the
#   imageio-ffmpeg-bundled Linux binary does NOT include (confirmed by
#   actually running a render: "No such filter: 'drawtext'"). Debian's ffmpeg
#   package is built with drawtext/fontconfig support. See
#   render_service.py's _node_renderer_ffmpeg_binary — only the Node child
#   process env is repointed at this binary; the Python-side shorts pipeline
#   keeps using imageio-ffmpeg (it never calls drawtext).
# - fonts-noto-cjk: Korean text (shorts titles/keywords, timeline subtitles)
#   has no glyphs on Linux otherwise — see app/services/font_resolver.py and
#   the mirrored candidate list in renderTimeline.mjs. Windows dev boxes never
#   hit this because Malgun Gothic ships with Windows.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg ffmpeg fonts-noto-cjk \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY backend/constraints.txt backend/requirements.txt backend/requirements-mediapipe.txt ./
RUN pip install --no-cache-dir -c constraints.txt -r requirements.txt \
    && pip install --no-cache-dir --no-deps mediapipe==1.1.1 \
    && pip install --no-cache-dir -r requirements-mediapipe.txt

COPY backend/scripts/verify_opencv_install.py ./scripts/
COPY backend/app ./app
# Root .dockerignore excludes backend/data/*.db — only style_presets.json
# (curated seed config) is copied. The *.db files are the developer's real
# local SQLite state (accounts, chat history, render jobs); baking them in
# would ship that into the image, and docker-compose.yml's named volumes
# initialize themselves FROM the image's /app/data on first run, so it would
# have landed in "production" too. A fresh empty schema is created
# automatically on first boot — see AuthStore._init_db and friends.
COPY backend/data ./data
# Optional: drop a font at backend/assets/fonts/NotoSansKR-Bold.ttf before
# building to override the apt-installed Noto CJK fallback (font_resolver.py
# always prefers this path first). Safe to copy even when it's just .gitkeep.
COPY backend/assets ./assets

# See the nodejs comment above — this is the ONLY frontend file the API image
# needs; do not add node_modules/npm ci here, the script has zero deps.
COPY frontend/scripts/renderTimeline.mjs /frontend/scripts/renderTimeline.mjs
# Subtitle template fonts (OFL). renderTimeline.mjs looks in ../public/fonts next to itself.
COPY frontend/public/fonts /frontend/public/fonts

# Run as an unprivileged user, so a bug in file handling cannot reach the rest of the
# container. Named volumes (data, outputs, cache) take this ownership when Docker first
# creates them from these folders. A volume made by an older root image needs one
# `chown` — see docs/DEPLOY.md.
RUN mkdir -p assets/fonts cache outputs data     && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin flow     && chown -R flow:flow /app
USER flow

# Commit this image was built from (shown by /api/health and the page footer).
# scripts/deploy.sh passes it; the image has no .git to ask. Kept last so it doesn't bust the cache.
ARG FLOW_GIT_COMMIT=
ENV FLOW_GIT_COMMIT=${FLOW_GIT_COMMIT}

EXPOSE 8800

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8800/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8800"]
