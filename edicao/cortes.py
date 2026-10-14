#!/usr/bin/env python3
"""Edição de reels: headlines + corpo.

Fluxo:
  1. transcrever  -> transcrição com tempo de cada palavra (Groq ou OpenAI Whisper)
  2. silencios    -> lista dos silêncios do vídeo (ffmpeg silencedetect)
  3. (Claude lê a transcrição e escreve o plano.json com os trechos a manter)
  4. montar       -> corta erros e silêncios e gera um vídeo final por headline

Formato do plano.json:
{
  "saida": "finais",
  "silencio_min": 0.6,      # silêncios maiores que isso (s) são removidos do corpo
  "folga": 0.12,            # respiro mantido antes/depois da fala em cada corte (s)
  "corpo": {"arquivo": "corpo.mp4", "manter": [[0.0, 12.4], [15.1, 48.0]]},
  "heads": [
    {"nome": "head_01", "arquivo": "heads.mp4", "manter": [[3.2, 7.9]]},
    {"nome": "head_02", "arquivo": "heads.mp4", "manter": [[21.0, 25.6]]}
  ]
}
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import uuid

FADE = 0.015  # fade curtinho de áudio em cada corte, evita estalo


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def probe(path):
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_streams", "-show_format", path]).stdout
    data = json.loads(out)
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    num, den = v["r_frame_rate"].split("/")
    rot = 0
    for sd in v.get("side_data_list", []):
        rot = int(sd.get("rotation", 0)) or rot
    w, h = int(v["width"]), int(v["height"])
    if abs(rot) in (90, 270):
        w, h = h, w
    return {"w": w, "h": h, "fps": round(int(num) / int(den), 3),
            "dur": float(data["format"]["duration"])}


# ---------------------------------------------------------------- transcrição

def load_env():
    env = {}
    for p in (os.path.expanduser("~/.config/watch/.env"), ".env"):
        if os.path.exists(p):
            for line in open(p):
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    env.update({k: v for k, v in os.environ.items() if k.endswith("_API_KEY")})
    return env


def multipart(fields, file_field, file_path):
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{k}"\r\n\r\n{v}\r\n').encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
             f'filename="{os.path.basename(file_path)}"\r\n'
             "Content-Type: audio/mpeg\r\n\r\n").encode()
    body += open(file_path, "rb").read() + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def transcrever(args):
    env = load_env()
    if env.get("GROQ_API_KEY"):
        url, key, model = ("https://api.groq.com/openai/v1/audio/transcriptions",
                           env["GROQ_API_KEY"], "whisper-large-v3")
    elif env.get("OPENAI_API_KEY"):
        url, key, model = ("https://api.openai.com/v1/audio/transcriptions",
                           env["OPENAI_API_KEY"], "whisper-1")
    else:
        sys.exit("Sem GROQ_API_KEY nem OPENAI_API_KEY (~/.config/watch/.env).")

    with tempfile.TemporaryDirectory() as tmp:
        audio = os.path.join(tmp, "audio.mp3")
        run(["ffmpeg", "-y", "-i", args.video, "-vn", "-ac", "1", "-ar", "16000",
             "-b:a", "64k", audio])
        fields = [("model", model), ("language", "pt"),
                  ("response_format", "verbose_json"),
                  ("timestamp_granularities[]", "word"),
                  ("timestamp_granularities[]", "segment")]
        body, ctype = multipart(fields, "file", audio)
        req = urllib.request.Request(url, data=body, headers={
            "Authorization": f"Bearer {key}", "Content-Type": ctype})
        data = json.load(urllib.request.urlopen(req, timeout=600))

    base = args.out or os.path.splitext(args.video)[0]
    json.dump(data, open(base + ".transcricao.json", "w"), ensure_ascii=False, indent=1)
    with open(base + ".transcricao.txt", "w") as f:
        for s in data.get("segments", []):
            f.write(f"[{s['start']:7.2f} - {s['end']:7.2f}] {s['text'].strip()}\n")
    print(open(base + ".transcricao.txt").read())


# ---------------------------------------------------------------- silêncios

def detectar_silencios(video, minimo, ruido="-35dB"):
    err = subprocess.run(["ffmpeg", "-i", video, "-af",
                          f"silencedetect=noise={ruido}:d={minimo}", "-f", "null", "-"],
                         text=True, capture_output=True).stderr
    sil, ini = [], None
    for line in err.splitlines():
        if "silence_start:" in line:
            ini = float(line.split("silence_start:")[1].split()[0])
        elif "silence_end:" in line and ini is not None:
            sil.append((max(0.0, ini), float(line.split("silence_end:")[1].split()[0])))
            ini = None
    if ini is not None:
        sil.append((ini, probe(video)["dur"]))
    return sil


def silencios(args):
    for a, b in detectar_silencios(args.video, args.minimo, args.ruido):
        print(f"{a:7.2f} - {b:7.2f}  ({b - a:.2f}s)")


def tirar_silencios(trechos, sil, folga):
    """Remove de cada trecho os silêncios internos, deixando `folga` de respiro."""
    out = []
    for a, b in trechos:
        cur = a
        for s, e in sil:
            cs, ce = s + folga, e - folga
            if ce - cs <= 0 or ce <= cur or cs >= b:
                continue
            if cs > cur:
                out.append((cur, cs))
            cur = max(cur, ce)
        if b - cur > 0.05:
            out.append((cur, b))
    return out


# ---------------------------------------------------------------- montagem

def montar(args):
    plano = json.load(open(args.plano))
    raiz = os.path.dirname(os.path.abspath(args.plano))
    caminho = lambda p: p if os.path.isabs(p) else os.path.join(raiz, p)
    saida = caminho(plano.get("saida", "finais"))
    os.makedirs(saida, exist_ok=True)

    corpo_arq = caminho(plano["corpo"]["arquivo"])
    alvo = probe(corpo_arq)
    corpo = plano["corpo"]["manter"]
    if plano.get("silencio_min"):
        sil = detectar_silencios(corpo_arq, plano["silencio_min"], plano.get("ruido", "-35dB"))
        corpo = tirar_silencios(corpo, sil, plano.get("folga", 0.12))
    print(f"corpo: {len(corpo)} trechos, {sum(b - a for a, b in corpo):.1f}s")

    for head in plano["heads"]:
        partes = [(caminho(head["arquivo"]), a, b) for a, b in head["manter"]]
        partes += [(corpo_arq, a, b) for a, b in corpo]
        destino = os.path.join(saida, head["nome"] + ".mp4")
        renderizar(partes, alvo, destino)
        print(f"ok: {destino}")


def renderizar(partes, alvo, destino):
    arquivos = list(dict.fromkeys(p for p, _, _ in partes))
    entradas = []
    for f in arquivos:
        entradas += ["-i", f]
    w, h, fps = alvo["w"], alvo["h"], alvo["fps"]
    filtros, rotulos = [], ""
    for i, (f, a, b) in enumerate(partes):
        n = arquivos.index(f)
        d = b - a
        filtros.append(
            f"[{n}:v]trim={a}:{b},setpts=PTS-STARTPTS,"
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{i}]")
        filtros.append(
            f"[{n}:a]atrim={a}:{b},asetpts=PTS-STARTPTS,aresample=48000,"
            f"afade=t=in:d={FADE},afade=t=out:st={max(0, d - FADE)}:d={FADE}[a{i}]")
        rotulos += f"[v{i}][a{i}]"
    filtros.append(f"{rotulos}concat=n={len(partes)}:v=1:a=1[v][a]")
    run(["ffmpeg", "-y", *entradas, "-filter_complex", ";".join(filtros),
         "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium",
         "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", destino])


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(required=True)

    t = sub.add_parser("transcrever", help="transcrição com tempo por palavra")
    t.add_argument("video")
    t.add_argument("--out", help="prefixo dos arquivos gerados")
    t.set_defaults(func=transcrever)

    s = sub.add_parser("silencios", help="lista os silêncios do vídeo")
    s.add_argument("video")
    s.add_argument("--minimo", type=float, default=0.6)
    s.add_argument("--ruido", default="-35dB")
    s.set_defaults(func=silencios)

    m = sub.add_parser("montar", help="gera um vídeo final por headline")
    m.add_argument("plano")
    m.set_defaults(func=montar)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
