# Headbop AI — plano de entrega

- [x] Inspecionar checkout vazio, Python 3.12, Node 24 e FFmpeg 7.
- [x] Implementar análise musical e geração determinística de keyframes.
- [x] Implementar recorte de cabeça/personagem e renderização MP4.
- [x] Integrar API FastAPI, armazenamento temporário e interface responsiva.
- [x] Implementar importação/exportação de projeto XML próprio com validação.
- [x] Validar unidades, API, navegador, segurança de entradas e build Docker.
- [x] Documentar instalação, arquitetura, limitações e reprodução do ambiente.
- [x] Registrar commits e verificar publicação em socratesmarques/headbop-ai.

Decisões: processamento local, sem APIs externas ou modelos baixados; Librosa para
áudio, OpenCV Haar/GrabCut para recorte com seleção manual; preview pelo mesmo MP4
exportado. Compatibilidade com XML Alight Motion não será alegada sem arquivo e
aplicativo reais para validar. O XML Headbop é um fallback portátil documentado.

Entrega publicada em `main`: https://github.com/socratesmarques/headbop-ai.
O commit inicial `76619e5` foi confirmado por consulta ao remoto após o push.
Não foi aberto PR porque o repositório estava vazio e não tinha branch de destino.
Instruções `install_script` e `start_skill` foram salvas no rascunho do ambiente;
a publicação do snapshot na interface e a restauração em outra tarefa não foram
executadas. A execução remota da CI ainda não foi consultada.
