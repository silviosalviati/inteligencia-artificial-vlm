#!/usr/bin/env python3
"""Inicia o servidor de visão local no Windows ou no Linux."""

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
# 2 páginas × 768 tokens de imagem + texto do PDF + prompt + até 2048 tokens de resposta
CONTEXTO_PADRAO = 8192


def iniciar(gpu_layers=None, contexto=CONTEXTO_PADRAO, extras=()):
    if sys.platform == "win32":
        executavel = "llama-server.exe"
        if gpu_layers is None:
            gpu_layers = 99
    elif sys.platform.startswith("linux"):
        executavel = "llama-server"
    else:
        print(f"Sistema operacional não suportado: {sys.platform}. Use Windows ou Linux.", file=sys.stderr)
        return 1

    servidor = ROOT / "bin" / "llama-b12292" / executavel
    modelo = ROOT / "models" / "Qwen3VL-4B-Instruct-Q4_K_M.gguf"
    mmproj = ROOT / "models" / "mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf"
    ausentes = [arquivo for arquivo in (servidor, modelo, mmproj) if not arquivo.is_file()]
    if ausentes:
        for arquivo in ausentes:
            print(f"Arquivo não encontrado: {arquivo}", file=sys.stderr)
        print("Consulte as instruções de instalação no README.md.", file=sys.stderr)
        return 1

    comando = [
        str(servidor), "-m", str(modelo), "--mmproj", str(mmproj),
        "--host", "127.0.0.1", "--port", "8080",
        "-np", "1", "-c", str(contexto), "-t", str(os.cpu_count() or 1),
        "--image-max-tokens", "768", "--jinja",
    ]
    if gpu_layers is not None:
        comando.extend(["-ngl", str(gpu_layers)])
    comando.extend(extras)

    try:
        return subprocess.run(comando, check=False).returncode
    except OSError as erro:
        print(f"Não foi possível iniciar {servidor}: {erro}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Opções não reconhecidas são repassadas ao llama-server (ex.: --port 8081).")
    parser.add_argument(
        "--gpu-layers", type=int, default=None, metavar="N",
        help="número de camadas na GPU (Windows: 99 por padrão; use 0 para CPU)",
    )
    parser.add_argument(
        "--ctx", type=int, default=CONTEXTO_PADRAO, metavar="N",
        help=f"tamanho do contexto em tokens (padrão: {CONTEXTO_PADRAO})",
    )
    args, extras = parser.parse_known_args()
    if args.gpu_layers is not None and args.gpu_layers < 0:
        parser.error("--gpu-layers deve ser maior ou igual a zero")
    if args.ctx < 2048:
        parser.error("--ctx deve ser de pelo menos 2048")
    return iniciar(args.gpu_layers, args.ctx, extras)


if __name__ == "__main__":
    sys.exit(main())
