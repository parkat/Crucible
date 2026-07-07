Drop the classic Macintosh "Chicago" display face here for the System 3.0 dashboard skin.

Expected files (either or both — the CSS tries woff2 first, then ttf):
    ChicagoFLF.woff2
    ChicagoFLF.ttf

ChicagoFLF is a free TrueType clone of Chicago by Robin Casady (redistributable freeware).
Any Chicago-style bitmap face works — just name it ChicagoFLF.woff2 / ChicagoFLF.ttf.

Wiring is already in place:
  - server.py serves this dir at  /fonts/<name>
  - index.html @font-face  family "ChicagoFLF"  ->  /fonts/ChicagoFLF.woff2, /fonts/ChicagoFLF.ttf
  - the skin's --serif stack leads with "ChicagoFLF", so the face activates the moment a file lands here.

Until a file is present the skin falls back to the system stack (Charcoal / Geneva / system-ui).
