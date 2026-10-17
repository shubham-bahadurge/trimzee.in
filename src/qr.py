"""Tiny dependency-free QR code generator (byte mode, error correction M, versions 1-6 -> up to ~100 characters).
qr_svg(text) returns an <svg> string."""

_EXP, _LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i], _LOG[_x] = _x, _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]

# version: (data bytes per block, EC bytes per block, blocks)
_TABLE = {1: (16, 10, 1), 2: (28, 16, 1), 3: (44, 26, 1), 4: (32, 18, 2), 5: (43, 24, 2), 6: (27, 16, 4)}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}


def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs(data, n):
    gen = [1]
    for i in range(n):
        nxt = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            nxt[j] ^= c
            nxt[j + 1] ^= _mul(c, _EXP[i])
        gen = nxt
    res = [0] * n
    for b in data:
        factor = b ^ res[0]
        res = res[1:] + [0]
        for i in range(n):
            res[i] ^= _mul(gen[i + 1], factor)
    return res


def _codewords(data, v):
    dpb, ecpb, blocks = _TABLE[v]
    total = dpb * blocks
    bits = "0100" + format(len(data), "08b") + "".join(format(b, "08b") for b in data)
    bits += "0" * min(4, total * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)
    cw = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    i = 0
    while len(cw) < total:
        cw.append(0xEC if i % 2 == 0 else 0x11)
        i += 1
    parts = [cw[k * dpb:(k + 1) * dpb] for k in range(blocks)]
    ecs = [_rs(p, ecpb) for p in parts]
    out = [p[i] for i in range(dpb) for p in parts]
    out += [e[i] for i in range(ecpb) for e in ecs]
    return out


def _build(data):
    v = next((v for v in range(1, 7) if len(data) <= _TABLE[v][0] * _TABLE[v][2] - 2), None)
    if v is None:
        raise ValueError("text too long for this QR generator")
    size = 17 + 4 * v
    mod = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def setf(x, y, dark):
        mod[y][x] = dark
        fn[y][x] = True

    for i in range(size):                      # timing patterns
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)

    def finder(cx, cy):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    d = max(abs(dx), abs(dy))
                    setf(x, y, d not in (2, 4))
    finder(3, 3), finder(size - 4, 3), finder(3, size - 4)

    pos = _ALIGN[v]
    for cy in pos:
        for cx in pos:
            if (cx, cy) in ((6, 6), (6, pos[-1]), (pos[-1], 6)) and len(pos) > 1 and (cx, cy) != (pos[-1], pos[-1]):
                continue
            if len(pos) == 2 and (cx, cy) in ((pos[0], pos[0]), (pos[0], pos[1]), (pos[1], pos[0])):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setf(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    def format_bits(mask):
        d = (0 << 3) | mask                    # error level M = 00
        rem = d
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = ((d << 10) | rem) ^ 0x5412
        b = lambda i: (bits >> i) & 1 == 1
        for i in range(6):
            setf(8, i, b(i))
        setf(8, 7, b(6)), setf(8, 8, b(7)), setf(7, 8, b(8))
        for i in range(9, 15):
            setf(14 - i, 8, b(i))
        for i in range(8):
            setf(size - 1 - i, 8, b(i))
        for i in range(8, 15):
            setf(8, size - 15 + i, b(i))
        setf(8, size - 8, True)
    format_bits(0)                              # reserve the format areas

    cw = _codewords(data, v)
    i = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not fn[y][x] and i < len(cw) * 8:
                    mod[y][x] = (cw[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        right -= 2

    masks = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0]

    def penalty(m):
        score = 0
        for grid in (m, [list(r) for r in zip(*m)]):          # rows, then columns
            for row in grid:
                run = 1
                for k in range(1, size):
                    if row[k] == row[k - 1]:
                        run += 1
                        if run == 5:
                            score += 3
                        elif run > 5:
                            score += 1
                    else:
                        run = 1
        for y in range(size - 1):
            for x in range(size - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(r.count(True) for r in m)
        score += abs(dark * 20 - size * size * 10) // (size * size) * 10
        return score

    best = None
    for mask, test in enumerate(masks):
        m = [row[:] for row in mod]
        for y in range(size):
            for x in range(size):
                if not fn[y][x] and test(x, y):
                    m[y][x] = not m[y][x]
        s = penalty(m)
        if best is None or s < best[0]:
            best = (s, mask, m)
    _, mask, matrix = best
    mod[:] = matrix
    format_bits(mask)
    return mod


def qr_matrix(text):
    m = _build(text.encode("utf-8"))
    return m


def qr_svg(text, scale=8, border=4):
    m = _build(text.encode("utf-8"))
    n = len(m) + 2 * border
    path = "".join(f"M{x + border},{y + border}h1v1h-1z" for y, row in enumerate(m) for x, d in enumerate(row) if d)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{n * scale}" height="{n * scale}" '
            f'shape-rendering="crispEdges" role="img" aria-label="QR code"><rect width="{n}" height="{n}" fill="#fff"/>'
            f'<path d="{path}" fill="#000"/></svg>')
