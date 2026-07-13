#!/bin/sh
set -e

case "${1:-serve}" in
  serve)
    exec uvicorn sentinel.server:app --host 0.0.0.0 --port 8080
    ;;
  doctor)
    exec python -m sentinel.doctor
    ;;
  *)
    exec "$@"
    ;;
esac
