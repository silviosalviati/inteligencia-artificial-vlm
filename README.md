# Análise de Reembolso (local)

Recebe um comprovante (foto ou PDF de NFC-e, NF-e/DANFE, cupom SAT, NFS-e/RPS, recibo, comprovante de cartão),
**extrai os dados** e calcula um **score de risco de fraude**. Tudo roda na máquina; a única chamada externa
(opcional, desligável) é a consulta do CNPJ do estabelecimento na BrasilAPI.

## Instalação no Linux

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

## Instalação no Windows (PowerShell)

Instale o Python e, para usar uma GPU NVIDIA, um driver compatível com CUDA 13.5. Na pasta do projeto:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Modelo de visão (~3 GB)
New-Item -ItemType Directory -Force models
$base = "https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main"
foreach ($f in @("Qwen3VL-4B-Instruct-Q4_K_M.gguf", "mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf")) {
    Invoke-WebRequest "$base/$f" -OutFile "models\$f"
}

# llama.cpp CUDA e suas bibliotecas (Windows x64, GPU NVIDIA)
New-Item -ItemType Directory -Force bin\llama-b12292
$release = "https://github.com/ggml-org/llama.cpp/releases/download/b12292"
Invoke-WebRequest "$release/llama-b12292-bin-win-cuda-13.5-x64.zip" -OutFile "bin\llama-cuda.zip"
Invoke-WebRequest "$release/cudart-llama-bin-win-cuda-13.5-x64.zip" -OutFile "bin\cudart.zip"
Expand-Archive "bin\llama-cuda.zip" -DestinationPath "bin\llama-b12292" -Force
Expand-Archive "bin\cudart.zip" -DestinationPath "bin\llama-b12292" -Force
```

O arquivo `bin\llama-b12292\llama-server.exe` e as DLLs dos dois pacotes devem ficar na mesma pasta.
Após a extração, os arquivos ZIP em `bin` podem ser removidos. Confira o driver com `nvidia-smi` e
confirme nos logs do servidor que a GPU e as camadas do modelo foram carregadas. Para outras GPUs,
consulte os pacotes da [versão b12292](https://github.com/ggml-org/llama.cpp/releases/tag/b12292);
uma versão CPU não utiliza a placa de vídeo.

## Como rodar

São dois terminais. No Linux:

```bash
# Terminal 1 — modelo de visão (Qwen3-VL-4B, GGUF Q4, CPU) em http://127.0.0.1:8080
./start_llm.sh

# Terminal 2 — interface web em http://127.0.0.1:7860
.venv/bin/python -m reembolso.app

# ou pela linha de comando
.venv/bin/python -m reembolso.cli recibos/arquivo.pdf --valor 30
.venv/bin/python -m reembolso.cli recibos/arquivo.pdf --json --offline --nao-registrar
```

No Windows (PowerShell):

```powershell
# Terminal 1 — modelo de visão em http://127.0.0.1:8080
.\.venv\Scripts\python.exe start_llm.py

# Terminal 2 — interface web em http://127.0.0.1:7860
.\.venv\Scripts\python.exe -m reembolso.app

# ou pela linha de comando
.\.venv\Scripts\python.exe -m reembolso.cli arquivos\arquivo.pdf --valor 30
.\.venv\Scripts\python.exe -m reembolso.cli arquivos\arquivo.pdf --json --offline --nao-registrar
```

Substitua `arquivo.pdf` pelo nome do comprovante. A pasta `arquivos/` é ignorada pelo Git
para evitar que recibos locais sejam adicionados acidentalmente ao repositório.

O inicializador detecta Windows ou Linux e usa os arquivos na pasta do projeto, independentemente do
diretório atual. No Windows, solicita até 99 camadas na GPU; isso exige um binário com suporte à GPU.
Para usar apenas CPU, acrescente `--gpu-layers 0`. No Linux, o padrão do llama.cpp é mantido;
use `--gpu-layers N` para ajustar o número de camadas na GPU em qualquer sistema.
O comando `./start_llm.sh` continua disponível no Linux e aceita as mesmas opções.

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
start_llm.py            sobe o llama-server no Windows ou Linux
start_llm.sh            atalho Linux para o inicializador
models/                 GGUF baixados de huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF
bin/                    llama.cpp b12292 (CPU ou GPU)
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
