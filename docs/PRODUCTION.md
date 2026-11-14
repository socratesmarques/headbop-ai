# Implantação do estúdio público

A home é pública. Ao entrar no estúdio, a aplicação cria uma sessão privada para
o navegador, sem cadastro, senha ou provisionamento pelo administrador. Projetos,
mídia e exportação continuam protegidos por autorização: uma sessão só acessa
seus próprios projetos. Não há recuperação por e-mail ou provedor de identidade.
Esta configuração atende uma única instância Linux, com uma fila de renderização
limitada. Não aumente `--workers` nem compartilhe o volume entre réplicas: o
controle de concorrência e as reservas de disco são locais ao processo.

## Docker e HTTPS

Requer Docker Engine/Compose, domínio com DNS apontando ao servidor e portas
TCP 80/443 disponíveis. Crie `.env` (ignorado pelo Git):

```dotenv
HEADBOP_DOMAIN=studio.exemplo.com
```

O nome `compose.staging.yml` foi mantido para preservar o caminho de implantação
existente. Ele protege as sessões da aplicação, sem senha compartilhada do Caddy:

```bash
docker compose -f compose.staging.yml config --quiet
docker compose -f compose.staging.yml up -d --build
```

Abra o endereço HTTPS e clique em **Criar meu vídeo**. A home sozinha não cria
identidades no banco. `POST /api/auth/guest` exige a origem correta e reutiliza
uma sessão válida; não renova sua duração absoluta. O cookie de sessão é Secure,
HttpOnly e SameSite=Strict; o token opaco é armazenado por hash no servidor.
Sessões duram no máximo 12 horas. Alterações em projetos exigem origem correta e
token CSRF. Limpar cookies ou expirar a sessão remove o acesso pelo navegador;
o usuário deve salvar MP4/XML antes disso. O TTL dos arquivos não prolonga a sessão.

O banco `security.sqlite3` reside no mesmo volume persistente dos projetos. Faça
backup desse volume com a aplicação parada; preserve permissões e proteja a cópia
como um segredo. Os comandos e o login de contas nomeadas continuam disponíveis
para instalações anteriores, mas não são necessários para liberar o estúdio:

```bash
docker compose -f compose.staging.yml exec app python -m headbop.manage users list
docker compose -f compose.staging.yml exec app python -m headbop.manage users reset alice
docker compose -f compose.staging.yml exec app python -m headbop.manage users disable alice
```

Reset e desativação revogam as sessões existentes. Contas temporárias não aceitam
login por senha e não aparecem na lista de contas administradas.

## Migração sem perder projetos

Antes de atualizar, pare o serviço e faça uma cópia do volume `headbop_data`.
Não execute `down -v`. Projetos antigos não têm proprietário; eles permanecem no
disco e ficam inacessíveis pela API até atribuição explícita. Identifique o dono
correto e execute, para cada projeto:

```bash
docker compose -f compose.staging.yml exec app python -m headbop.manage projects adopt ID_DO_PROJETO alice
```

O comando não transfere projetos já pertencentes a outra pessoa. Esse procedimento
é destinado a contas nomeadas já existentes. Para continuar no estúdio livre,
importe o XML salvo. XML v1/v2 cria um novo projeto pertencente à sessão atual; arquivos XML
não podem escolher o proprietário. Projetos e mídias continuam temporários, com
retenção configurada por TTL. Os usuários devem exportar MP4/XML antes da expiração.

## Limites e isolamento

O Compose executa a aplicação sem root, com filesystem somente leitura, sem
capabilities, sem novos privilégios e com limites de 2 CPUs, 4 GiB de RAM e 128
processos. `/data` é persistente; `/tmp` tem limite de 512 MiB. A porta 8000 não é
publicada. Caddy termina TLS, sobrescreve cabeçalhos de endereço do cliente e limita
o corpo e o tempo de leitura. `--forwarded-allow-ips=*` só é apropriado nessa rede
privada: não publique a porta do backend nem conecte containers não confiáveis à rede.
Se houver CDN/proxy adicional, configure e teste explicitamente sua cadeia de confiança;
não aceite `X-Forwarded-For` arbitrário vindo da internet.

| Variável | Padrão da aplicação | Função |
| --- | --- | --- |
| `HEADBOP_ENV` | `production` | Requer origem HTTPS; `development` é explícito |
| `HEADBOP_PUBLIC_ORIGIN` | obrigatório em produção | Origem canônica, sem caminho |
| `HEADBOP_TTL_HOURS` | `24` | Retenção de projetos |
| `HEADBOP_MAX_PROJECTS` | `50` | Limite global; Compose usa 30 |
| `HEADBOP_USER_MAX_PROJECTS` | `10` | Projetos por conta ou rede de convidados |
| `HEADBOP_MAX_STORAGE_MIB` | `2048` | Disco global, com reservas concorrentes |
| `HEADBOP_USER_STORAGE_MIB` | `256` | Disco por conta ou rede de convidados |
| `HEADBOP_RENDER_TIMEOUT_SECONDS` | `300` | Prazo total do job; máximo 1800 s |
| `HEADBOP_MAX_VIDEO_MIB` | `64` | Limite de saída por vídeo |

A admissão reserva espaço antes de uploads/importação e renderização. Sessões de
convidados do mesmo IPv4 ou prefixo IPv6 /64 compartilham cotas, mesmo após limpar
cookies. O banco guarda um hash do escopo de rede, sem expor outros convidados.
Em redes compartilhadas, os visitantes dividem esse orçamento; isso não permite
acessar os projetos uns dos outros. A consulta de proprietários e a reserva são
serializadas, incluindo sessões criadas durante uma requisição concorrente.

A criação de convidados é limitada a 10 por hora por rede e 100 em cinco minutos
globalmente. Uma sessão já válida pode reabrir o estúdio sem consumir esse limite.
Análises são limitadas a 60 por hora e renders a 30 por hora por rede de convidados,
além dos limites de conta e IP atual. Há até duas análises e dois jobs de render
admitidos globalmente, com uma vaga de cada tipo por rede. Limites persistem em
SQLite entre reinícios. O supervisor executa renderização em processo separado,
encerra também descendentes, remove saída incompleta e libera a fila em falhas ou
timeout. A interface usa um prazo finito que inclui a espera por outro job na fila.
Jobs interrompidos por reinício são recuperados como erro e podem ser
reenviados. O progresso chega a 100% somente após a validação do MP4 final.

Esses limites reduzem abuso por sessões e requisições; não substituem proteção
contra DDoS na infraestrutura. Meça carga com a resolução/duração reais antes de
definir capacidade e limites de uso. Monitore disco do volume, memória, latência,
429, falhas de renderização e disponibilidade. Não registre mídia, senhas ou cookies.

## Validação e operação

```bash
ruff check .
python -m pytest -q
python scripts/browser_smoke.py
python scripts/i18n_browser_test.py
python scripts/brush_browser_test.py
python scripts/staging_smoke.py --image headbop-ai:public-studio
```

O relatório [PUBLIC_STUDIO_VALIDATION.md](PUBLIC_STUDIO_VALIDATION.md) registra as
validações desta alteração. [PR3_FIXES_VALIDATION.md](PR3_FIXES_VALIDATION.md) mantém
o histórico das correções anteriores. Testes locais de HTTPS não comprovam DNS,
emissão/renovação ACME ou firewall do servidor público. Antes de liberar o domínio:

- Verifique certificado público, renovação, redirecionamento HTTP→HTTPS e acesso
  externo negado à porta 8000.
- Teste backup/restauração, alertas e capacidade no host real.
- Valide Safari/iOS e Firefox reais, além do Chromium automatizado.
- Publique termos, política de privacidade/retenção, canal de suporte e orientações
  sobre direitos de imagem e áudio adequados à operação.
- Faça análise atualizada de dependências e da imagem de distribuição e registre
  eventuais CVEs aceitas ou corrigidas.

Para atualizar, preserve uma referência à imagem anterior e um backup do volume,
pare o serviço, faça backup, construa a nova imagem e execute o smoke autenticado.
Em rollback que anteceda o modelo de contas, **não exponha a aplicação antiga**:
ela não aplica autorização por usuário. Restaure a versão e o backup em acesso
restrito e confirme os testes antes de reabrir.
