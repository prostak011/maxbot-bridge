#!/usr/bin/with-contenv bashio
# ============================================================
# MaxBot Bridge — запуск
# Конфигурацию аддона читает сам Python из /data/options.json
# ============================================================
bashio::log.info "Запуск MaxBot Bridge..."
cd /usr/src/app
exec python3 -u -m app.main
