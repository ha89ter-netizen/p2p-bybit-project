#!/bin/bash
# Панель наблюдения. Только localhost — снаружи машины недоступна.
cd "$(dirname "$0")"
if curl -s -o /dev/null -m 2 http://127.0.0.1:8787/; then
  echo "панель уже работает"
else
  nohup python3 -m dashboard.server > dashboard.log 2>&1 < /dev/null &
  disown; sleep 2
fi
open http://127.0.0.1:8787/
