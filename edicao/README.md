# Edição: headlines + corpo

`cortes.py` corta erros e silêncios e gera um vídeo final por headline (headline + corpo limpo).
Requer `ffmpeg`/`ffprobe`, e uma chave `GROQ_API_KEY` ou `OPENAI_API_KEY` em `~/.config/watch/.env` para transcrever.

```bash
python3 edicao/cortes.py transcrever HEADLINES.mov   # gera HEADLINES.transcricao.txt/.json
python3 edicao/cortes.py transcrever CORPO.mov
python3 edicao/cortes.py silencios CORPO.mov         # lista silêncios > 0,6 s
python3 edicao/cortes.py montar plano.json           # gera finais/head_XX.mp4
```

O `plano.json` (trechos a manter de cada headline e do corpo) é escrito a partir da transcrição; o formato está no topo de `cortes.py`.
