#!/usr/bin/env python3
"""Generate test fixtures for manyue under tests/fixtures/books/:

  漫画/测试漫画-大文件.pdf   >20MB manga-style PDF (noise pages)
  漫画/测试漫画-小.pdf       3-page text PDF
  小说/测试小说.epub         EPUB with embedded cover + Chinese chapters
  小说/测试文章.txt           UTF-8 TXT (~150KB)
  小说/测试文章-GBK.txt       GBK-encoded TXT
  小说/sample-book.azw3      real AZW3 sample (copied, not generated)

Run: PYTHONPATH=/tmp/piptest python3 tests/make_fixtures.py
"""

import os
import shutil
import zipfile

ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "books")


def make_big_pdf(path, pages=36):
    """Manga-like PDF: each page a big noisy JPEG + page number.

    Noise JPEGs compress poorly, so each page adds ~700KB-1MB.
    """
    from PIL import Image, ImageDraw
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    tmpdir = os.path.join(os.path.dirname(path), ".tmpimg")
    os.makedirs(tmpdir, exist_ok=True)
    c = canvas.Canvas(path, pagesize=A4)
    width, height = A4
    for i in range(pages):
        img_path = os.path.join(tmpdir, "page_%02d.jpg" % i)
        img = Image.effect_noise((1100, 1550), 100 + i).convert("RGB")
        draw = ImageDraw.Draw(img)
        # A white speech-balloon-ish box + page number, manga flavour.
        draw.rectangle([80, 80, 1020, 320], fill=(255, 255, 255))
        draw.rectangle([80, 1250, 1020, 1470], fill=(20, 20, 20))
        draw.text((120, 140), "TEST MANGA PAGE %d" % (i + 1),
                  fill=(0, 0, 0))
        draw.text((120, 1330), "第 %d 页" % (i + 1), fill=(255, 255, 255))
        img.save(img_path, "JPEG", quality=88)
        c.drawImage(img_path, 0, 0, width=width, height=height)
        c.showPage()
    c.save()
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("wrote %s (%d bytes, %d pages)"
          % (path, os.path.getsize(path), pages))


def make_small_pdf(path):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(path, pagesize=A4)
    for i in range(3):
        c.setFont("Helvetica", 20)
        c.drawString(72, 750, "Small test PDF - page %d" % (i + 1))
        c.setFont("Helvetica", 12)
        c.drawString(72, 720, "Hello from manyue test fixture.")
        c.showPage()
    c.save()
    print("wrote %s (%d bytes)" % (path, os.path.getsize(path)))


def make_epub(path):
    """Minimal EPUB 2 with an embedded PNG cover and Chinese chapters."""
    from PIL import Image, ImageDraw

    tmpdir = os.path.join(os.path.dirname(path), ".tmpepub")
    os.makedirs(os.path.join(tmpdir, "META-INF"), exist_ok=True)
    os.makedirs(os.path.join(tmpdir, "OEBPS", "Text"), exist_ok=True)
    os.makedirs(os.path.join(tmpdir, "OEBPS", "Styles"), exist_ok=True)
    os.makedirs(os.path.join(tmpdir, "OEBPS", "Images"), exist_ok=True)

    cover = Image.new("RGB", (600, 800), (46, 58, 79))
    draw = ImageDraw.Draw(cover)
    draw.ellipse([150, 100, 450, 400], fill=(240, 230, 199))
    draw.text((230, 500), "测试小说", fill=(255, 255, 255))
    cover.save(os.path.join(tmpdir, "OEBPS", "Images", "cover.png"))

    def write(rel, text):
        full = os.path.join(tmpdir, rel)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)

    write("mimetype", "application/epub+zip")
    write("META-INF/container.xml", """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""")
    write("OEBPS/content.opf", """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>测试小说：满月之下</dc:title>
    <dc:creator>测试作者</dc:creator>
    <dc:language>zh-CN</dc:language>
    <dc:identifier id="BookID">urn:uuid:manyue-test-epub</dc:identifier>
    <meta name="cover" content="cover-img"/>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="cover" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch1" href="Text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="Text/ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="style" href="Styles/style.css" media-type="text/css"/>
    <item id="cover-img" href="Images/cover.png" media-type="image/png"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="cover"/>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>
""")
    write("OEBPS/toc.ncx", """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:manyue-test-epub"/></head>
  <docTitle><text>测试小说：满月之下</text></docTitle>
  <navMap>
    <navPoint id="n1" playOrder="1"><navLabel><text>封面</text></navLabel><content src="Text/cover.xhtml"/></navPoint>
    <navPoint id="n2" playOrder="2"><navLabel><text>第一章</text></navLabel><content src="Text/ch1.xhtml"/></navPoint>
    <navPoint id="n3" playOrder="3"><navLabel><text>第二章</text></navLabel><content src="Text/ch2.xhtml"/></navPoint>
  </navMap>
</ncx>
""")
    write("OEBPS/Styles/style.css",
          "body{font-size:18px;line-height:1.8}h1{font-size:24px}\n")
    write("OEBPS/Text/cover.xhtml", """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>封面</title>
<link rel="stylesheet" type="text/css" href="../Styles/style.css"/>
</head><body><div style="text-align:center"><img src="../Images/cover.png" alt="封面"/></div>
<h1>测试小说：满月之下</h1><p>测试作者 著</p></body></html>
""")
    chapter_body = (
        "<p>月亮升起来的时候，满月正挂在屋顶上。这是一个测试章节，"
        "用来验证 EPUB 阅读器能不能正常显示中文、能不能记住阅读位置。</p>\n"
        "<p>第二段文字。春江潮水连海平，海上明月共潮生。"
        "滟滟随波千万里，何处春江无月明。</p>\n"
    )
    write("OEBPS/Text/ch1.xhtml", """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第一章</title>
<link rel="stylesheet" type="text/css" href="../Styles/style.css"/>
</head><body><h1>第一章 月升</h1>%s</body></html>
""" % chapter_body)
    write("OEBPS/Text/ch2.xhtml", """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第二章</title>
<link rel="stylesheet" type="text/css" href="../Styles/style.css"/>
</head><body><h1>第二章 月落</h1>%s</body></html>
""" % chapter_body)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(os.path.join(tmpdir, "mimetype"), "mimetype",
                 compress_type=zipfile.ZIP_STORED)
        for dirpath, _dirs, files in os.walk(tmpdir):
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, tmpdir)
                if rel == "mimetype":
                    continue
                zf.write(full, rel)
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("wrote %s (%d bytes)" % (path, os.path.getsize(path)))


def make_txt(path, encoding, chars=60000):
    para = ("这是一个 TXT 测试文件。满月阅读要能分页显示长文本，记住读到的"
            "字符位置，换设备也能接着读。床前明月光，疑是地上霜。"
            "举头望明月，低头思故乡。\n")
    text = para * (chars // len(para) + 1)
    text = text[:chars]
    with open(path, "w", encoding=encoding) as fh:
        fh.write(text)
    print("wrote %s (%d bytes, %s)"
          % (path, os.path.getsize(path), encoding))


def main():
    for sub in ("漫画", "小说"):
        os.makedirs(os.path.join(ROOT, sub), exist_ok=True)
    make_big_pdf(os.path.join(ROOT, "漫画", "测试漫画-大文件.pdf"))
    make_small_pdf(os.path.join(ROOT, "漫画", "测试漫画-小.pdf"))
    make_epub(os.path.join(ROOT, "小说", "测试小说.epub"))
    make_txt(os.path.join(ROOT, "小说", "测试文章.txt"), "utf-8")
    make_txt(os.path.join(ROOT, "小说", "测试文章-GBK.txt"), "gbk")
    azw3_src = os.path.join(os.path.dirname(__file__), "fixtures",
                            "sample-book.azw3")
    azw3_dst = os.path.join(ROOT, "小说", "sample-book.azw3")
    shutil.copyfile(azw3_src, azw3_dst)
    print("copied %s (%d bytes)" % (azw3_dst, os.path.getsize(azw3_dst)))
    total = sum(os.path.getsize(os.path.join(dp, f))
                for dp, _dns, fs in os.walk(ROOT) for f in fs)
    print("fixtures total: %d bytes in %s" % (total, ROOT))


if __name__ == "__main__":
    main()
