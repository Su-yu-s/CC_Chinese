"""把用户的 CC_Chinese SVG Logo 转成多尺寸 Windows ICO（assets/icon.ico）。

用 PySide6 QSvgRenderer 渲染 SVG（黑底白弧），再用 Pillow 存成含 16~256 的 .ico
供 exe 图标使用。运行一次即可。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

SRC = r"F:\浏览器下载\CC_Chinese_Logo_One\cc_chinese_logo.svg"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.ico")
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def qimage_to_pil(qimg: QImage) -> Image.Image:
    qimg = qimg.convertToFormat(QImage.Format.Format_RGBA8888)
    return Image.frombytes("RGBA", (qimg.width(), qimg.height()), bytes(qimg.constBits()))


def main():
    app = QGuiApplication(sys.argv)
    renderer = QSvgRenderer(SRC)
    img = QImage(512, 512, QImage.Format.Format_ARGB32)
    img.fill(0x00000000)
    painter = QPainter(img)
    renderer.render(painter)
    painter.end()
    pil = qimage_to_pil(img)
    pil.save(OUT, format="ICO", sizes=[(s, s) for s in SIZES])
    print("saved", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
