# Headbop AI

Animação de imagens guiada pelo ritmo, com análise local de música e preview do
mesmo MP4 entregue no download. Interface em português, responsiva para celular e
computador. Backend Python/FastAPI, Librosa, OpenCV e FFmpeg; sem serviços de IA
externos, chaves de API ou download de modelos neurais.

## O que a aplicação faz

- Recebe uma imagem e um trecho de áudio entre 0,5 e 30 segundos.
- Estima BPM, posições das batidas e intensidade relativa da música.
- Gera keyframes para a imagem inteira ou para um recorte de cabeça/personagem.
- Preserva transparência existente, tenta localizar rostos frontais e permite
  selecionar manualmente uma região para refinar o recorte com GrabCut.
- Oferece movimentos **smooth**, **hard** e **velocity**, BPM de 40 a 240 e
  intensidade de 0 a 2.
- Renderiza MP4 H.264/AAC a 24 fps, com maior dimensão de até 640 pixels.
- Exporta e importa projetos **XML Headbop v1**, incluindo imagem, áudio,
  configurações, região de recorte e keyframes.

**XML Headbop não é XML Alight Motion.** Não há arquivos reais desse aplicativo
nem validação nele neste projeto. XML de outros formatos é recusado com uma
mensagem clara. Use o MP4 como saída de vídeo e o XML Headbop para guardar ou
reabrir seu trabalho. Não há exportação verificada de keyframes editáveis para
Alight Motion. Veja [limitações](docs/LIMITATIONS.md).

## Instalação e execução

Requisitos: Python 3.12 e FFmpeg com os codecs `libx264` e `aac`. O ambiente
de desenvolvimento foi preparado com Python 3.12 e FFmpeg 7. Em Debian/Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y python3-venv ffmpeg libsndfile1
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
uvicorn headbop.main:app --host 127.0.0.1 --port 8000
```

Abra o endereço local mostrado pelo Uvicorn no navegador. A API de saúde fica em
`/api/health` e a especificação OpenAPI em `/openapi.json`. Não é necessário Node.js,
banco de dados, Redis ou serviço adicional.

O projeto usa **um único processo Uvicorn** para coordenar os arquivos e a fila
de renderização; não use `--workers` maior que 1. A primeira análise com Librosa
pode ser mais lenta devido à compilação/cache do Numba.

### Docker

```bash
docker build -t headbop-ai .
docker run --rm -p 127.0.0.1:8000:8000 headbop-ai
```

O contêiner contém FFmpeg e dependências Python. Por padrão, seus projetos são
temporários; exporte o XML antes de remover o contêiner. Para acesso pelo iPhone
na mesma rede, exponha a porta apenas na rede privada de confiança, usando um
endereço alcançável pelo aparelho. O frontend não exige aplicativos nativos.

### Configuração

As variáveis abaixo são opcionais. Consulte [.env.example](.env.example).
O aplicativo lê variáveis do processo; não carrega `.env` automaticamente.

| Variável | Padrão | Uso |
| --- | --- | --- |
| `HEADBOP_DATA_DIR` | `/tmp/headbop-ai` | Diretório privado dos projetos |
| `HEADBOP_TTL_HOURS` | `24` | Prazo de expiração dos projetos inativos |
| `HEADBOP_MAX_PROJECTS` | `50` | Limite de projetos mantidos localmente |

## Como usar

1. Envie uma imagem e um arquivo de áudio de até 30 segundos. Cada upload de mídia
   pode ter até 20 MiB; imagens podem ter até 16 megapixels.
2. Confira o BPM estimado. Ajuste-o manualmente se houver uma interpretação de
   meio tempo ou tempo dobrado, comum em análise musical automática.
3. Escolha **imagem inteira** ou **cabeça/personagem**. Se o recorte automático
   não encontrar o objeto, marque um retângulo ao redor dele na imagem. Uma
   imagem PNG já recortada com transparência costuma oferecer o melhor resultado.
4. Ajuste movimento e intensidade, gere o vídeo e aguarde a renderização.
5. Reproduza o preview e baixe o MP4. O preview serve o mesmo arquivo do download,
   por isso não usa uma simulação separada da animação.
6. Exporte o XML Headbop para preservar as mídias e as configurações, ou importe
   um XML anteriormente exportado para continuar. Exclua o projeto ao terminar
   quando não desejar manter as mídias no servidor.

## Testes e qualidade

```bash
. .venv/bin/activate
python -m pytest -q
ruff check .
```

Os testes cobrem análise musical com sinais sintéticos, geração de keyframes,
segmentação, renderização e fluxos da API, além de projetos XML reais gerados
com PNG e WAV. A suíte XML verifica ida e volta, limites, dados inválidos,
entidades externas, versões e formatos não suportados. FFmpeg precisa estar
instalado para os testes de integração de vídeo.

Para executar o fluxo automatizado no Chromium, incluindo viewport móvel:

```bash
python -m playwright install chromium
python scripts/browser_smoke.py
```

`requirements.lock` fixa as versões usadas no ambiente de referência, incluindo
ferramentas de desenvolvimento. `pyproject.toml` registra os intervalos de
compatibilidade declarados; atualizar o lock exige executar novamente as
verificações.

A CI está em [.github/workflows/ci.yml](.github/workflows/ci.yml). Uma execução
local bem-sucedida não significa que a CI remota ou um iPhone físico tenham sido
validados; os resultados efetivos são informados na entrega.

Os resultados da entrega inicial estão em [validação](docs/VALIDATION.md):
78 testes aprovados tanto localmente quanto no Docker, além do fluxo Chromium
em desktop e viewport móvel.

## Privacidade e implantação

Imagens e áudio são processados no servidor onde você executa o aplicativo. A
implementação não envia mídias para serviços externos. Arquivos temporários,
metadados de projeto e vídeos ficam no diretório configurado. O XML exportado
incorpora uma cópia completa das mídias: compartilhe-o somente quando desejar
compartilhar esse conteúdo.

Projetos usam identificadores aleatórios e diretórios privados; nomes enviados
pelo cliente não se tornam caminhos no disco. A API valida dimensões, duração,
tamanhos e configurações. DTDs e entidades XML são proibidos. A expiração é
aplicada na limpeza do armazenamento, não por uma garantia de apagamento no
instante exato do prazo; a exclusão explícita remove o diretório do projeto.

A aplicação não implementa contas nem autenticação: qualquer pessoa com acesso
à instância e a um identificador pode acessar aquele projeto. A execução padrão
é local. Uma implantação pública exige controle de acesso, HTTPS, limites de
requisição no proxy e isolamento dos recursos de processamento.

## Arquitetura e decisões

Veja [arquitetura](docs/ARCHITECTURE.md), [limitações](docs/LIMITATIONS.md) e
[plano de execução](docs/PLAN.md). O nome “AI” inclui análise de sinais e visão
computacional clássica; não implica geração por modelos neurais.

Licença [MIT](LICENSE).
