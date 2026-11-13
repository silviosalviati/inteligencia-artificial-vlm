#!/usr/bin/env bash
# Sobe o modelo de visão (Qwen3-VL-4B) localmente via llama.cpp, API compatível com OpenAI.
# Ajustado para notebook só com CPU:
#   -np 1                 atende um recibo por vez (os demais esperam na fila, sem disputar CPU)
#   -c 4096               contexto suficiente para 1 imagem + prompt + resposta
#   --image-max-tokens    limita o custo de "ler" a imagem (principal gargalo em CPU)
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/bin/llama-b12292/llama-server" \
  -m "$DIR/models/Qwen3VL-4B-Instruct-Q4_K_M.gguf" \
  --mmproj "$DIR/models/mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf" \
  --host 127.0.0.1 --port 8080 \
  -np 1 -c 4096 -t "$(nproc)" \
  --image-max-tokens 768 \
  --jinja
