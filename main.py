#!/usr/bin/env python3
"""Sobe tudo com um comando: modelo de visão (llama-server) + interface web, e abre o navegador.

    python main.py                  # Windows: GPU (99 camadas); Linux: padrão do llama.cpp
    python main.py --gpu-layers 0   # só CPU
    python main.py --sem-navegador  # não abre o navegador automaticamente

Ctrl+C encerra o modelo e a interface juntos. Não precisa ativar a venv: o Python do
.venv do projeto é usado automaticamente para a interface web.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
VERSAO_LLAMA = "llama-b12292"
MODELO = "Qwen3VL-4B-Instruct-Q4_K_M.gguf"
MMPROJ = "mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf"
# 2 páginas × 768 tokens de imagem + texto do PDF + prompt + até 2048 tokens de resposta
CONTEXTO_PADRAO = 8192
HOST = "127.0.0.1"
PORTA_MODELO = 8080
PORTA_WEB = 7860
ESPERA_WEB_S = 180
ESPERA_MODELO_S = 900

# Conexões locais não devem passar por proxy do sistema.
_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _msg(texto: str = "", erro: bool = False) -> None:
    print(texto, file=sys.stderr if erro else sys.stdout, flush=True)


def responde(url: str, timeout: float = 2) -> bool:
    """True se a URL responde HTTP 200 (o /health do llama-server dá 503 enquanto carrega)."""
    try:
        with _http.open(url, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def comando_modelo(gpu_layers: int | None = None, contexto: int = CONTEXTO_PADRAO,
                   extras=(), porta: int = PORTA_MODELO) -> list[str] | None:
    """Monta o comando do llama-server, ou None (com mensagem) se faltar algo."""
    if sys.platform == "win32":
        executavel = "llama-server.exe"
        if gpu_layers is None:
            gpu_layers = 99
    elif sys.platform.startswith("linux"):
        executavel = "llama-server"
    else:
        _msg(f"Sistema operacional não suportado: {sys.platform}. Use Windows ou Linux.", erro=True)
        return None

    servidor = ROOT / "bin" / VERSAO_LLAMA / executavel
    modelo = ROOT / "models" / MODELO
    mmproj = ROOT / "models" / MMPROJ
    ausentes = [a for a in (servidor, modelo, mmproj) if not a.is_file()]
    if ausentes:
        for a in ausentes:
            dica = " (download incompleto?)" if a.with_name(a.name + ".part").exists() else ""
            _msg(f"Arquivo não encontrado: {a}{dica}", erro=True)
        _msg("Consulte as instruções de instalação no README.md.", erro=True)
        return None

    comando = [
        str(servidor), "-m", str(modelo), "--mmproj", str(mmproj),
        "--host", HOST, "--port", str(porta),
        "-np", "1", "-c", str(contexto), "-t", str(os.cpu_count() or 1),
        "--image-max-tokens", "768", "--jinja",
    ]
    if gpu_layers is not None:
        comando.extend(["-ngl", str(gpu_layers)])
    comando.extend(extras)
    return comando


def python_do_projeto() -> str:
    """Python do .venv (com gradio etc.), mesmo que main.py tenha sido chamado por outro Python."""
    for candidato in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if candidato.is_file():
            return str(candidato)
    return sys.executable


def _aguardar(url: str, processo: subprocess.Popen | None, limite_s: float, nome: str) -> bool:
    inicio = time.monotonic()
    aviso = 15.0
    while time.monotonic() - inicio < limite_s:
        if responde(url):
            return True
        if processo is not None and processo.poll() is not None:
            _msg(f"✖ {nome} encerrou durante a inicialização (código {processo.returncode}).", erro=True)
            return False
        decorrido = time.monotonic() - inicio
        if decorrido >= aviso:
            _msg(f"  … aguardando {nome} ({int(decorrido)} s)")
            aviso += 15
        time.sleep(0.5)
    _msg(f"✖ {nome} não respondeu em {int(limite_s)} s.", erro=True)
    return False


def _resumo_gpu(log: Path) -> list[str]:
    """Linhas do log do llama-server que dizem onde o modelo foi carregado."""
    try:
        linhas = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    chaves = ("offloaded", "Device 0:", "no usable GPU", "CUDA error")
    return [ln.strip() for ln in linhas if any(c in ln for c in chaves)][:4]


def _final_do_log(log: Path, n: int = 15) -> str:
    try:
        return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def _encerrar(processos: dict[str, subprocess.Popen]) -> None:
    for p in processos.values():
        if p.poll() is None:
            p.terminate()
    for p in processos.values():
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Opções não reconhecidas são repassadas ao llama-server.")
    parser.add_argument("--gpu-layers", type=int, default=None, metavar="N",
                        help="camadas na GPU (Windows: 99 por padrão; 0 = só CPU)")
    parser.add_argument("--ctx", type=int, default=CONTEXTO_PADRAO, metavar="N",
                        help=f"contexto do modelo em tokens (padrão: {CONTEXTO_PADRAO})")
    parser.add_argument("--sem-navegador", action="store_true", help="não abre o navegador")
    parser.add_argument("--so-modelo", action="store_true", help="sobe só o modelo, sem a interface")
    args, extras = parser.parse_known_args(argv)
    if args.gpu_layers is not None and args.gpu_layers < 0:
        parser.error("--gpu-layers deve ser maior ou igual a zero")
    if args.ctx < 2048:
        parser.error("--ctx deve ser de pelo menos 2048")

    url_saude = f"http://{HOST}:{PORTA_MODELO}/health"
    url_web = f"http://{HOST}:{PORTA_WEB}/"
    log_modelo = ROOT / "logs" / "llama-server.log"
    processos: dict[str, subprocess.Popen] = {}

    try:
        # 1. Modelo de visão
        if responde(url_saude):
            _msg(f"✔ Modelo de visão já está rodando em {HOST}:{PORTA_MODELO} — reaproveitando.")
        else:
            comando = comando_modelo(args.gpu_layers, args.ctx, extras)
            if comando is None:
                return 1
            log_modelo.parent.mkdir(exist_ok=True)
            _msg("▶ Iniciando o modelo de visão (log em logs/llama-server.log)…")
            try:
                with open(log_modelo, "w", encoding="utf-8") as log:
                    processos["Modelo de visão"] = subprocess.Popen(
                        comando, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            except OSError as e:
                _msg(f"Não foi possível iniciar {comando[0]}: {e}", erro=True)
                return 1

        if args.so_modelo:
            if "Modelo de visão" in processos and not _aguardar(
                    url_saude, processos["Modelo de visão"], ESPERA_MODELO_S, "Modelo de visão"):
                _msg(_final_do_log(log_modelo), erro=True)
                return 1
            _msg(f"✔ Modelo pronto em http://{HOST}:{PORTA_MODELO}. Ctrl+C para encerrar.")
        else:
            # 2. Interface web (sobe em paralelo enquanto o modelo carrega)
            if responde(url_web):
                _msg(f"✔ Interface web já está rodando em {url_web} — reaproveitando.")
            else:
                _msg("▶ Iniciando a interface web…")
                env = dict(os.environ, PYTHONUTF8="1",
                           REEMBOLSO_LLM_URL=f"http://{HOST}:{PORTA_MODELO}/v1/chat/completions")
                processos["Interface web"] = subprocess.Popen(
                    [python_do_projeto(), "-m", "reembolso.app"], cwd=ROOT, env=env)
                if not _aguardar(url_web, processos["Interface web"], ESPERA_WEB_S, "Interface web"):
                    return 1

            _msg("")
            _msg("═" * 52)
            _msg(f"  Acesse no navegador:  {url_web}")
            _msg("  Ctrl+C neste terminal encerra tudo.")
            _msg("═" * 52)
            _msg("")
            if not args.sem_navegador:
                webbrowser.open(url_web)

            # 3. Espera o modelo terminar de carregar (a interface já funciona e mostra o status)
            if "Modelo de visão" in processos:
                if _aguardar(url_saude, processos["Modelo de visão"], ESPERA_MODELO_S, "Modelo de visão"):
                    _msg("✔ Modelo de visão pronto.")
                    for linha in _resumo_gpu(log_modelo):
                        _msg(f"    {linha}")
                else:
                    _msg("Últimas linhas de logs/llama-server.log:\n" + _final_do_log(log_modelo), erro=True)
                    _msg("A interface continua no ar, mas sem extração por IA.", erro=True)
                    processos.pop("Modelo de visão", None)

        # 4. Supervisão: se algo cair, encerra o resto
        while processos:
            for nome, p in list(processos.items()):
                if p.poll() is not None:
                    _msg(f"✖ {nome} encerrou (código {p.returncode}). Encerrando o restante…", erro=True)
                    if nome == "Modelo de visão":
                        _msg(_final_do_log(log_modelo), erro=True)
                    processos.pop(nome)
                    return p.returncode or 1
            time.sleep(1)
        if not args.so_modelo:
            _msg("Tudo já estava rodando; nada a supervisionar.")
        return 0
    except KeyboardInterrupt:
        _msg("\nEncerrando…")
        return 130
    finally:
        _encerrar(processos)


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8")
    sys.exit(main())
