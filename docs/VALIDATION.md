# Validação da entrega inicial

## Resultados executados

| Verificação | Resultado |
| --- | --- |
| `python -m pytest -q` | 78 testes passaram; nenhum ignorado |
| `ruff check .` | Passou |
| `node --check headbop/static/app.js` | Passou |
| `python -m pip check` | Nenhum conflito de dependências |
| `sh scripts/setup.sh` | Instalou o lock e o pacote editável com sucesso |
| `python scripts/browser_smoke.py` | Fluxos desktop e móvel passaram no Chromium |
| Build Docker | Passou com certificados TLS verificados |
| Suíte no contêiner, usuário sem privilégios | 78 testes passaram |
| Serviço Docker iniciado pelo CMD | Saúde, HTML e JavaScript retornaram com sucesso |

Referência local: Python 3.12.14, FFmpeg 7.1.5, dependências de
`requirements.lock`. O contêiner usa Python 3.12 em Debian Bookworm com FFmpeg 5.1.
Foi necessário conceder leitura dos arquivos da aplicação ao usuário sem
privilégios da imagem; o build e a suíte foram repetidos após essa correção.

A suíte emite um aviso de depreciação do Starlette sobre seu adaptador de testes
com HTTPX. Nenhuma asserção foi desativada para obter aprovação.

## Comportamentos verificados

- BPM de sinais sintéticos a 90/120/150, alinhamento das batidas e intensidade.
- Keyframes determinísticos e diferenças entre smooth, hard e velocity.
- Transparência, seleção manual e detecção automática em uma fotografia frontal
  real de domínio público (atribuição em `tests/fixtures/ATTRIBUTION.md`).
- Streams H.264/AAC, transformação dos pixels e remoção da área original no fundo.
- Upload, configurações, renderização, exportação, importação e exclusão pela API.
- Mesmos bytes no MP4 de preview e download; reprodução no navegador.
- XML próprio com mídias PNG/WAV, preservação das configurações e rejeição de
  entidades externas, DTDs, documentos inválidos e formatos alheios.
- Limites de requisições, rejeição de playlists de áudio, limpeza de projetos e
  recuperação de falhas de renderização.
- Interface desktop e viewport móvel de 390 pixels; seleção por mouse e toque,
  importação, renderização e exclusão; sem overflow horizontal, erros JS ou CSP.

O teste de navegador gera imagens de evidência e log em `test-results/`, ignorado
pelo Git. Ele cria e encerra seu próprio servidor e remove seus arquivos temporários.

## O que estes resultados não comprovam

Não houve teste físico em iPhone/Safari, execução do Alight Motion, importação de
seu XML real, benchmark geral de precisão musical ou segmentação de personagens
arbitrários. A CI foi configurada, mas sua execução no GitHub é independente dos
testes locais. Salvar a configuração do ambiente não publica um snapshot nem
comprova restauração em uma nova tarefa.
