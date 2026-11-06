# Arquitetura

## Fluxo principal

```text
Imagem + áudio
    │ validação, normalização e armazenamento privado
    ├── Pillow → PNG RGBA até 1600 pixels/lado, orientação EXIF aplicada
    ├── FFmpeg → WAV mono 22050 Hz
    ├── Librosa → BPM, batidas, intensidade
    └── OpenCV → máscara alpha / rosto / seleção manual
                    │
Configurações → keyframes amostrados a 24 fps
                    │
       composição de quadros + FFmpeg H.264/AAC
                    │
              MP4 único
                ├── preview HTML video
                └── download
```

## Organização

| Módulo | Responsabilidade |
| --- | --- |
| `headbop/main.py` | Rotas FastAPI, validação de uploads e orquestração |
| `headbop/store.py` | Identificadores, metadados, expiração e exclusão |
| `headbop/audio.py` | Análise musical local |
| `headbop/animation.py` | Transformações determinísticas amostradas no tempo |
| `headbop/imaging.py` | Detecção de rosto e recorte alpha/GrabCut |
| `headbop/renderer.py` | Composição e codificação do vídeo |
| `headbop/formats.py` | Importação/exportação XML estrita |
| `headbop/static/` | Interface HTML/CSS/JavaScript sem build Node |
| `tests/` | Testes de unidades, mídia, formatos e API |

Não há banco de dados. Cada projeto tem seu próprio diretório e metadados JSON.
Isso mantém instalação e operação proporcionais ao uso local; a coordenação
atual pressupõe um único processo de aplicação. Processos vivos não são
preservados ao restaurar um ambiente: o Uvicorn deve ser iniciado novamente.

## Movimento e fidelidade

Keyframes contêm `time` em segundos, deslocamentos `x` e `y` como frações da
largura/altura, `rotation` em graus e `scale` como multiplicador. Há amostras a
24 fps e uma amostra final no fim da duração. O BPM ajustado determina a grade
de pulsos a partir da primeira batida identificada; intensidade musical modula
o movimento. A intensidade configurada pelo usuário escala sua amplitude.

O renderer consome essas mesmas transformações. A interface reproduz o MP4
finalizado; mudar controles exige renderizar novamente. Não há um preview em
canvas com interpolação diferente da saída. A reprodução e a exibição das
cores ainda dependem do navegador e do dispositivo, e H.264/AAC são formatos
com perdas.

## XML Headbop v1

O formato é próprio e explicitamente diferente do formato de Alight Motion.
Ele não contém caminhos no servidor, URLs remotas nem instruções executáveis.

```xml
<?xml version='1.0' encoding='utf-8'?>
<headbop-project version="1">
  <settings mode="subject" motion="smooth" bpm="120" intensity="1" />
  <segmentation x="0.2" y="0.1" width="0.4" height="0.5" />
  <assets>
    <image encoding="base64" mime="image/png">…</image>
    <audio encoding="base64" mime="audio/wav">…</audio>
  </assets>
  <timeline fps="24" duration="5">
    <keyframe time="0" x="0" y="0" rotation="0" scale="1" />
    <!-- Demais amostras da animação -->
  </timeline>
</headbop-project>
```

O exemplo ilustra a estrutura; as reticências não são base64 válido. O exportador
gera documentos completos, sem esses marcadores. `segmentation` é opcional:
preserva o retângulo de recorte quando ele existe. Imagens com alpha preservam
sua máscara na própria imagem PNG. Cada mídia incorporada tem limite de 20 MiB;
o documento completo tem limite de 56 MiB. O exportador inclui no máximo 722
amostras de transformação para a duração limitada a 30 segundos.

O importador usa `defusedxml`, rejeita DTDs, entidades, formatos desconhecidos,
versões diferentes, seções duplicadas, conteúdo obrigatório ausente, base64
inválido e valores numéricos fora dos limites. Assinaturas PNG/WAV são checadas,
e a API decodifica e valida as mídias novamente. Os keyframes do arquivo são
validados; a aplicação **reanalisa o áudio e regenera a animação** a partir das
configurações. Isso evita executar transformações arbitrárias como dados
confiáveis, mas atualizações de bibliotecas/algoritmos podem mudar o resultado.
O MP4 é o artefato adequado para preservar a aparência final exata.

## Decisões de implantação

FastAPI oferece validação tipada e API documentada sem uma camada adicional de
servidor. JavaScript nativo atende aos controles e uploads sem uma cadeia de
build frontend. Librosa e OpenCV usam bibliotecas locais conhecidas; não há
credenciais de terceiros. FFmpeg cuida de formatos de entrada e codificação de
saída. O limite de 30 segundos, a saída de 640 pixels e a execução de renderização
controlada limitam o custo, mas não substituem isolamento e quotas em servidores
compartilhados.
