#!/bin/zsh
cd "${0:A:h:h}" || exit 1
if [[ ! -f ".venv/bin/python" ]]; then
  echo 'Install packages first: python3 -m venv .venv && .venv/bin/python -m pip install -r mac/requirements.txt'
  exit 1
fi
./.venv/bin/python mac/mac84_bridge.py --port auto --pace .003
