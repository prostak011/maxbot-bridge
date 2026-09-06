ARG BUILD_FROM=ghcr.io/home-assistant/base-python:latest
FROM ${BUILD_FROM}

# Зависимости Python (wheels, без компиляции)
WORKDIR /usr/src/app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Код аддона
COPY app/ app/
COPY run.sh /
RUN chmod a+x /run.sh

LABEL \
  io.hass.version="VERSION" \
  io.hass.type="app" \
  io.hass.arch="aarch64|amd64"

CMD [ "/run.sh" ]
