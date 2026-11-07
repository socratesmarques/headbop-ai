# Headbop 2.0 — plano e contrato incremental

Base: `a13bb33`; branch `feat/headbop-2-engine`. Nenhum vídeo de referência foi
encontrado nos anexos ou checkout. Não implementar preset chamado referência sem
comparação real. Main e configuração publicada do ambiente serão preservadas.

- [x] Inspecionar base, anexos e acesso remoto.
- [x] Enriquecer DSP (onsets, energia contínua, silêncio, eventos por bandas).
- [x] Adicionar motor v2 com easing, antecipação, recuperação e camadas.
- [x] Aprimorar segmentação conservadora e rigging com fallback manual.
- [x] Renderizar 24/30/60fps, 640/720/1080 e source/square/vertical.
- [x] Preservar XML v1 e projetos antigos; adicionar XML v2 validado.
- [x] Integrar UI automática, máscaras, pivôs e controles opcionais.
- [x] Validar unidades, API, navegador, renders CPU e Docker.
- [x] Documentar Linux/Kali, Windows, decisões e limitações.
- [x] Publicar a branch de revisão, preservando main.
- [x] Investigar e corrigir a falha de reprodução na CI; execução remota aprovada.
- [ ] Abrir PR; criação bloqueada pela política de rede, estado em [REPORT_V2.md](REPORT_V2.md).

Git fetch funciona. A API `api.github.com` respondeu CONNECT 403 pela política de
rede. A página pública de Actions permitiu confirmar uma falha anterior no teste
de navegador, mas não sua causa. Criação de PR depende do acesso à API.

## Contrato de integração v2 (mantém APIs anteriores)

Settings: campos existentes mode, motion, bpm, intensity; motion aceita auto,
smooth, hard, velocity. Novos campos: engine='v2'|'legacy', fps=24|30|60,
resolution=640|720|1080, aspect='source'|'square'|'vertical',
speed=0.5..2, easing='natural'|'linear'|'ease-in-out', squash=0..1.
640 limita o maior lado; 720/1080 miram o lado curto, limitando o maior a
1280/1920. Quadrado usa 720/1080 por lado e vertical usa 720×1280/1080×1920.
Defaults novos: engine v2, motion auto, fps24, resolution640, aspect source,
speed1, easing natural, squash0.25. Projetos/XML antigos usam engine legacy e
settings defaults equivalentes ao motor original. Não expor reference como preset.

`audio.analyze_audio` mantém os campos existentes e adiciona:
onsets:number[], energy:[{time,value}], silences:[[start,end]],
events:[{time,kind:'low'|'high',strength}], rhythm_confidence:0..1.
Eventos low/high são heurísticos, não classificação garantida kick/snare.

`animation.generate_keyframes` permanece INALTERADA para compatibilidade.
Nova `generate_motion(duration, analysis, settings, rig=None) -> list[dict]`.
Cada frame contém time,x,y,rotation,scale,scale_x,scale_y e
layers:{body:{x,y,rotation,scale,scale_x,scale_y}, head:{...}}.
Transformação head é relativa ao corpo (composição parent @ child); normalized
x/y relativos às dimensões da imagem; pivôs definidos pelo rig e rotação OpenCV.
Tempos amostrados no fps selecionado, com frame final em duration.

`imaging.segment_image` mantém assinatura e chaves, adiciona confidence0..1,
head_rect normalizado|null e kind:'head'|'character'|'unknown'. Pode estimar
foreground em fundo uniforme, sem alegar detector semântico. Fall back com warning.

`rigging.build_rig(image_path, foreground_path, output_dir, segmentation,
head_rect=None) -> dict`: retorna available, kind='head-body'|'single'|'whole',
confidence, warning, head_rect, pivots:{body:[x,y],head:[x,y]} normalizados e
layers:{background:'rig-background.png',body:'rig-body.png',head:'rig-head.png'}
quando disponíveis. Camadas canvas inteiro. Caminhos são basenames fixos internos.
Manter sobreposição em pescoço e corpo; preferir fallback a partes sem evidência.

`renderer.render_video` mantém parâmetros posicionais e adiciona keyword-only
settings=None, rig=None. Usa rig layers relativas à pasta do image_path,
composição hierárquica e fallback whole/single. Legacy sem novos kwargs mantém
comportamento existente. Fit sem cortar para square/vertical; fundo neutro.

API GET project adiciona rig e mask_url (=foreground_url), capabilities incluindo
reference_available:false. POST /api/projects/{id}/rig JSON {head_rect:[x,y,w,h]}
reconstrói rig, frames e invalida vídeo. POST segment reconstrói rig também.
UI gera render automaticamente após upload novo, com modo subject se recorte
confiável; whole caso contrário. Exporta mesmo MP4 de preview.

XML: v1 import/export preservados; v2 serializa novos settings, head_rect do rig e
frames incluindo layers, máscara PNG e metadados de segmentação. Import devolve
schema_version, settings,image,audio, rect opcional, head_rect opcional, foreground,
segmentation_info e frames validados. API valida duração da mídia e dimensões da
máscara e restaura frames importados para fidelidade (não executa caminhos do XML).
Projetos antigos recebem metadados conservadores apenas na atualização explícita
do rig; leitura e exportação v1 continuam sem migração.

## Divisão de arquivos

DSP/motion: audio.py, animation.py, testes correspondentes.
Imaging/rig: imaging.py, rigging.py, testes correspondentes.
Renderer: renderer.py, tests/test_renderer.py, script de comparação render.
Frontend: static/*, scripts/browser_smoke.py.
Integração: main.py, settings.py, formats.py, testes API/XML, docs/CI/empacotamento.
