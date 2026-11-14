# Arquitetura do motor 2.0

A aplicação mantém FastAPI, UI HTML/CSS/JavaScript e processamento local em CPU.
Não há serviços/modelos externos obrigatórios nem novas dependências de execução.

1. `main.py` valida uploads, normaliza imagem/áudio e coordena projetos temporários.
   FFmpeg aceita apenas demuxers de áudio permitidos, sem playlists ou URLs remotas.
2. `audio.py` mantém BPM/batidas/intensidade e mede onsets, energia contínua,
   intervalos de silêncio e eventos espectrais low/high. Esses eventos não são
   classificações garantidas de kick/snare.
3. `imaging.py` preserva alfa, tenta rosto frontal e testa um fundo uniforme com
   evidências conservadoras. GrabCut e seleção manual permanecem disponíveis.
4. `rigging.py` produz camadas RGBA de fundo, corpo e cabeça, com pivôs normalizados.
   Uma máscara confiável sem anatomia identificada permanece como camada única.
   O pescoço é uma região de sobreposição; não há detecção semântica de cada osso.
5. `animation.py` mantém `generate_keyframes` original intacta. `generate_motion`
   gera poses independentes de corpo e cabeça, com antecipação, acento musical e
   recuperação. Easing Bézier inverte a coordenada temporal da curva. Velocidade
   modifica a largura temporal dos gestos, preservando os instantes das batidas.
6. `renderer.py` usa composição de transformações `corpo @ cabeça` em torno dos
   pivôs. Alfa pré-multiplicado evita bordas escuras. Imagens são ajustadas ao
   canvas e os frames são transmitidos ao FFmpeg, sem guardar o vídeo inteiro em
   memória. Um arquivo temporário é substituído atomicamente ao terminar.
7. A interface reproduz esse MP4, e o download usa o mesmo arquivo; não há um
   simulador JavaScript concorrente que possa divergir do resultado exportado.

## Compatibilidade explícita

`settings.py` diferencia os motores `legacy` e `v2`. Ausência de `engine` em um
projeto existente significa `legacy`, nunca uma migração implícita. As opções
originais continuam disponíveis. O usuário pode escolher atualizar o motor,
regenerando a timeline; abrir/exportar o projeto antigo preserva sua configuração.

`formats.py` aceita XML Headbop v1/v2 com esquema estrito e sem DTDs/entidades.
V1 mantém a estrutura original; v2 adiciona settings, transformações por camada,
região de cabeça, máscara incorporada e metadados limitados de segmentação.
Paths do servidor nunca vêm do XML. Ao importar, a aplicação valida as mídias,
confere a duração do áudio e restaura os keyframes validados. Isso também preserva
edições da timeline feitas em um arquivo de projeto. Novas alterações nos controles
regeneram os keyframes com o motor selecionado.

## Concorrência e limites

Um worker Uvicorn, no máximo duas análises simultâneas e uma renderização ativa
com outra aceita na fila. Operações de edição são recusadas durante a renderização
para impedir mistura de configurações. A expiração não remove projetos em uso.
Uma trava de processo no diretório de dados impede múltiplos workers de contornar
as reservas. Cotas de projetos e disco são verificadas por conta e globalmente,
com reserva atômica antes da leitura de uploads e da entrada na fila. Cada conta
tem no máximo uma análise e um render pendentes. O vídeo tem até 30 segundos/1800
frames; resolução alta é opcional.

`jobs.py` supervisiona um processo `worker.py` por render. O prazo total inclui
preparação nativa, escrita no FFmpeg, finalização e validação com ffprobe. Timeout,
cancelamento e falha encerram o grupo de processos, removem a saída incompleta e
liberam a fila. O arquivo de saída tem limite de tamanho imposto ao processo.
O progresso usa os quadros enviados e reserva a conclusão para a saída validada.

`auth.py` mantém contas, hashes scrypt, sessões opacas revogáveis e limites de
requisições em SQLite. `security.py` aplica origem, CSRF e admissão. `.owner` associa
cada projeto à sua conta; IDs de outro usuário recebem 404 em todas as rotas.
Projetos antigos sem dono só ficam acessíveis após adoção explícita pela CLI.
`auth.js` abre uma sessão anônima ao entrar no estúdio e apresenta o histórico
privado desse navegador. A home não cria sessões para visitantes que só leem.
Sessões de convidados na mesma rede compartilham cotas de disco/projetos, limites
de operações e admissão; a associação é metadado privado, não autorização de mídia. `i18n.js`
usa bindings explícitos para textos e atributos, sem MutationObserver contínuo.

## UI e API

APIs anteriores preservadas. Novos projetos recebem settings v2 e modo automático;
O frontend abre o pincel após a análise e aguarda uma seleção explícita antes do render. GET project fornece `rig`,
`mask_url` e `capabilities.reference_available=false`. POST `/api/projects/{id}/rig`
recebe `head_rect` normalizado. O controle manual do personagem inteiro continua
em POST `/api/projects/{id}/segment`. PUT settings invalida o vídeo e regenera a
timeline. Exportar um projeto sem editar controles não recalcula seus frames.

## Pincel inteligente

`brush.js` mantém traços normalizados com mouse, toque ou teclado, incluindo
borracha, desfazer/refazer e tamanho. A API só é chamada ao aplicar, com até 100
traços/5000 pontos e corpo limitado a 256 KiB. `painting.py` combina sementes de
foreground/background com GrabCut e filtra componentes não marcados, mantendo alfa
e cores da origem. O modo manual rasteriza os traços sem expandi-los.

POST `/api/projects/{id}/paint` aceita `{strokes,smart,base}`; `base` é `empty`
ou `current` para refinar uma máscara salva. Máscara, rig e metadados são preparados
antes da troca dos arquivos; falhas restauram a seleção e o vídeo anteriores.
Pintura usa uma camada única até articulação explícita. Máscaras pintadas exportam
XML v2, inclusive no motor `legacy`, para preservar a seleção arbitrária.
