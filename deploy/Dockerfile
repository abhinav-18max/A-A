ARG BASE_IMAGE=ubuntu:24.04
ARG NODE_IMAGE=node:22-bookworm-slim
FROM ${NODE_IMAGE} AS browser-build
WORKDIR /work/browser-worker
COPY browser-worker/package*.json ./
RUN npm ci
COPY browser-worker/src ./src
COPY browser-worker/tsconfig.json ./
RUN npm run build

FROM ${BASE_IMAGE} AS media-build
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates build-essential pkg-config libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
RUN curl --fail --location https://sh.rustup.rs --output /tmp/rustup.sh \
    && sh /tmp/rustup.sh -y --profile minimal --default-toolchain 1.98.1
ENV PATH=/root/.cargo/bin:$PATH
WORKDIR /work
COPY proto ./proto
COPY media-core ./media-core
RUN cargo build --release --locked --manifest-path media-core/Cargo.toml

FROM ${BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/browsers PATH=/opt/venv/bin:$PATH \
    NODE_PATH=/opt/mba-node/node_modules MBA_MEDIA_BINARY=/usr/local/bin/mba-media
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv gstreamer1.0-tools gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav gstreamer1.0-pulseaudio gstreamer1.0-x pulseaudio-utils pulseaudio \
    xvfb x11-utils openbox v4l-utils ffmpeg ca-certificates libnss-wrapper \
    && python3 -m venv /opt/venv && rm -rf /var/lib/apt/lists/*
COPY --from=browser-build /usr/local/bin/node /usr/local/bin/node
COPY --from=browser-build /work/browser-worker/node_modules/playwright /opt/mba-node/node_modules/playwright
COPY --from=browser-build /work/browser-worker/node_modules/playwright-core /opt/mba-node/node_modules/playwright-core
WORKDIR /opt/mba
COPY deploy/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && node /opt/mba-node/node_modules/playwright/cli.js install --with-deps chromium \
    && chmod -R a+rX /opt/browsers \
    && dpkg-query -W > /opt/mba/os-packages.txt && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=browser-build /work/src/mba/worker/main.cjs ./src/mba/worker/main.cjs
RUN pip install --no-deps .
COPY deploy/native_preflight.py ./native_preflight.py
COPY deploy/entrypoint.sh ./entrypoint.sh
COPY --from=media-build /work/media-core/target/release/mba-media /usr/local/bin/mba-media
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix \
    && python native_preflight.py --plugins-only
EXPOSE 8080
ENTRYPOINT ["sh", "/opt/mba/entrypoint.sh"]
