# Headbop AI — checklist de publicação

> **Status: pré-produção / staging privado.** Este exemplo exige autenticação
> compartilhada para demonstrações com um grupo pequeno e confiável. Não é
> configuração para um serviço público com contas individuais.

## Implantação de staging com Docker e HTTPS

Pré-requisitos: servidor Linux com Docker Engine e plugin Docker Compose,
domínio apontando para o IP público do servidor e portas TCP 80 e 443
disponíveis. O processamento usa CPU e FFmpeg; não é indicado para hospedagem
estática como GitHub Pages.

1. Clone o repositório e entre no diretório. Confirme que o Docker funciona
   com `docker compose version`.
2. Crie um hash de senha localmente sem colocá-la no histórico do shell:

   ```bash
   docker run --rm -it caddy:2 caddy hash-password
   ```

   Digite uma senha forte quando solicitado. Copie somente o **hash**.

3. Crie `.env` na raiz (arquivo já ignorado pelo Git):

   ```dotenv
   HEADBOP_DOMAIN=studio.exemplo.com
   HEADBOP_BASIC_AUTH_HASH='$2a$14$SUBSTITUA_PELO_HASH_COMPLETO'
   ```

   Mantenha as aspas simples para não interpolar os caracteres `$` do hash.
   Use o domínio real no lugar do exemplo. A senha não deve ser enviada
   para o GitHub.

4. Valide a configuração e suba os contêineres:

   ```bash
   docker compose -f compose.staging.yml config --quiet
   docker compose -f compose.staging.yml up -d --build
   docker compose -f compose.staging.yml ps
   docker compose -f compose.staging.yml logs -f --tail=100
   ```

5. Acesse `https://studio.exemplo.com`, autentique-se com o usuário
   **tester** e a senha escolhida. Teste com imagem+áudio reais:
   análise, barra de progresso, preview com áudio, MP4, exportação/importação
   XML, troca de idioma, limpeza da sessão e navegadores móveis.

O Caddy gerencia HTTPS se o DNS estiver correto e as portas estiverem
acessíveis. O backend **não publica a porta 8000** no host; ela é acessível
somente pelos serviços da rede Docker. O contêiner de aplicação roda com
UID sem privilégios, diretório de dados persistente, sistema de arquivos
raiz somente leitura e limites iniciais de CPU, memória e processos.
Aumente os limites somente após monitorar renderizações reais de 1080p
e 60 FPS. O serviço usa deliberadamente **um worker**, pois a fila e
seu estado estão em memória.

O volume `headbop_data` guarda temporariamente os projetos entre reinícios
do contêiner, mas a aplicação remove projetos após o TTL. **Não considere
isso uma solução de backup.** O MP4 e o XML devem ser baixados pelo usuário
antes da limpeza. Use `docker compose -f compose.staging.yml down` para parar;
não use `down -v` a menos que queira excluir os volumes.

## O que falta antes de abrir para qualquer pessoa

1. **Autenticação e isolamento por usuário:** contas/sessões, autenticação
   segura, autorização em todos os endpoints de projeto e políticas
   de expiração. IDs aleatórios de projeto e uma senha compartilhada
   não equivalem a isolamento.
2. **Antiabuso e cotas:** limitação de requisições por IP/conta, limite de
   uploads/renderizações simultâneos, cotas de CPU/disco por pessoa,
   proteção contra picos e fila justa. Hoje a fila é local e curta.
3. **Persistência e escalabilidade:** separar o processamento pesado para
   workers, persistir estados de jobs, recuperação de tarefas interrompidas,
   métricas de fila e limpeza garantida. Uma única instância é adequada
   apenas para tráfego inicial controlado.
4. **Privacidade e conformidade:** política de privacidade, retenção e
   exclusão de arquivos, termos de uso, contato de suporte e direitos
   sobre imagens/músicas enviadas. Avalie LGPD e leis aplicáveis.
5. **Segurança:** revisar dependências e CVEs, validação de arquivos,
   isolamento do FFmpeg, limite de tempo, egress, logging sem dados de
   mídia e monitoramento de falhas. Guardar segredos fora do repositório.
6. **Qualidade:** testes automatizados, smoke real no celular, testes de
   carga de imagens/áudios de limite, i18n revisado por nativos e
   acessibilidade (teclado, leitores de tela, contraste).
7. **Entrega:** domínio, ícone/favicons, página de erro/404, textos
   institucionais, analytics com respeito à privacidade, monitoramento
   de uptime, alertas e plano de rollback.
8. **Performance:** avaliar custos por render e limites de preço/uso.
   Publicar primeiro como beta com acesso controlado antes de divulgar.

## Checklist de release

- [ ] `ruff check .`, `pytest -q` e `python scripts/browser_smoke.py` passam.
- [ ] CI do GitHub verde e imagem Docker construída com sucesso.
- [ ] HTTPS válido, autenticação exigida em todas as rotas no staging.
- [ ] Acesso direto à porta 8000 bloqueado externamente.
- [ ] Testes de vídeo e XML feitos no Firefox, Chrome e Safari/iOS.
- [ ] Tempo de render, uso máximo de RAM e espaço em disco monitorados.
- [ ] Procedimento de atualização e rollback documentado.
- [ ] Nenhuma afirmação de serviço público/multiusuário até concluir
      autenticação, autorização, rate limit e cotas.

Documentação de referência:
- https://caddyserver.com/docs/automatic-https
- https://caddyserver.com/docs/caddyfile/directives/basic_auth
- https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation
