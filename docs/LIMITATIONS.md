# Limitações e verificações pendentes

## Alight Motion

Não há compatibilidade XML verificada com Alight Motion. Nenhum arquivo de
projeto real e nenhuma execução do aplicativo estiveram disponíveis para
validar seu esquema, unidades, curvas, referências de mídia ou versões. O
projeto não inventa um esquema atribuído a esse aplicativo e não renomeia o XML
Headbop como se fosse compatível.

O fallback implementado é **MP4 H.264/AAC** para vídeo e **XML Headbop v1** para
projeto portátil. Os testes exercitam arquivos Headbop completos com PNG e WAV,
não fixtures alegadamente extraídas do Alight Motion. A aceitação de MP4 por
uma versão específica de um editor externo também deve ser verificada nesse
editor. Não há transferência verificada de keyframes editáveis entre os dois.

## Ritmo

Estimativas de BPM e batidas podem errar em música sem percussão, rubato,
polirritmos, ataques muito suaves e trechos curtos. Meio tempo e tempo dobrado
são ambiguidades comuns. Há ajuste manual de BPM. A grade de animação assume
tempo constante, ancorado na primeira batida identificada; não acompanha uma
curva de BPM variável. Os testes usam sinais de referência sintéticos e não
estabelecem precisão estatística sobre um catálogo musical.

Silêncio e áudio muito curto não fornecem uma referência útil e são recusados.
A intensidade é relativa ao trecho e não representa volume percebido calibrado.

## Recorte de cabeça e personagem

O reconhecimento automático usa detector clássico de rosto frontal Haar do
OpenCV, não um detector geral de personagens. Perfil, cabelos grandes, oclusão,
ilustrações, animais e fundos complexos podem falhar. GrabCut estima a máscara
com base em cores e na região selecionada; bordas finas e cores semelhantes ao
fundo são difíceis. A correção disponível é redesenhar o retângulo, não pintar
uma máscara pixel a pixel. Um PNG com transparência fornece um recorte explícito
quando a seleção automática ou manual não for suficiente.

No modo de objeto recortado, a área original é preenchida por inpainting antes
de compor o movimento. O preenchimento pode deixar artefatos em fundos com
texturas, texto ou geometria detalhada. Não há reconstrução generativa de áreas
ocultas. A detecção clássica evita downloads de modelos e consumo de GPU, mas
não substitui modelos especializados em segmentação semântica.

## Vídeo e desempenho

Uploads de imagem e áudio são limitados a 20 MiB cada; imagem até 16 megapixels;
áudio entre 0,5 e 30 segundos. Imagens de entrada são normalizadas a no máximo
1600 pixels na maior dimensão. A saída usa até 640 pixels na maior dimensão e
24 fps. Isso é uma escolha explícita de custo para CPU e uso móvel. Não há
renderização 4K, processamento em lote, exportação de canal alpha em MP4 nem
extensão automática de áudio. O MP4 comprime imagem e áudio com perdas.

Latência depende de CPU, duração, dimensões e primeira compilação do Numba.
Não foi estabelecido um SLA ou benchmark para todos os aparelhos. A interface
responsiva pode ser exercitada com viewport móvel, mas isso não equivale a
validar câmera, codecs e reprodução em um iPhone físico/Safari real.

O XML incorpora mídias normalizadas, com limite de 20 MiB por mídia e 56 MiB por
documento. Ao reabrir XML, áudio e recorte são processados novamente; alterações nas
dependências podem modificar a estimativa e os keyframes regenerados. Preserve
o MP4 quando precisar de uma cópia visual definitiva.

## Operação e armazenamento

O aplicativo é voltado ao uso local ou em uma rede privada de confiança. Não há
autenticação, perfis, cobrança, organização por usuário nem autorização entre
projetos. Identificadores aleatórios evitam caminhos previsíveis, mas não
substituem autenticação. Não exponha uma instância compartilhada sem controles
de acesso e limites de recursos adequados.

O armazenamento é local e temporário, limitado a 50 projetos por padrão. Novos
projetos são recusados quando o uso já alcança 512 MiB; esse orçamento de admissão
é aproximado e pode ser ultrapassado por operações já aceitas. Há no máximo duas
análises simultâneas e duas renderizações aceitas (uma ativa e outra na fila).
A limpeza por prazo ocorre na criação de
projetos e por uma rotina de fundo a cada cinco minutos. Projetos expirados e
arquivos removidos não possuem backup
automático. A renderização e a coordenação em memória exigem um único processo
Uvicorn; não há fila distribuída nem recuperação de trabalhos após interrupção
do processo.

CI, build de contêiner, publicação GitHub e testes no navegador só devem ser
considerados executados quando houver um resultado registrado para a revisão
em questão. Ter arquivos de configuração não prova que serviços externos os
executaram.
