"""Cut the What's Up? lettering out of the supplied image and recolour it
for the app's dark theme. One-off asset script; the output is web/splash.webp."""
import sys
import numpy as np
from PIL import Image, ImageFilter

src_path, out_path, preview_path = sys.argv[1:4]
src = Image.open(src_path).convert('RGB').resize((2048, 2048), Image.LANCZOS)
im = np.asarray(src).astype(float)
R, G, B = im[..., 0], im[..., 1], im[..., 2]
L = 0.299 * R + 0.587 * G + 0.114 * B

def smooth(x, a, b):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)

# The letter faces are strongly blue in absolute terms (B-R around 80). The
# paper grid is neutral and the cast shadows, though bluish, top out near 42.
# The deepest shadows, in the gaps between letters, are nearly as blue as the
# faces -- but much darker. Requiring brightness as well removes them.
alpha = smooth(B - R, 50, 66) * smooth(L, 98, 126)

h, w = L.shape
yy, xx = np.mgrid[0:h, 0:w]
t = np.clip(xx / w * 0.8 + yy / h * 0.2, 0, 1)[..., None]
violet = np.array([124, 108, 246.0])
blue = np.array([75, 157, 251.0])
base = violet * (1 - t) + blue * t
shade = np.clip(L / 168.0, 0.34, 1.26)[..., None]      # keep the 3D shading
rgb = base * np.minimum(shade, 1.0)
hi = np.clip(shade - 1.0, 0, 1) / 0.26                 # highlights lift to white
rgb = rgb * (1 - hi * 0.5) + 255 * hi * 0.5

out = np.dstack([np.clip(rgb, 0, 255), alpha * 255]).astype(np.uint8)
img = Image.fromarray(out, 'RGBA')
# Opening (erode, then dilate) deletes hairline leftovers from shadow edges
# without moving the outline of anything as thick as a letter.
a = img.getchannel('A').filter(ImageFilter.MinFilter(7)).filter(ImageFilter.MaxFilter(7))
img.putalpha(a.filter(ImageFilter.GaussianBlur(1.0)))

bbox = img.getchannel('A').point(lambda v: 255 if v > 40 else 0).getbbox()
pad = 40
bbox = (max(0, bbox[0] - pad), max(0, bbox[1] - pad),
        min(w, bbox[2] + pad), min(h, bbox[3] + pad))
img = img.crop(bbox)
img = img.resize((1100, round(img.height * 1100 / img.width)), Image.LANCZOS)
img.save(out_path, 'WEBP', quality=90, method=6)
print('splash', img.size)

bg = Image.new('RGBA', (1290, 900), (8, 9, 11, 255))
bg.alpha_composite(img, ((1290 - img.width) // 2, (900 - img.height) // 2))
bg.convert('RGB').save(preview_path)
