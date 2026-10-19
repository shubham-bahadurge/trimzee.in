"""A tiny dependency-free PDF writer: a title and a table of text (built-in Helvetica font)."""


def _esc(text):
    text = str(text if text is not None else "").encode("latin-1", "replace").decode("latin-1")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(title, subtitle, headers, rows, widths):
    """widths: relative column widths. Returns the PDF as bytes (A4 portrait, many pages if needed)."""
    page_w, page_h, margin, size, lead = 595, 842, 36, 8.5, 14
    usable = page_w - 2 * margin
    total = float(sum(widths))
    cols = [usable * w / total for w in widths]
    xs = [margin + sum(cols[:i]) for i in range(len(cols))]

    def fit(text, width):
        max_chars = max(1, int(width / (size * 0.52)))
        s = str(text if text is not None else "")
        return s if len(s) <= max_chars else s[:max_chars - 1] + "..."

    def row_ops(y, cells, bold):
        font = "F2" if bold else "F1"
        return "".join(f"BT /{font} {size} Tf {xs[i] + 2:.1f} {y:.1f} Td ({_esc(fit(c, cols[i] - 4))}) Tj ET\n"
                       for i, c in enumerate(cells))

    pages, ops, y = [], "", 0
    def new_page():
        nonlocal ops, y
        ops = "BT /F2 16 Tf %d %d Td (%s) Tj ET\n" % (margin, page_h - margin, _esc(title))
        ops += "BT /F1 9 Tf %d %d Td (%s) Tj ET\n" % (margin, page_h - margin - 16, _esc(subtitle))
        y = page_h - margin - 42
        ops += f"0.93 g {margin} {y - 4:.1f} {usable} {lead} re f 0 g\n" + row_ops(y, headers, True)
        y -= lead
    new_page()
    for r in rows:
        if y < margin + lead:
            pages.append(ops); new_page()
        ops += row_ops(y, r, False)
        y -= lead
        ops += f"0.85 G {margin} {y + lead - 3:.1f} m {margin + usable} {y + lead - 3:.1f} l S 0 G\n"
    if not rows:
        ops += row_ops(y, ["No records yet."] + [""] * (len(cols) - 1), False)
    pages.append(ops)

    objs = [None] * (4 + 2 * len(pages))          # 1 catalog, 2 pages, 3 font, 4 bold font, then page/content pairs
    kids = []
    for i, content in enumerate(pages):
        page_id, content_id = 5 + 2 * i, 6 + 2 * i
        kids.append(f"{page_id} 0 R")
        objs[page_id - 1] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w} {page_h}] "
                             f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>")
        data = content.encode("latin-1", "replace")
        objs[content_id - 1] = (b"<< /Length %d >>\nstream\n" % len(data)) + data + b"\nendstream"
    objs[0] = "<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    objs[2] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    objs[3] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"

    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        body = body.encode("latin-1") if isinstance(body, str) else body
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
