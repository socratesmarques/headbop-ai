# Headbop AI 2.0

Transforme uma imagem e um trecho de música em uma animação de headbop, usando
Python/FastAPI, Librosa, OpenCV e FFmpeg na CPU. O preview reproduz o **mesmo MP4**
que será baixado. Sem serviços externos obrigatórios, GPU, conta ou chaves de API.

## Fluxo de uso

1. Selecione uma imagem PNG/JPEG/WebP e um áudio de 0,5 a 30 segundos.
2. Clique em **Analisar e criar**. O aplicativo estima o ritmo, prepara o recorte
   quando confiável e gera o primeiro vídeo automaticamente.
3. Reproduza o vídeo. A timeline mostra batidas, onsets, energia e posição de
   reprodução; clique nela ou use o controle de tempo para navegar.
4. Opcionalmente ajuste o preset, BPM ou intensidade. Inspecione a máscara e os
   pivôs; selecione o personagem inteiro e depois a cabeça para articular o rig.
5. Gere novamente e baixe **MP4** ou salve o projeto **XML Headbop**.

O modo automático usa o recorte apenas quando há evidência suficiente. Rostos
frontais, PNGs transparentes e personagens sobre fundos uniformes têm caminhos
específicos; imagens ambíguas mantêm a opção de mover a imagem inteira. Cabeça e
corpo só são separados quando há evidência facial ou seleção manual, sem presumir
anatomia de qualquer desenho. Consulte [limitações](docs/LIMITATIONS.md).

## Presets e controles

| Controle | Comportamento |
| --- | --- |
| Auto | Escolhe uma resposta mais suave ou com impulso conforme energia e regularidade |
| Smooth | Antecipação e recuperação suaves |
| Hard | Acentos mais marcados, com transição contínua |
| Velocity | Impulso, overshoot e recuperação amortecida |
| BPM | Mantém as batidas medidas quando próximo da estimativa; outro valor cria uma grade regular |
| Velocidade | Altera a duração do gesto ao redor da batida, sem acelerar o áudio |
| Easing | Curvas Bézier natural/ease-in-out ou interpolação linear |
| Squash/stretch | Deformação controlada, com fatores horizontal/vertical recíprocos |
| Motor original | Preserva o comportamento de projetos antigos |

**Não há preset de referência ativo:** nenhum vídeo de referência estava acessível
nesta implementação. Os exemplos comparam os motores original e 2.0; não constituem
comparação com um vídeo externo.

Saída a **24, 30 ou 60 fps**, com formatos original, quadrado e vertical. O padrão
640 mantém o custo baixo. Os presets 720 e 1080 permitem HD e Full HD: vertical
720×1280 ou 1080×1920; quadrado 720×720 ou 1080×1080. No formato original, o lado
curto mira 720/1080, com o lado longo limitado a 1280/1920. A imagem cabe no canvas
sem corte intencional; o movimento ainda pode alcançar as bordas. Resoluções altas
e 60 fps custam mais CPU. Aumentar resolução não recupera detalhe ausente na imagem.

## Instalação em Linux/Kali

Python **3.12** recomendado; FFmpeg com `libx264` e `aac` é obrigatório. Não é
necessário Node.js para executar a aplicação.

```bash
sudo apt-get update
sudo apt-get install -y python3-venv ffmpeg libsndfile1
git clone https://github.com/socratesmarques/headbop-ai.git
cd headbop-ai
# Para experimentar esta evolução antes do merge:
git switch feat/headbop-2-engine
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
uvicorn headbop.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Se a distribuição oferecer apenas outra versão do Python, instale Python 3.12
com o gerenciador recomendado pela distribuição. Não substitua o Python do sistema.
A primeira análise pode demorar mais pela compilação do Numba.

## Instalação em Windows

PowerShell, Python 3.12 e FFmpeg no `PATH`:

```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
# Abra um novo terminal após a instalação.
git clone https://github.com/socratesmarques/headbop-ai.git
cd headbop-ai
git switch feat/headbop-2-engine
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m uvicorn headbop.main:app --host 127.0.0.1 --port 8000 --workers 1
```

As instruções Windows são documentadas, mas esta entrega foi executada em Linux.
Abra o endereço local informado pelo Uvicorn. Para usar no iPhone da mesma rede,
exponha a porta apenas em uma rede privada de confiança e acesse o endereço do
computador. Safari/iPhone físico ainda requer validação própria.

## Docker

```bash
docker build -t headbop-ai .
docker run --rm -p 127.0.0.1:8000:8000 headbop-ai
```

O contêiner usa usuário sem privilégios e inclui FFmpeg. Exporte seu XML antes de
removê-lo. Em ambiente com proxy TLS, o Dockerfile aceita um certificado confiável
via BuildKit: `--secret id=proxy_ca,src=/caminho/ca-bundle.pem`; a verificação TLS
permanece habilitada e o certificado não é incorporado à imagem.

## Projetos e compatibilidade

- Projetos e XML **v1** mantêm o motor original e seus keyframes. Ler um projeto
  antigo não o migra. A troca para o motor 2.0 é explícita.
- Projetos **v2** armazenam os controles novos, os keyframes por camada, a seleção
  da cabeça e o recorte. A importação restaura a timeline em vez de recalculá-la.
- A exportação do motor original continua produzindo XML v1. O motor 2.0 produz
  XML v2; versões antigas do aplicativo não conseguem abrir esse formato novo.
- O MP4 continua independente do XML. Preserve o MP4 para uma cópia visual final.

## Testes e exemplos reproduzíveis

```bash
. .venv/bin/activate
python -m pytest -q
ruff check .
python -m playwright install chromium
python scripts/browser_smoke.py
python scripts/render_comparison.py --help
```

O smoke test cria seu próprio servidor e arquivos temporários; testa upload,
preview, MP4, XML, controles e interação móvel. Screenshots e diagnósticos ficam
em `test-results/` (ignorado pelo Git). A comparação de motores usa mídia sintética
reproduzível e produz vídeos e uma folha de quadros; consulte o `--help` para as
opções e [relatório da evolução](docs/REPORT_V2.md) para resultados efetivamente
executados, desempenho medido e situação da CI.

## Configuração e privacidade

| Variável | Padrão |
| --- | --- |
| `HEADBOP_DATA_DIR` | `/tmp/headbop-ai` |
| `HEADBOP_TTL_HOURS` | `24` |
| `HEADBOP_MAX_PROJECTS` | `50` |

Veja [.env.example](.env.example). As variáveis devem ser exportadas no processo;
`.env` não é carregado automaticamente. Use **um único worker**, porque a fila é
local. Endpoints: `/api/health` e especificação `/openapi.json`.

Cada mídia enviada tem limite de 20 MiB; imagens até 16 megapixels são normalizadas
para até 1600 pixels de lado. XML tem limite de 56 MiB. Processamento e armazenamento
acontecem no servidor local, sem envio da mídia a serviços de IA. A limpeza ocorre
periodicamente; também é possível excluir a sessão. O XML incorpora as mídias:
compartilhá-lo compartilha seu conteúdo.

Não há autenticação nem isolamento por usuário. Para exposição pública, adicione
controle de acesso, HTTPS e limites de recursos. Arquivos temporários não têm
backup automático. Mais detalhes em [arquitetura](docs/ARCHITECTURE.md),
[limitações](docs/LIMITATIONS.md) e [plano](docs/ENGINE_V2_PLAN.md).

Licença [MIT](LICENSE). A fotografia usada em testes tem atribuição própria em
[tests/fixtures/ATTRIBUTION.md](tests/fixtures/ATTRIBUTION.md).
