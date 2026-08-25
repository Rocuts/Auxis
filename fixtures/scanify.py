import pymupdf, numpy as np, io, os
from PIL import Image, ImageEnhance, ImageFilter

SRC = "/home/claude/fixtures/_tmp_05_digital.pdf"
DST = "/home/claude/fixtures/05_capital_gains_preferential_rates_TY2025.pdf"
DPI = 200
rng = np.random.default_rng(7)

doc = pymupdf.open(SRC)
out = pymupdf.open()

for i, page in enumerate(doc):
    pix = page.get_pixmap(dpi=DPI)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")

    # 1. leve rotacion (papel torcido en el alimentador)
    img = img.rotate(-0.55, resample=Image.BICUBIC, expand=False, fillcolor=248)

    # 2. desenfoque optico del sensor
    img = img.filter(ImageFilter.GaussianBlur(0.45))

    # 3. gradiente de iluminacion (lampara del escaner)
    a = np.asarray(img).astype(np.float32)
    h, w = a.shape
    yy, xx = np.mgrid[0:h, 0:w]
    grad = 1.0 - 0.055 * (xx / w) - 0.035 * ((yy / h) ** 1.6)
    a *= grad

    # 4. ruido de grano + motas de polvo
    a += rng.normal(0, 3.4, a.shape)
    mask = rng.random(a.shape) < 0.00035
    a[mask] = rng.uniform(70, 170, mask.sum())

    # 5. bordes ligeramente sucios
    a[:6, :] *= 0.965; a[-6:, :] *= 0.965
    a[:, :6] *= 0.97;  a[:, -6:] *= 0.97

    img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    img = ImageEnhance.Contrast(img).enhance(1.13)

    buf = io.BytesIO()
    img.convert("L").save(buf, format="JPEG", quality=74, optimize=True)
    buf.seek(0)

    W, H = page.rect.width, page.rect.height
    p = out.new_page(width=W, height=H)
    p.insert_image(pymupdf.Rect(0, 0, W, H), stream=buf.read())

out.set_metadata({
    "title": "Preferential Rates on Long-Term Capital Gain - CG-2025/07",
    "author": "Capital Gains Division",
    "subject": "Preferential rate bands (scanned copy)",
    "producer": "MFP Scan Module v3.2",
    "creator": "Ricoh IM C4500 / Scan-to-PDF",
})
out.save(DST, deflate=True, garbage=3)
out.close(); doc.close()
os.remove(SRC)
print("escaneado ->", os.path.basename(DST), round(os.path.getsize(DST)/1024, 1), "KB")
