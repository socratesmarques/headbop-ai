# Internal integration contract

Historical v1 contract. For the current v2 extensions and compatibility rules, see
[ENGINE_V2_PLAN.md](ENGINE_V2_PLAN.md) and [ARCHITECTURE.md](ARCHITECTURE.md).

Current security additions supersede the historical unauthenticated examples:
individual login at `POST /api/auth/login`, session/CSRF at `GET /api/auth/session`,
logout at `POST /api/auth/logout`, and owned history at `GET /api/projects`.
Every project/media route requires the session cookie; writes also require the
configured `Origin` and `X-CSRF-Token`. Errors use `{code, params, detail?}`; known
media validation details are translated by the client. Render POST/GET return
`progress` and `timeout_seconds`, a finite budget including the bounded queue.
XML v1 and v2 are both accepted; ownership is always assigned by the server.

Python package `headbop`. UI is vanilla HTML/CSS/JS served at `/`, Portuguese copy.
No services besides FastAPI and FFmpeg. API errors JSON `{detail: string}`.

## API (implemented by integration owner)
- GET /api/health -> {status:'ok', ffmpeg:bool}
- POST /api/projects multipart `image`, `audio` -> project object (analysis synchronous).
- GET /api/projects/{id} -> project object.
- DELETE /api/projects/{id} -> 204.
- PUT /api/projects/{id}/settings JSON settings -> project (regenerates keyframes).
- POST /api/projects/{id}/segment JSON `{rect:[x,y,width,height]}` normalized 0..1 -> project.
- POST /api/projects/{id}/render -> {status:'queued'|'rendering'|'ready'|'error'}; schedules render.
- GET /api/projects/{id}/render -> {status, video_url: string|null, error:string|null}.
- GET /api/projects/{id}/video -> MP4 (exact preview/download bytes).
- GET /api/projects/{id}/export -> downloadable Headbop XML (embedded assets, settings, frames).
- POST /api/import multipart `file` -> project object; only Headbop XML v1 accepted.
- GET /api/projects/{id}/image and /foreground -> source/cutout PNG.

Project: `{id, width, height, duration, analysis:{bpm, beats:number[], intensity:number[]}, segmentation:{method, rect:[x,y,w,h]|null, available:bool, warning:string|null}, settings:{mode:'whole'|'subject',motion:'smooth'|'hard'|'velocity',bpm:number,intensity:number}, keyframes:[{time,x,y,rotation,scale}], image_url, foreground_url}`.
Settings bpm 40..240, intensity 0..2, default intensity 1, default mode whole/motion smooth.
Keyframes x/y normalized fractions of output width/height; rotation degrees; scale factor.
Video max 30s, uploads each <=20MiB, input image <=16 megapixels, output max dimension 640, 24fps.
Uploads normalize images via Pillow EXIF transpose to RGBA PNG, audio via FFmpeg to mono WAV 22050Hz.
All project assets are server paths and never accepted from the client.

## Media module interfaces
`audio.analyze_audio(path: Path) -> dict` contains bpm, beats, intensity, duration. Fail with ValueError for silence/invalid/too short (<0.5s) audio. Full duration limit enforced also by API.
`animation.generate_keyframes(duration:float, beats:list[float], intensity:list[float], bpm:float, strength:float, motion:str) -> list[dict]`. Uniform time samples at 24fps plus final duration. Settings BPM controls beat grid starting first detected beat; intensity array corresponds detected beats. x/y normalized; return exact sample transforms.
`imaging.segment_image(source:Path, destination:Path, rect:list[float]|None=None) -> dict` returns segmentation object above, PNG RGBA if available; automatic: preserve existing alpha if meaningful, otherwise try Haar face region / GrabCut; explicit normalized manual rectangle for any subject. Document no general automatic character recognition.
`renderer.render_video(image_path:Path, foreground_path:Path|None, audio_path:Path, output_path:Path, keyframes:list[dict], duration:float, mode:str) -> None`. Use MP4 libx264 yuv420p + AAC, 24fps, max dimension 640. Whole mode background neutral fill; subject mode original background with masked area inpainted then animated foreground. Transform uses frame keyframes; server preview is exported video.

## Format interfaces
`formats.export_project(project:dict,image_bytes:bytes,audio_bytes:bytes)->bytes` generates safe own XML with embedded normalized PNG/WAV; project includes id and metadata but do not expose server paths. `formats.import_project(data:bytes)->dict` returns `{settings:dict,image:bytes,audio:bytes}` after strict validation. Must reject external entities, oversized input, unexpected format, invalid settings, invalid base64. Do not claim Alight compatibility.

## Shared work boundaries
Integration owner: main.py, store.py, pyproject.toml, Dockerfile, CI, environment, API tests.
Audio agent: audio.py, animation.py, their tests.
Image agent: imaging.py, renderer.py, their tests.
Frontend agent: static/index.html, style.css, app.js only.
Formats/docs agent: formats.py, format tests, README.md, docs/ARCHITECTURE.md, docs/LIMITATIONS.md, LICENSE.
