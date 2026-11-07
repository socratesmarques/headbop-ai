# Headbop AI 2.0 — relatório de entrega

## Resumo

Evolução incremental sobre `a13bb33`, na branch `feat/headbop-2-engine`. Novos
projetos analisam imagem/áudio e geram automaticamente um MP4 com motor v2. Projetos
antigos continuam no motor original até uma troca explícita. A `main` não foi
alterada diretamente. A configuração publicada do ambiente não foi republicada.

Nenhum vídeo de referência foi encontrado nos anexos disponíveis. Portanto não
há preset `reference`, medição de fidelidade ou alegação de equivalência ao vídeo.

## Mudanças principais

- DSP local com BPM, batidas, onsets, energia, intervalos de silêncio e eventos de
  bandas baixa/alta. Estes últimos não são classificados como kick/snare reais.
- Motor com presets auto/smooth/hard/velocity, antecipação, overshoot, recuperação,
  easing Bézier, velocidade do gesto e squash/stretch. Tempos detectados são
  preservados; alteração explícita de BPM cria uma grade regular.
- Recorte por alfa, rosto frontal ou fundo uniforme, com confiança conservadora,
  seleção manual e fallback de imagem inteira. Uma máscara não presume anatomia.
- Rig de fundo/corpo/cabeça, pivôs e composição hierárquica. Separação da cabeça
  exige evidência facial ou seleção manual. Sobreposição do pescoço estende pixels
  do corpo sob a cabeça e evita duplicar o queixo na camada inferior.
- Render CPU a 24/30/60 fps, formato original/quadrado/vertical, até 1080×1920
  vertical ou 1920×1080 paisagem. Alfa premultiplicado reduz halos; frames são
  enviados ao FFmpeg sem guardar o vídeo inteiro na memória.
- UI com geração automática, máscaras, pivôs, seleção da cabeça, controles
  opcionais, timeline de energia/onsets e navegação no preview. Preview e download
  usam os mesmos bytes do MP4 H.264/AAC.
- XML v1 preservado; XML v2 guarda controles, keyframes por camada, máscara e
  metadados de rig. Importação valida duração e restaura os frames salvos. Fallback
  sem recorte e atualização de projetos antigos também preservam exportação XML.

As escolhas usam dependências já existentes: OpenCV para máscaras/inpainting,
Librosa para DSP, Pillow para imagens e FFmpeg para codificação. Não foram
adicionados modelos neurais, downloads de pesos, novas dependências de execução
ou serviços obrigatórios. Mantida licença MIT; atribuição da foto NASA de teste
em `tests/fixtures/ATTRIBUTION.md`.

## Testes e CI

Ambiente executado: Linux, Python 3.12, FFmpeg 7, Chromium do sistema
155.0.8059.79. Comandos a partir da raiz, com `.venv` ativado:

```bash
python -m pytest -q
ruff check .
node --check headbop/static/app.js
python scripts/browser_smoke.py
python scripts/render_comparison.py --output test-results/v2-final-comparison \
  --duration 2 --fps 60 --resolution 1080 --aspect vertical
```

- **279 testes passaram**, cobrindo DSP/silêncio, continuidade e sincronização,
  segmentação de fotografia e arte sintética, alfa, identidade do rig, pivôs,
  formatos/segurança, API, MP4 e preservação XML. O XML v1 tem fixture de regressão.
  Ruff e sintaxe JavaScript passaram.
- Há um aviso de depreciação do TestClient Starlette sobre `httpx`; não é falha de
  teste. Não foi suprimido nem motivou troca de dependências fora do escopo.
- Smoke de navegador passou em desktop e Chromium móvel emulado: upload e render
  automático, controles, máscara/rig, seleção manual/touch, timeline, MP4 vertical
  720×1280 a 30 fps com áudio, download idêntico ao preview, XML v1/v2 e exclusão.
  Nenhum erro JavaScript/CSP foi observado. Não equivale a Safari/iPhone físico.
- Docker construiu `headbop-ai:v2`, instalou o pacote 2.0.0 e executou a suíte como
  UID 10001. O serviço pelo CMD real respondeu HTTP 200 em `/api/health`, `/` e
  `/static/app.js`; saúde reportou FFmpeg disponível. O build usou o segredo
  BuildKit `proxy_ca` para confiar no proxy, sem desabilitar TLS.

Reprodução da validação Docker fora deste ambiente:

```bash
docker build -t headbop-ai:v2 .
docker run --rm -v "$PWD/tests:/checks/tests:ro" headbop-ai:v2 \
  python -m pytest /checks/tests -q -p no:cacheprovider
docker run --rm -p 127.0.0.1:8000:8000 headbop-ai:v2
```

Os arquivos de teste montados precisam ser legíveis pelo UID 10001. No ambiente
gerenciado foi usada uma cópia temporária legível, preservando as permissões do
checkout. Os seletores Docker foram removidos para acessar o daemon local.

### CI anterior investigada

O run de `main` [`42144976252`](https://github.com/socratesmarques/headbop-ai/actions/runs/42144976252)
falhou no passo `Run python scripts/browser_smoke.py`, com código 1. O job
`container` passou. A página pública permitiu confirmar o passo, mas logs e
artefato detalhado exigiram acesso autenticado; **a causa da falha de navegador
não foi determinada**. O teste local passando não demonstra correção dessa falha.

Os avisos de migração de Node 20 nas actions e de mudança de `ubuntu-latest`
foram observados. O workflow agora fixa `ubuntu-24.04`, actions v6/Node 24 por SHA
verificado e timeouts. O teste de navegador registra versão/executável, erros da
mídia e sua saída no resumo do job, além do artefato, para tornar uma próxima
falha diagnosticável. Isso não é uma alegação de causa resolvida.

A imagem Playwright `v1.64.1-noble` não pôde ser baixada: uma camada foi recusada
pelo registro/proxy. O navegador empacotado usado em CI não foi reproduzido
localmente; a validação local usou explicitamente `/usr/bin/chromium`.

O resultado remoto desta branch e o estado do PR serão registrados após o envio.

### Render reproduzível e desempenho

Fixture sintética de personagem transparente com cabeça selecionada manualmente,
batidas conhecidas de 120 BPM e duração de 2 s. Foram inspecionados quadros de
antecipação, impacto e recuperação; não é comparação com referência externa.

| Motor/saída | Quadros | Tempo local | Tamanho |
| --- | ---: | ---: | ---: |
| Original, 640×640, 24 fps | 48 | 0,949 s | 74.018 bytes |
| V2, 1080×1920, 60 fps | 120 | 9,209 s | 144.680 bytes |

Ambos: H.264, AAC, yuv420p, duração de 2 s verificada pelo ffprobe. Pico de memória
do processo Python: 241.436 KiB (aproximadamente 236 MiB), sem incluir o filho
FFmpeg. As saídas diferem em resolução/fps: esta tabela mede custo dos dois
cenários, não aceleração relativa dos motores. Hardware e concorrência afetam
os números. `metrics.json`, `comparison.png`, PNGs e MP4s são gerados pelo script
em `test-results/`, que permanece fora do Git.

## Limitações ou pendências

- Referência visual indisponível; qualidade de um exemplo externo não avaliada.
- Segmentação é heurística, sem reconhecimento universal de anime/personagens.
  Arte com fundo complexo pode exigir recorte/seleção manual ou PNG transparente.
- Inpainting e sobreposição do pescoço não recuperam anatomia oculta. Movimentos
  extremos, recortes inadequados ou objetos sobrepostos podem mostrar artefatos.
- CPU/60 fps/Full HD têm custo maior; vídeo limitado a 30 s. Não há fila
  distribuída, contas ou retomada de render interrompido.
- Windows/Kali estão documentados; execução específica nesses sistemas e teste
  em iPhone/Safari físico permanecem pendentes.
- Acesso a `api.github.com` retornou `CONNECT 403` pela política de rede. Foi
  preparado um rascunho restrito a esse domínio para revisão do usuário; nenhum
  ambiente foi publicado. O bloqueio afeta criação de PR e consulta autenticada
  dos logs, não comprova falta de credenciais.

Veja [LIMITATIONS.md](LIMITATIONS.md) para os limites completos e
[README](../README.md) para instalação e uso.

## Branch e Pull Request

Branch de trabalho: `feat/headbop-2-engine`, baseada em `a13bb33`.
Entrega remota pendente de confirmação após o push e a tentativa de abrir PR.
Nenhuma alteração direta à `main` foi solicitada ou executada.
