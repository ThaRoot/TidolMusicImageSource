"""
cli.py — Uso del formato .tmisc sin levantar el servidor.

  python cli.py encode audio.wav -o salida.tmisc [-c cover.png] [-m auto|lsb|append]
  python cli.py decode salida.tmisc -o recuperado.wav
  python cli.py info  archivo.tmisc
"""

import argparse
import sys

from PIL import Image

import tmisc


def cmd_encode(a):
    audio = open(a.audio, "rb").read()
    fmt = a.audio.rsplit(".", 1)[-1] if "." in a.audio else "bin"
    cover = Image.open(a.cover) if a.cover else None
    data, mode = tmisc.encode(audio, fmt, cover=cover, mode=a.mode)
    out = a.output or "output.tmisc"
    open(out, "wb").write(data)
    print(f"OK  modo={mode}  {len(audio)}B audio -> {len(data)}B  ({out})")


def cmd_decode(a):
    data = open(a.file, "rb").read()
    audio, fmt, mode = tmisc.decode(data)
    out = a.output or f"recovered.{fmt}"
    open(out, "wb").write(audio)
    print(f"OK  modo={mode}  formato={fmt}  -> {out} ({len(audio)}B)")


def cmd_info(a):
    data = open(a.file, "rb").read()
    try:
        audio, fmt, mode = tmisc.decode(data)
        print(f"TMISC válido | modo={mode} | audio={fmt} | {len(audio)}B")
    except tmisc.TmiscError as e:
        print(f"No es TMISC o dañado: {e}")


def main():
    p = argparse.ArgumentParser(prog="tmisc")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("encode")
    e.add_argument("audio")
    e.add_argument("-o", "--output")
    e.add_argument("-c", "--cover")
    e.add_argument("-m", "--mode", default="auto", choices=["auto", "lsb", "append"])
    e.set_defaults(func=cmd_encode)

    d = sub.add_parser("decode")
    d.add_argument("file")
    d.add_argument("-o", "--output")
    d.set_defaults(func=cmd_decode)

    i = sub.add_parser("info")
    i.add_argument("file")
    i.set_defaults(func=cmd_info)

    args = p.parse_args()
    try:
        args.func(args)
    except tmisc.TmiscError as ex:
        print(f"Error: {ex}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
