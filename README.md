# Headbop AI 2.0

Crie animações de headbop a partir de uma imagem e um trecho de áudio. A aplicação roda na CPU com Python, FastAPI, Librosa, OpenCV e FFmpeg, sem exigir GPU, conta ou chave de API. O preview exibe o mesmo MP4 disponível para download.

## O que oferece

- Seleção da área animada por pincel com reconhecimento opcional de bordas, ou pela imagem inteira.
- Articulação opcional da cabeça e presets **Auto**, **Smooth**, **Hard** e **Velocity**.
- Ajustes de BPM, velocidade, easing e squash/stretch.
- Exportação de vídeo em **MP4** (24, 30 ou 60 fps; formatos original, quadrado e vertical) e projeto editável em **XML Headbop**.
- Interface em 8 idiomas e indicador de progresso durante a renderização.

## Como usar

1. Abra o estúdio em **Criar meu vídeo** e envie uma imagem PNG/JPEG/WebP e um áudio de **0,5 a 30 segundos**.
2. Clique em **Analisar e criar**, pinte a área que deve se mover e escolha **Aplicar seleção**.
3. Ajuste o movimento e clique em **Gerar preview**.
4. Confira o resultado e baixe o MP4 ou salve o XML para editar depois.

Não é necessário login. As sessões são temporárias: salve o XML para continuar o projeto em outro momento ou dispositivo.

## Requisitos

- **Python 3.12** recomendado (compatível com versões >=3.12 e <3.14).
- **Git** e **FFmpeg** com suporte a H.264 (`libx264`) e AAC no `PATH`.
- **Linux:** `libsndfile1`. Não é necessário Node.js.

## Windows (PowerShell)

Instale os pré-requisitos:

```powershell
winget install --id Git.Git -e
winget install --id Python.Python.3.12 -e
winget install --id Gyan.FFmpeg -e
```

Abra um **novo PowerShell** e execute:

```powershell
git clone --branch main https://github.com/socratesmarques/headbop-ai.git
cd headbop-ai
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
$env:HEADBOP_ENV="development"
$env:HEADBOP_PUBLIC_ORIGIN="http://127.0.0.1:8000"
.\.venv\Scripts\python.exe -m uvicorn headbop.main:app --host 127.0.0.1 --port 8000 --workers 1
```

## Linux / Kali

Instale o Python 3.12 se ele ainda não estiver disponível na distribuição. Não substitua o Python do sistema.

```bash
sudo apt-get update
sudo apt-get install -y git python3-venv ffmpeg libsndfile1
git clone --branch main https://github.com/socratesmarques/headbop-ai.git
cd headbop-ai
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
export HEADBOP_ENV=development
export HEADBOP_PUBLIC_ORIGIN=http://127.0.0.1:8000
uvicorn headbop.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Depois de iniciar, acesse **http://127.0.0.1:8000**. Encerre o servidor com `Ctrl+C`.

## Docker

```bash
docker build -t headbop-ai .
docker run -d --name headbop -p 127.0.0.1:8000:8000 \
  -v headbop_data:/data -e HEADBOP_DATA_DIR=/data \
  -e HEADBOP_ENV=development \
  -e HEADBOP_PUBLIC_ORIGIN=http://127.0.0.1:8000 headbop-ai
```

O volume preserva os arquivos entre reinícios do contêiner, respeitando o prazo de expiração dos projetos.

## Projetos e limites

- **XML v1** mantém o motor original; **XML v2** guarda máscaras, camadas e controles do motor 2.0. A importação preserva a timeline.
- O **MP4** é o vídeo final; o **XML** permite retomar a edição e incorpora as mídias — tenha cuidado ao compartilhá-lo.
- Cada arquivo enviado tem limite de **20 MiB**; o prazo padrão das sessões é **24 horas**.
- Os arquivos são processados no servidor onde a aplicação é executada, sem envio obrigatório a serviços externos.

## Testes

Com o ambiente configurado, execute os testes unitários e a análise estática:

```bash
# Linux (venv ativado)
python -m pytest -q
python -m ruff check .
```

No Windows, substitua `python` por `.\.venv\Scripts\python.exe`. Para testes de navegador e resultados de desempenho, consulte [o relatório da v2](docs/REPORT_V2.md) e a [CI da main](https://github.com/socratesmarques/headbop-ai/actions?query=branch%3Amain).

## Configuração e implantação

- Variáveis disponíveis em [`.env.example`](.env.example). Defina-as no ambiente do processo; o `.env` não é carregado automaticamente.
- Execute com **um único worker**, pois a fila de renderização é local.
- Para publicar com domínio, HTTPS, limites e persistência, siga [docs/PRODUCTION.md](docs/PRODUCTION.md).
- Consulte também [arquitetura](docs/ARCHITECTURE.md) e [limitações](docs/LIMITATIONS.md).

Licença [MIT](LICENSE). Atribuição da imagem de teste em [tests/fixtures/ATTRIBUTION.md](tests/fixtures/ATTRIBUTION.md).
