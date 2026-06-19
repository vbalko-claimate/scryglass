#!/bin/zsh
set -euo pipefail

cd /Users/vladimirbalko/MTG/scryglass
exec /opt/homebrew/bin/python3 -m tools.mtga_reader \
  -o /Users/vladimirbalko/MTG/my_collection_memory.json \
  --inventory
