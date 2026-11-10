# Correções e validação da PR #3

Esta alteração corrige os sete achados do relatório `headbop-pr3-review.md` sobre
a PR [#3](https://github.com/socratesmarques/headbop-ai/pull/3), a partir do commit
`dbad954f9741deca79febd1690e7e23854b4a132`. O código foi validado em Linux,
Python 3.12, FFmpeg e Chromium 155. Não houve implantação em domínio público.

## Achados corrigidos

| Achado | Correção | Evidência automatizada |
| --- | --- | --- |
| A1: render pode bloquear a fila | Supervisor de processo com prazo total, encerramento dos descendentes, limite de saída e limpeza; timeout inclui preparação nativa, pipe, mux e ffprobe | `test_jobs.py`, `test_renderer.py`, `test_security.py`: encoder que não lê stdin, mux/probe travados, worker nativo bloqueado, cancelamento e geração real após falha |
| A2: falta de autenticação/autorização | Contas individuais com scrypt, sessões opacas revogáveis em SQLite, cookie HttpOnly/Secure/SameSite, CSRF/origem e proprietário obrigatório em cada rota | `test_auth.py`, `test_security.py`, smoke do navegador e staging: acesso sem sessão 401; outra conta recebe 404, inclusive mídia/XML/exclusão |
| A3: abuso e cotas globais insuficientes | Limites persistentes por IP/conta, cotas por usuário, reservas atômicas antes do multipart, admissão por conta e fila limitada; trava impede múltiplos workers | `test_store.py`, `test_security.py`, staging: concorrência não ultrapassa reserva, erro libera admissão, XFF falso não contorna limite e reinício não limpa contadores |
| M1: catálogos incompletos | 315 entradas em cada um dos oito idiomas, com os mesmos parâmetros | `test_i18n.py`: igualdade de chaves, parâmetros, textos HTML, mensagens dinâmicas, avisos e instruções de validação de mídia/XML |
| M2: textos dinâmicos não atualizados | Bindings explícitos de texto/atributos, incluindo instruções, resumo, avisos e erros; sem MutationObserver contínuo | `i18n_browser_test.py`: oito idiomas em 1440/390/320 px, persistência, nomes de arquivo preservados e 100 trocas sem mutações ociosas |
| M3: requisição pendente trava interface | AbortController e prazo cobrindo resposta e corpo; prazo total da fila informado pelo servidor, sem renovação a cada poll | Teste de navegador: fetch/corpo que nunca terminam, erro no POST, recuperação dos botões e job ainda na fila após 301 s com expiração em 630 s |
| B1: aria-valuetext desatualizado | Atributos de progresso participam dos bindings de idioma | Teste de navegador valida texto indeterminado e percentual nos oito idiomas |

Também foram corrigidos erros 500 causados por CSRF não ASCII/usuário Unicode
malformado, marcação incorreta de 100% quando o arquivo pronto desapareceu e
buffer ilimitado de diagnósticos do decodificador de áudio. Projetos sem dono são
preservados para migração explícita. A CLI recusa transferir um projeto já atribuído.

## Execuções concluídas

| Comando/validação | Resultado |
| --- | --- |
| `python -m pytest -q` | **425 passaram**, 70,92 s |
| `ruff check .` | Passou |
| `pip check` | Nenhuma dependência incompatível |
| `node --check` em app.js, auth.js e i18n.js | Passou |
| `git diff --check` | Passou |
| `python scripts/browser_smoke.py` | Passou com geração automática v2, MP4 vertical 720×1280/30 FPS com áudio, bytes iguais no preview/download, XML v1/v2, edição, seleção por toque, exclusão e ausência de erros JS/CSP |
| Autenticação no smoke real | Login desktop/celular, sessão persistida, reabertura pelo histórico, logout, conta diferente sem acesso e revogação administrativa passaram |
| `python scripts/i18n_browser_test.py` | Passou nos oito idiomas × três larguras; inclui falhas de rede, prazo de fila e acessibilidade |
| `docker build --secret id=proxy_ca,src=… -t headbop-ai:pr3-fixes .` | Passou, mantendo verificação TLS |
| `docker compose -f compose.staging.yml config --quiet` e `caddy validate` | Passaram |
| `python scripts/staging_smoke.py --image headbop-ai:pr3-fixes` | Passou com a configuração de produção, em portas loopback e CA local confiável |

A suíte Python emitiu um aviso de depreciação do adaptador HTTPX do TestClient,
sem falhas. Os testes HTTP/navegador exigiram execução fora do bloqueio de sockets
do sandbox. Nenhuma verificação TLS foi desabilitada.

No staging foram confirmados HTTPS local, redirecionamento HTTP, flags dos cookies,
CSRF/origem, autorização, vídeo H.264/AAC real, progresso 95% antes da conclusão e
100% apenas com arquivo pronto, XML, persistência de sessão/proprietário/MP4 após
reinício e limites de login persistentes mesmo com X-Forwarded-For falsificado.
A imagem de execução testada foi
`sha256:1be704aeb35152442a53e7d6a1ded06e635acfffccb652071c31a1da4dc89d0c`.

O container rodou com UID 10001, raiz somente leitura, `/data` gravável, 2 CPUs,
4 GiB de memória, 128 processos, capabilities removidas e porta do backend não
publicada. O harness acrescenta somente `app` às exceções do proxy para a rede
privada do teste e usa `tls internal`; isso não valida ACME público. Containers e
rede de teste foram removidos ao terminar; volumes foram preservados.

Logs e screenshots locais ficam em `test-results/` e `/tmp/headbop-fixes-*.log`.
Eles não são incluídos no Git. A CI executará novamente após o envio; resultados
locais não equivalem à aprovação remota do novo commit.

## Limites e pendências externas

- Implantação suportada: uma instância Linux, uma fila limitada, contas criadas
  pelo administrador. Não há cadastro aberto, reset por e-mail ou fila distribuída.
- DNS, certificado público/renovação ACME, firewall, backup/restauração e alertas
  precisam ser validados na infraestrutura escolhida.
- Carga e custos com usuários concorrentes e mídia máxima precisam ser medidos no
  host final. Cotas e limites não substituem proteção DDoS de infraestrutura.
- Safari/iPhone físico, Firefox e Windows não foram validados nesta execução.
- Não foi executada uma varredura externa atualizada de CVEs da imagem/dependências;
  `pip check` verifica compatibilidade, não vulnerabilidades.
- Revisão linguística por falantes nativos e validação com leitores de tela reais
  continuam sendo etapas de qualidade distintas dos testes automatizados.
- Termos, política de privacidade/retenção e canal de suporte dependem da operação.

As correções do relatório estão implementadas e os testes acima passaram. A
liberação pública deve depender também das verificações externas listadas e da
CI do commit enviado. Siga [PRODUCTION.md](PRODUCTION.md) para contas, migração,
backup e implantação; preserve o volume e não use `down -v` na atualização.
