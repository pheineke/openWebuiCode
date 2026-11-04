"""Text out of attached documents (PDF, Word, PowerPoint, OpenDocument), so any model can read them.

Plain text and code files never come here: the app reads those itself.  Office formats are zip
files of XML; only the body text is taken (no headers, footnotes or speaker notes).
"""
import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

MAX_MEMBER = 64 * 1024 * 1024        # bytes read from one file inside an Office/OpenDocument zip

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
DRAW = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"
TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"

HINTS = {"xlsx": "Excel-Tabellen bitte als CSV speichern und die CSV-Datei anhängen.",
         "xls": "Excel-Tabellen bitte als CSV speichern und die CSV-Datei anhängen.",
         "ods": "Tabellen bitte als CSV speichern und die CSV-Datei anhängen.",
         "doc": "Altes Word-Format: bitte als .docx oder PDF speichern.",
         "ppt": "Altes PowerPoint-Format: bitte als .pptx oder PDF speichern."}


class Unsupported(Exception):
    pass


def text_of(name: str, data: bytes) -> dict:
    """{"text", "pages"} of a document; raises Unsupported with a message for the user."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if data[:5] == b"%PDF-":
        pages, unit = _pdf(data), "Page"
    elif ext == "pdf":
        raise Unsupported("Die Datei ist kein gültiges PDF.")
    elif ext in ("docx", "docm"):
        pages, unit = [_docx(data)], ""
    elif ext in ("pptx", "pptm"):
        pages, unit = _pptx(data), "Slide"
    elif ext == "odt":
        pages, unit = [_odf(data, slides=False)[0]], ""
    elif ext == "odp":
        pages, unit = _odf(data, slides=True), "Slide"
    else:
        raise Unsupported(HINTS.get(ext) or f"Dateityp {'.' + ext if ext else 'ohne Endung'} wird nicht unterstützt.")
    if not any(p.strip() for p in pages):
        raise Unsupported("Kein Text gefunden (gescanntes PDF?). Als Bild anhängen geht bei Modellen mit Bildeingabe."
                          if unit == "Page" else "Kein Text gefunden.")
    if len(pages) == 1:
        text = pages[0].strip()
    else:
        text = "\n\n".join(f"[{unit} {i}]\n{p.strip()}" for i, p in enumerate(pages, 1) if p.strip())
    return {"text": text, "pages": len(pages)}


# ---- PDF ----------------------------------------------------------------------------------------
def _pdf(data: bytes) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise Unsupported("PDF-Unterstützung fehlt: pypdf ist nicht installiert (requirements.txt).") from e
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise Unsupported("Das PDF ist passwortgeschützt.")
        return [page.extract_text() or "" for page in reader.pages]
    except Unsupported:
        raise
    except Exception as e:                    # pypdf raises many kinds of errors on broken files
        raise Unsupported(f"PDF konnte nicht gelesen werden: {e}") from e


# ---- Office Open XML ----------------------------------------------------------------------------
def _zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise Unsupported("Die Datei ist beschädigt oder kein Office-Dokument.") from e


def _xml(z: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        info = z.getinfo(name)
        if info.file_size > MAX_MEMBER:
            raise Unsupported("Das Dokument ist zu groß.")
        return ET.fromstring(z.read(info))
    except KeyError as e:
        raise Unsupported("Die Datei ist beschädigt oder kein Office-Dokument.") from e
    except ET.ParseError as e:
        raise Unsupported(f"Das Dokument konnte nicht gelesen werden: {e}") from e


def _docx_par(p: ET.Element) -> str:
    parts = []
    for el in p.iter():
        if el.tag == W + "t":
            parts.append(el.text or "")
        elif el.tag == W + "tab":
            parts.append("\t")
        elif el.tag in (W + "br", W + "cr"):
            parts.append("\n")
    return "".join(parts)


def _docx_block(el: ET.Element, out: list[str]) -> None:
    """Paragraphs in order; a table row becomes one line with its cells separated by |."""
    for c in el:
        if c.tag == W + "p":
            out.append(_docx_par(c))
        elif c.tag == W + "tbl":
            for tr in c.iter(W + "tr"):
                out.append(" | ".join(" ".join(_docx_par(p) for p in tc.iter(W + "p")) for tc in tr.findall(W + "tc")))
        else:                                 # content controls, sections, ...
            _docx_block(c, out)


def _docx(data: bytes) -> str:
    with _zip(data) as z:
        root = _xml(z, "word/document.xml")
    body = root.find(W + "body")
    out: list[str] = []
    _docx_block(body if body is not None else root, out)
    return "\n".join(out)


def _pptx(data: bytes) -> list[str]:
    with _zip(data) as z:
        try:                                  # slide order as in the presentation, not as the files are numbered
            rels = {r.get("Id"): r.get("Target") for r in _xml(z, "ppt/_rels/presentation.xml.rels").iter(REL + "Relationship")}
            names = [t.lstrip("/") if t.startswith("/") else posixpath.normpath(posixpath.join("ppt", t))
                     for s in _xml(z, "ppt/presentation.xml").iter(P + "sldId") if (t := rels.get(s.get(R + "id")))]
            names = [n for n in names if n in z.NameToInfo]
        except Unsupported:
            names = []
        if not names:
            names = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                           key=lambda n: int(re.findall(r"\d+", n)[-1]))
        slides = []
        for n in names:
            paras = ("".join(t.text or "" for t in p.iter(A + "t")) for p in _xml(z, n).iter(A + "p"))
            slides.append("\n".join(x for x in paras if x.strip()))
    return slides


# ---- OpenDocument -------------------------------------------------------------------------------
def _odf_text(el: ET.Element) -> str:
    out = [el.text or ""]
    for c in el:
        if c.tag == TEXT + "s":
            n = c.get(TEXT + "c") or "1"
            out.append(" " * (int(n) if n.isdigit() else 1))
        elif c.tag == TEXT + "tab":
            out.append("\t")
        elif c.tag == TEXT + "line-break":
            out.append("\n")
        elif c.tag not in (TEXT + "note", TEXT + "p", TEXT + "h"):   # footnotes out; nested paragraphs come on their own
            out.append(_odf_text(c))
        out.append(c.tail or "")
    return "".join(out)


def _odf_block(el: ET.Element, out: list[str]) -> None:
    """Paragraphs in order; a table row becomes one line with its cells separated by |."""
    for c in el:
        if c.tag in (TEXT + "p", TEXT + "h"):
            out.append(_odf_text(c))
        elif c.tag == TABLE + "table":
            for tr in c.iter(TABLE + "table-row"):
                out.append(" | ".join(" ".join(_odf_text(p) for p in tc.iter() if p.tag in (TEXT + "p", TEXT + "h"))
                                      for tc in tr.findall(TABLE + "table-cell")))
        else:                                 # lists, sections, frames, text boxes, ...
            _odf_block(c, out)


def _odf_paras(el: ET.Element) -> str:
    out: list[str] = []
    _odf_block(el, out)
    return "\n".join(out)


def _odf(data: bytes, slides: bool) -> list[str]:
    with _zip(data) as z:
        root = _xml(z, "content.xml")
    if slides:
        return [_odf_paras(page) for page in root.iter(DRAW + "page")]
    return [_odf_paras(root)]
