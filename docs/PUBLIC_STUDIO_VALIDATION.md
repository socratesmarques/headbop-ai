# Home pública, acesso livre e pincel inteligente

Validação local em 14/11/2026, sobre a `main` d60f5e3 (PR #3 já integrada).
Esta alteração está na branch `feature/free-studio-smart-brush`.

## Comportamento entregue

- Home responsiva com apresentação, demonstração vetorial pausável, instruções
  em três passos e links para o repositório. Respeita movimento reduzido.
- Entrada no estúdio sem cadastro, senha ou provisionamento. Sessões privadas
  mantêm propriedade de projetos, cookies HttpOnly, CSRF, origem e limites.
- A análise abre o pincel e aguarda a escolha da área; não renderiza a imagem
  inteira automaticamente. Pintura inteligente por cor/bordas, modo manual,
  borracha, tamanho, desfazer/refazer e refinamento da máscara salva.
- Mouse, toque e teclado, com traços normalizados e processamento ao aplicar.
  Falhas de aplicação preservam os traços para tentar novamente.
- Pintura escolhe os pixels animados; articulação opcional da cabeça define a
  separação e o pivô dentro do personagem. Retângulo e imagem inteira continuam
  disponíveis. Áreas pintadas se movem como uma camada.
- 394 chaves em cada um dos oito idiomas, incluindo home, pincel, erros,
  instruções dinâmicas e acessibilidade. Sem observador contínuo de mutações.
- XML v1 permanece compatível; máscaras pintadas usam XML v2 mesmo com motor
  original, preservando o recorte e o motor escolhido.

## Comandos e resultados

Python 3.12, Chromium 155.0.8059.79 e FFmpeg em Linux. O ambiente usa o Python de
`/workspace/headbop-ai/.venv/bin/python`, com `PYTHONPATH=.` apontando para este
checkout; os comandos abaixo usam os nomes usuais de um virtualenv ativado.

| Verificação | Resultado |
| --- | --- |
| `python -m pytest -q` | **484 passaram**, 78,71 s; um aviso de depreciação Starlette/httpx |
| `ruff check .` | Aprovado |
| `pip check` | Sem dependências incompatíveis |
| `node --check` em app.js, auth.js, brush.js e i18n.js | Aprovado |
| `git diff --check` | Aprovado |
| `python scripts/browser_smoke.py` | Aprovado, backend/FFmpeg reais |
| `python scripts/i18n_browser_test.py` | Aprovado, 8 idiomas × 1440/390/320 px |
| `python scripts/brush_browser_test.py` | Aprovado, mouse/toque/teclado |
| Docker build `headbop-ai:public-studio` | Aprovado |
| `python scripts/staging_smoke.py --image headbop-ai:public-studio --project headbop-public-studio-final2-1114` | Aprovado, HTTPS local e backend em contêiner |

O smoke real cobre upload, pintura, preview H.264/AAC 720×1280 a 30 fps, igualdade
entre preview e download, XML v2, controles avançados, timeline, recorte e cabeça
por toque, XML v1 legado, exclusão, histórico privado, revogação e isolamento
entre navegadores. Não houve erros JavaScript ou violações de CSP.

Os testes de UI com respostas determinísticas exercitam coordenadas com barras
laterais na imagem, falha/timeout de aplicação, refinamento, ausência de chamadas
por traço, ausência de render automático, 100 trocas de idioma sem loop,
persistência do idioma, textos dinâmicos, `aria-valuetext`, timeouts de resposta e
corpo HTTP, expiração de sessão e recuperação de controles.

Novos testes Python verificam isolamento em todas as rotas, CSRF/origem, cotas
compartilhadas entre novos cookies, IPv6 /64, rate limiting persistente, limpeza,
validação de traços, expansão inteligente, alfa, borracha, rollback de falhas na
gravação, render real com máscara e XML legado pintado. Dois testes reproduziram
um bypass concorrente de quantidade/disco: a consulta das identidades foi movida
para dentro do lock de reserva, após admissão. Ambos falhavam antes e passam agora.

A repetição final encontrou overflow no cabeçalho do estúdio móvel. Os controles
agora se reorganizam em linhas; o teste compara largura do documento com
`clientWidth`, evitando que `innerWidth` esconda overflow no viewport móvel. O
teste de toque converte coordenadas DOM para o viewport visual e verifica os
retângulos enviados. O fluxo completo passou novamente após essas correções.

## Docker e staging

Build usando o daemon local, com verificação TLS e certificado fornecido apenas
como segredo BuildKit:

```bash
docker --host=unix:///var/run/docker.sock build \
  --secret id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt \
  -t headbop-ai:public-studio .
python scripts/staging_smoke.py --image headbop-ai:public-studio \
  --project headbop-public-studio-final2-1114
```

O projeto de teste usa portas de loopback 18084/18444 e CA local do Caddy,
confiada explicitamente pelo cliente; nunca desativa a verificação TLS. O script
preserva volumes e derruba apenas seus próprios contêineres ao terminar.

Staging **aprovado** com imagem
`sha256:efa0e74f0a726586b5e3db0fde5f4a0cde6971c1aeb2038a35abf5e4e22813ec`.
Validados Compose e Caddy, HTTPS com CA confiável, redirecionamento HTTP→HTTPS,
sessões automáticas distintas sem provisionamento, cookies seguros, origem/CSRF,
negação de acesso a projetos alheios, pincel real, MP4 H.264/AAC e XML com máscara.
O progresso permaneceu abaixo de 100 até o arquivo estar pronto. Reinícios
preservaram sessões, propriedade, MP4, cotas e rate limiting. Limpar cookies e
forjar `X-Forwarded-For` não reiniciou os limites de convidados.

Inspeção do contêiner confirmou UID 10001, raiz somente leitura, capabilities
removidas, backend sem porta pública e limites de 2 CPUs, 4 GiB e 128 processos.

## Evidências e limites

Logs desta execução: `/tmp/headbop-public-final-pytest.log`,
`/tmp/headbop-public-final-browser.log`, `/tmp/headbop-public-final-i18n.log`,
`/tmp/headbop-public-final-brush.log`, `/tmp/headbop-public-docker-build.log` e
`/tmp/headbop-public-staging.log`. O checkout contém screenshots em
`test-results/home-desktop.png`, `home-mobile.png`, `desktop-painted.png` e
`mobile-ready.png`, além de `staging-results.json`. Esses artefatos locais são
ignorados pelo Git; os scripts permitem reproduzi-los.

Não foi implantado um domínio público. DNS, emissão/renovação ACME, firewall,
carga do host final, backup/restauração e browsers físicos Safari/iOS/Firefox
dependem de validação externa. O teste móvel usa emulação Chromium. Estes
resultados não são um benchmark de capacidade ou uma auditoria atual de CVEs.
Consulte [PRODUCTION.md](PRODUCTION.md) e [LIMITATIONS.md](LIMITATIONS.md).

As sessões duram até 12 horas e os projetos têm TTL padrão de 24 horas. Limpar
cookies ou expirar a sessão remove o acesso pelo navegador; o XML permite
continuar depois. Visitantes na mesma rede compartilham cotas, mas não projetos.
