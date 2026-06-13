"""tailor.py: per-job ATS resume building (ADVANCED feature).

config/resume_template.txt is the user's resume with the literal marker
<tag> in its skills line. The bot swaps the marker with ONE confirmed
keyword (the user said "yes, I have that"). On skip it removes the
marker cleanly. When the original upload was a .docx (kept at
config/resume_template.docx) the output is a real .docx with the user's
formatting; otherwise plain .txt.

Per-run output layout: output/resumes/J01..J10 (and W1 for the wild
card), each holding a plainly-named resume.docx/.txt ready to upload
as-is, plus a job.txt identifying the posting.

Confirmed skills persist in config/approvedskills.txt: once you say you
have a skill, you're never asked about it again and it auto-applies when
a future job wants it.
"""
from __future__ import annotations

import io
import re
import zipfile
from xml.sax.saxutils import escape

from . import paths

MARKER = "<tag>"
XML_MARKER = "&lt;tag&gt;"          # how <tag> appears inside docx XML


def template() -> str:
    if not paths.RESUME_TEMPLATE.exists():
        raise RuntimeError(
            "config/resume_template.txt is missing; rerun python3 setup.py "
            "and say yes to the advanced setup")
    t = paths.RESUME_TEMPLATE.read_text()
    if MARKER not in t:
        raise RuntimeError(
            f"config/resume_template.txt has no {MARKER} marker; put {MARKER} "
            "inside the skills line where job keywords should go, e.g.\n"
            f"  SKILLS: Excel, {MARKER}, communication")
    return t


# ------------------------------------------------------ approved skills ----
def approved_raw() -> list[str]:
    """Approved skills as written (for display and matching)."""
    if paths.APPROVED.exists():
        return [l.strip() for l in paths.APPROVED.read_text().splitlines()
                if l.strip()]
    return []


def approved_list() -> set[str]:
    """Lowercased skills the user already confirmed having."""
    return {s.lower() for s in approved_raw()}


def approved_add(kw: str) -> None:
    """Remember a confirmed skill; never ask about it again."""
    if kw.strip() and kw.strip().lower() not in approved_list():
        lead = ""
        if paths.APPROVED.exists():     # hand-edited file may lack the
            text = paths.APPROVED.read_text()       # trailing newline
            if text and not text.endswith("\n"):
                lead = "\n"
        with paths.APPROVED.open("a") as f:
            f.write(lead + kw.strip() + "\n")


def approved_remove(kw: str) -> bool:
    """Forget a skill; True if it was on the list."""
    keep = [l for l in approved_raw()
            if l.lower() != kw.strip().lower()]
    if len(keep) == len(approved_raw()):
        return False
    paths.APPROVED.write_text("\n".join(keep) + ("\n" if keep else ""))
    return True


def on_resume(kw: str, text: str) -> bool:
    """Whole-word, case-insensitive: is this skill already in the text?
    (THE canonical copy; score.py imports it too.)"""
    return re.search(rf"(?i)(?<![A-Za-z0-9]){re.escape(kw.strip())}"
                     rf"(?![A-Za-z0-9])", text) is not None


# -------------------------------------------------------------- building ----
def strip_marker(text: str, marker: str) -> str:
    """Remove the marker and tidy the commas around it: the ONE place
    the comma rules live, for .txt and .docx alike."""
    for pat in (f", {marker}", f",{marker}", f"{marker}, ", f"{marker},",
                marker):
        text = text.replace(pat, "")
    return text


def render(choice: str | None) -> str:
    """Template text with <tag> replaced by the keyword (or removed cleanly)."""
    t = template()
    return t.replace(MARKER, choice) if choice else strip_marker(t, MARKER)


def render_docx(choice: str | None) -> bytes | None:
    """Same swap inside the ORIGINAL .docx (keeps all formatting). Returns
    None when there's no docx template or Word split the marker across
    XML runs; caller falls back to text output."""
    src = paths.RESUME_TEMPLATE_DOCX
    if not src.exists():
        return None
    try:
        zin = zipfile.ZipFile(src)
        xml = zin.read("word/document.xml").decode("utf-8")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    if XML_MARKER not in xml:
        return None
    xml = (xml.replace(XML_MARKER, escape(choice)) if choice
           else strip_marker(xml, XML_MARKER))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.namelist():
            zout.writestr(item, xml.encode("utf-8")
                          if item == "word/document.xml" else zin.read(item))
    return buf.getvalue()


def save(job: dict, choice: str | None, slot: str):
    """Build the per-job resume into output/resumes/<slot>/resume.docx
    (or .txt), plus a job.txt saying which posting the folder belongs
    to. The resume is named plainly so it uploads to applications
    as-is."""
    folder = paths.RESUMES / slot
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "job.txt").write_text(
        f"{job.get('title', '')} @ {job.get('company', '')}\n"
        f"{job.get('url', '')}\n"
        f"keyword: {choice or '(none; clean copy)'}\n")
    docx = render_docx(choice)
    if docx is not None:
        p = folder / "resume.docx"
        p.write_bytes(docx)
        return p
    p = folder / "resume.txt"
    p.write_text(render(choice))
    return p
