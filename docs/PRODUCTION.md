# Implantação com contas individuais

A aplicação exige conta individual em todos os endpoints de projetos, mídia e
exportação. As contas são criadas pelo administrador: não há cadastro anônimo,
recuperação por e-mail nem dependência de um provedor externo de identidade.
Esta configuração atende uma única instância Linux, com uma fila de renderização
limitada. Não aumente `--workers` nem compartilhe o volume entre réplicas: o
controle de concorrência e as reservas de disco são locais ao processo.

## Docker, HTTPS e primeira conta

Requer Docker Engine/Compose, domínio com DNS apontando ao servidor e portas
TCP 80/443 disponíveis. Crie `.env` (ignorado pelo Git):

```dotenv
HEADBOP_DOMAIN=studio.exemplo.com
```

O nome `compose.staging.yml` foi mantido para preservar o caminho de implantação
existente. Ele agora usa autenticação individual da aplicação, sem senha
compartilhada do Caddy:

```bash
docker compose -f compose.staging.yml config --quiet
docker compose -f compose.staging.yml up -d --build
docker compose -f compose.staging.yml exec app python -m headbop.manage users create alice
```

Digite a senha de 12–128 caracteres no terminal. Ela não é passada em argumento,
variável de ambiente, log ou Git. Abra o endereço HTTPS e faça login. O cookie de
sessão é Secure, HttpOnly e SameSite=Strict; o token opaco é armazenado por hash
no servidor. Sessões duram no máximo 12 horas. Operações de escrita exigem origem
correta e token CSRF. Login é limitado por IP/conta; falhas não revelam se o usuário
existe. A senha usa scrypt com salt individual.

```bash
docker compose -f compose.staging.yml exec app python -m headbop.manage users list
docker compose -f compose.staging.yml exec app python -m headbop.manage users reset alice
docker compose -f compose.staging.yml exec app python -m headbop.manage users disable alice
```

Reset e desativação revogam as sessões existentes. O banco `security.sqlite3`
reside no mesmo volume persistente dos projetos. Faça backup desse volume com a
aplicação parada; a cópia deve preservar permissões e ser protegida como um segredo.

## Migração sem perder projetos

Antes de atualizar, pare o serviço e faça uma cópia do volume `headbop_data`.
Não execute `down -v`. Projetos antigos não têm proprietário; eles permanecem no
disco e ficam inacessíveis pela API até atribuição explícita. Identifique o dono
correto e execute, para cada projeto:

```bash
docker compose -f compose.staging.yml exec app python -m headbop.manage projects adopt ID_DO_PROJETO alice
```

O comando não transfere projetos já pertencentes a outra pessoa. XML v1/v2 continua
importável e cria um novo projeto pertencente ao usuário autenticado; arquivos XML
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
| `HEADBOP_USER_MAX_PROJECTS` | `10` | Projetos por conta |
| `HEADBOP_MAX_STORAGE_MIB` | `2048` | Disco global, com reservas concorrentes |
| `HEADBOP_USER_STORAGE_MIB` | `256` | Disco por conta |
| `HEADBOP_RENDER_TIMEOUT_SECONDS` | `300` | Prazo total do job; máximo 1800 s |
| `HEADBOP_MAX_VIDEO_MIB` | `64` | Limite de saída por vídeo |

A admissão reserva espaço antes de uploads/importação e renderização. Uma conta
não pode ocupar todas as vagas simultâneas. Limites de requisições persistem em
SQLite entre reinícios. O supervisor executa renderização em processo separado,
encerra também descendentes, remove saída incompleta e libera a fila em falhas ou
timeout. A interface usa um prazo finito que inclui a espera por outro job na fila.
Jobs interrompidos por reinício são recuperados como erro e podem ser
reenviados. O progresso chega a 100% somente após a validação do MP4 final.

Esses limites reduzem abuso por contas e requisições; não substituem proteção
contra DDoS na infraestrutura. Meça carga com a resolução/duração reais antes de
definir capacidade e número de contas. Monitore disco do volume, memória, latência,
429, falhas de renderização e disponibilidade. Não registre mídia, senhas ou cookies.

## Validação e operação

```bash
ruff check .
python -m pytest -q
python scripts/browser_smoke.py
python scripts/i18n_browser_test.py
python scripts/staging_smoke.py --image headbop-ai:pr3-fixes
```

O relatório [PR3_FIXES_VALIDATION.md](PR3_FIXES_VALIDATION.md) registra exatamente o
que foi executado nesta alteração. Testes locais de HTTPS não comprovam DNS,
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
