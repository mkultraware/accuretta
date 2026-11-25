# SPDX-License-Identifier: AGPL-3.0-only
"""Reading PDF text page by page, with a clear note for scanned PDFs that hold no text
layer.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os


# Cap on extracted PDF text; matches the 64 KB binary cap used elsewhere.
_PDF_TEXT_CAP = 64 * 1024


def _extract_pdf_text(path: str) -> dict:
    """Pull a text rendering out of a PDF for the model to read.

    Tries pypdf, then pdfplumber (better with table-like layouts), then fails
    with an install hint. Scanned (image-only) PDFs have no text layer; that is
    flagged explicitly so the model suggests OCR instead of reporting an empty
    file."""
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        try:
            # very old envs sometimes only have PyPDF2 — same API
            from PyPDF2 import PdfReader  # type: ignore
        except Exception:
            PdfReader = None  # type: ignore

    pages_text: list[str] = []
    page_count = 0
    backend_used = ""
    last_err = ""

    if PdfReader is not None:
        try:
            reader = PdfReader(path)
            page_count = len(reader.pages)
            for i, page in enumerate(reader.pages):
                try:
                    t = page.extract_text() or ""
                except Exception as e:
                    t = f"[page {i + 1}: extraction error — {e}]"
                if t.strip():
                    pages_text.append(f"--- page {i + 1} ---\n{t.rstrip()}")
                # short-circuit if we already blew the cap
                if sum(len(p) for p in pages_text) > _PDF_TEXT_CAP:
                    break
            backend_used = "pypdf"
        except Exception as e:
            last_err = f"pypdf failed: {e}"

    # If pypdf gave us nothing usable, try pdfplumber (better with tables).
    if not pages_text:
        try:
            import pdfplumber  # type: ignore
            with pdfplumber.open(path) as pdf:
                page_count = len(pdf.pages)
                for i, page in enumerate(pdf.pages):
                    try:
                        t = page.extract_text() or ""
                    except Exception as e:
                        t = f"[page {i + 1}: extraction error — {e}]"
                    if t.strip():
                        pages_text.append(f"--- page {i + 1} ---\n{t.rstrip()}")
                    if sum(len(p) for p in pages_text) > _PDF_TEXT_CAP:
                        break
            backend_used = "pdfplumber"
        except ImportError:
            pass
        except Exception as e:
            last_err = (last_err + " · " if last_err else "") + f"pdfplumber failed: {e}"

    if PdfReader is None and not backend_used:
        return {
            "error": (
                "PDF text extraction needs pypdf. Install with:\n"
                "  pip install pypdf\n"
                "(optional, better for tables: pip install pdfplumber)"
            )
        }

    text = "\n\n".join(pages_text).strip()
    if not text:
        # Empty extract usually means a scanned/image-only PDF, or the PDF
        # was malformed. Either way the model needs to know it's not blank.
        msg = (
            f"[no extractable text — PDF appears to be scanned (image-only) "
            f"or has no text layer; {page_count} page(s) found]"
        )
        if last_err:
            msg += f"\n[extractor note: {last_err}]"
        return {
            "path": path,
            "content": msg,
            "truncated": False,
            "size": os.path.getsize(path),
            "pdf": {"pages": page_count, "backend": backend_used or "none", "empty": True},
        }

    truncated = len(text) > _PDF_TEXT_CAP
    if truncated:
        text = text[:_PDF_TEXT_CAP] + "\n\n[... PDF truncated to 64 KB ...]"
    return {
        "path": path,
        "content": text,
        "truncated": truncated,
        "size": os.path.getsize(path),
        "pdf": {"pages": page_count, "backend": backend_used, "empty": False},
    }
