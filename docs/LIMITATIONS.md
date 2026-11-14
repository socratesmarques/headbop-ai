# Limitações do Headbop 2.0

## Referência e evidência visual

Nenhum vídeo de referência foi recebido ou encontrado entre os anexos disponíveis.
Não existe preset de referência ativo, medição de fidelidade a ele ou alegação de
reprodução profissional equivalente. A comparação reproduzível é entre o motor
original e o motor 2.0 com mídia sintética; ela permite inspecionar movimento e
junções, mas não valida estilo de um exemplo externo.

## Segmentação e anatomia

Haar do OpenCV reconhece alguns rostos frontais; não é um detector universal de
anime, perfil ou personagens. A máscara de fundo uniforme usa cor da borda e
componentes conectados. Pode recusar imagens com fundo complexo, múltiplos objetos,
transparência difícil ou cores semelhantes. Sua confiança expressa critérios da
heurística, não uma probabilidade calibrada de acerto.

PNG com alfa preserva um recorte fornecido pelo usuário. Uma máscara por si só não
identifica pescoço, ombros e tronco. O rig hierárquico depende de evidência facial
ou da seleção manual da cabeça dentro do personagem. Sem isso, usa uma camada;
sem máscara confiável, permite imagem inteira. O pescoço tem sobreposição para
reduzir frestas, mas movimentos extremos e contornos mal selecionados podem
expor artefatos. Inpainting do fundo não reconstrói áreas ocultas com precisão
semântica. Não há segmentação neural nem preenchimento generativo.

## Análise musical

BPM e batidas são estimativas. Meio tempo, tempo dobrado, rubato, polirritmos,
transientes suaves e trechos curtos são difíceis. Onsets/energia/silêncio auxiliam
o movimento; eventos low/high são heurísticas de banda, não detecção garantida
de kick ou snare. O ajuste de BPM cria grade regular; sem ajuste, v2 preserva os
instantes detectados. Silêncio completo e áudio sem ritmo mensurável são recusados.
Não há benchmark de precisão em um catálogo musical nem estimativa de emoção.

## Saída e custo

Apenas CPU; H.264/AAC com perdas, 24/30/60 fps, até 30 segundos e opções HD/Full HD.
60 fps não melhora uma imagem de origem pequena; também aumenta renderização e
arquivo. O primeiro uso do Librosa/Numba pode compilar rotinas. Benchmarks do
relatório são medições locais, não garantias para todo hardware. Fontes muito
panorâmicas são limitadas pelo lado longo; o preset não garante uma dimensão curta
em fontes de qualquer proporção. A fonte é ajustada sem recorte inicial, mas
rotação e deslocamento podem alcançar a borda do canvas.

## Compatibilidade, plataforma e operação

V1 continua importável e exportável pelo motor original. V2 exige a versão nova.
A máscara e os keyframes salvos reduzem diferenças de importação; mudanças futuras
na renderização/rigging podem alterar o resultado, então guarde o MP4 definitivo.
As instruções Windows/Kali não equivalem a testes executados nesses sistemas;
consulte o relatório. Tela/toque móveis são exercitados em Chromium emulado;
iPhone/Safari físico não foi validado.

Projetos são temporários e pertencem a uma sessão privada criada automaticamente
para o navegador. Não há cadastro, exigência de login ou backup automático. A sessão
dura até 12 horas; projetos têm TTL de 24 horas por padrão. Limpar cookies ou expirar
a sessão remove o acesso pelo navegador; salve o XML para continuar depois.
A cota de rede é compartilhada por visitantes no mesmo IP (ou /64 IPv6), de forma
que limpar cookies não contorna os limites. Redes compartilhadas podem atingir a
cota mesmo usando navegadores diferentes.

O pincel usa segmentação por cor/bordas guiada pelos traços, com processamento
limitado a 640 pixels no maior lado. Ele não reconhece semanticamente qualquer
objeto e pode precisar da borracha ou do modo manual em imagens ambíguas. Áreas
pintadas desconectadas se movem juntas, sem timelines independentes por parte.

O processo deve ter um worker, imposto por trava no diretório de dados; não há fila
distribuída. Renderizações interrompidas podem ser solicitadas novamente. Consulte
[PRODUCTION.md](PRODUCTION.md) e [PUBLIC_STUDIO_VALIDATION.md](PUBLIC_STUDIO_VALIDATION.md).
