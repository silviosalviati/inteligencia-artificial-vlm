# Análise de Reembolso (local)

Recebe um comprovante (foto ou PDF de NFC-e, NF-e/DANFE, cupom SAT, NFS-e/RPS, recibo, comprovante de cartão),
**extrai os dados** e calcula um **score de risco de fraude**. Tudo roda na máquina; a única chamada externa
(opcional, desligável) é a consulta do CNPJ do estabelecimento na BrasilAPI.

## Instalação

```bash
# dependências Python
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# (sem python3-venv: python3 -m venv --without-pip .venv && python3 -m pip --python .venv/bin/python install -r requirements.txt)

# modelo de visão (Hugging Face, ~3 GB)
mkdir -p models && cd models
for f in Qwen3VL-4B-Instruct-Q4_K_M.gguf mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf; do
  curl -L -o "$f" "https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/$f"; done
cd ..

# llama.cpp (CPU): https://github.com/ggml-org/llama.cpp/releases/tag/b12292
mkdir -p bin && curl -L https://github.com/ggml-org/llama.cpp/releases/download/b12292/llama-b12292-bin-ubuntu-x64.tar.gz | tar xz -C bin
```

## Como rodar

São dois terminais:

```bash
# Terminal 1 — modelo de visão (Qwen3-VL-4B, GGUF Q4, CPU) em http://127.0.0.1:8080
./start_llm.sh

# Terminal 2 — interface web em http://127.0.0.1:7860
.venv/bin/python -m reembolso.app

# ou pela linha de comando
.venv/bin/python -m reembolso.cli recibos/arquivo.pdf --valor 30
.venv/bin/python -m reembolso.cli recibos/arquivo.pdf --json --offline --nao-registrar
```

Em CPU (i5-7300U), cada análise com IA leva de 1 a 4 minutos.

## O que é verificado

| Camada | Checagens |
|---|---|
| Metadados | editor no PDF/EXIF (Photoshop, Canva, iLovePDF…), PDF com revisões incrementais, modificação após criação |
| Forense | ELA (Error Level Analysis), com mapa visual. Sinal fraco |
| QR Code | lê NFC-e/CF-e/NFS-e, extrai a chave e alerta se o QR aponta para um site não oficial |
| Extração (IA) | Qwen3-VL lê emitente, CNPJ, data, itens, total, chave e forma de pagamento; aponta sinais visuais suspeitos |
| Fiscal | DV de CNPJ (inclusive o alfanumérico) e CPF; DV da chave de 44 posições; CNPJ, mês e número da chave × impressos |
| Receita | CNPJ existe, está ATIVO e foi aberto antes da emissão; nome e UF conferem; CNAE |
| Valores | soma dos itens = total; valor solicitado > documento; limite da política; valor redondo em recibo manual |
| Datas | data futura, prazo máximo, fim de semana |
| Duplicidade | mesmo arquivo, mesma imagem (pHash), mesma chave, mesmo CNPJ+número ou CNPJ+data+valor (SQLite em `dados/`) |

Score: soma dos pesos (0–100). **<20 baixo**, **20–49 revisão humana**, **≥50 alto**.
O sistema prioriza a revisão; **não deve reprovar sozinho**.

## Limitações

- A validação definitiva de nota eletrônica é na SEFAZ ou na prefeitura. A interface mostra o link do QR,
  mas as consultas públicas usam captcha e não são automatizadas.
- Recibos não fiscais (estacionamento, táxi, recibo manual) só podem ser checados por coerência e duplicidade.
- O modelo de 4B pode errar dígitos. Por isso, chave lida do QR ou do texto do PDF tem prioridade sobre a leitura da IA.

## Estrutura

```
start_llm.sh            sobe o llama-server com o modelo
models/                 GGUF baixados de huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF
bin/                    llama.cpp b12292 (CPU)
reembolso/pipeline.py   orquestração, regras e score
reembolso/llm.py        prompt + JSON schema da extração
reembolso/validators.py CNPJ/CPF/chave de acesso
reembolso/forensics.py  PDF/imagem, metadados, ELA, QR, pHash
reembolso/historico.py  duplicidade (SQLite)
reembolso/online.py     consulta CNPJ (BrasilAPI)
reembolso/app.py        interface Gradio
reembolso/cli.py        linha de comando
tests/                  .venv/bin/python -m tests.test_validators
```
