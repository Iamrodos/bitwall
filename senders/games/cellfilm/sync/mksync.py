import subprocess, sys, numpy as np
sys.path.insert(0, ".")
from bitwall import letters

DELAY = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
W, H, FPS, SECS = 640, 360, 25, 300

def stamp(img, text, cell, cy, ink=255):
    # A pixel a square, since these are pixels of a film and not cells.
    rows = letters.rows(text, letters.BLOCK, spacing="mono", cell=1.0)
    gh, gw = len(rows), len(rows[0])
    x0 = (W - gw * cell) // 2
    y0 = cy - (gh * cell) // 2
    for r, line in enumerate(rows):
        for c, ink in enumerate(line):
            if ink is not None:
                img[y0 + r*cell:y0 + (r+1)*cell,
                    x0 + c*cell:x0 + (c+1)*cell] = ink

# The delay the sound is being held back by, drawn into the picture, so
# a photograph of the wall says which setting it was taken at.
# No trailing S: in a five-row block font an S is indistinguishable
# from a 5, so "2S" reads as twenty-five.
label = "DELAY " + ("%.2f" % DELAY).rstrip("0").rstrip(".")

ff = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
     "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
     "-i", "-", "-f", "lavfi", "-i",
     "aevalsrc='sin(2*PI*(300+80*mod(trunc(t)\\,12))*t)"
     "*lt(mod(t\\,1)\\,0.12)':s=48000:d=%d" % SECS,
     "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
     "-c:a", "aac", "-shortest", "media/sync-test.mp4"],
    stdin=subprocess.PIPE)

# A film's worth of change behind the counter. A black screen with one
# digit on it is a few kilobytes a frame and arrives almost at once; a
# film is a couple of hundred, and the wait being measured is mostly the
# wait for those bytes. Measured against black, the answer belongs to
# the test pattern rather than to anything anyone watches.
rng = np.random.default_rng(1)
BLOCK = 8
for f in range(FPS * SECS):
    small = rng.integers(0, 256, ((H + BLOCK - 1) // BLOCK,
                                  (W + BLOCK - 1) // BLOCK, 3), dtype=np.uint8)
    img = np.repeat(np.repeat(small, BLOCK, 0), BLOCK, 1)[:H, :W]
    n = str(int(f / FPS) % 12 + 1)
    # Cleared behind, so the number is readable over the noise.
    img[60:240, W//2 - 200:W//2 + 200] = 0
    img[280:330, W//2 - 200:W//2 + 200] = 0
    stamp(img, n, 40, 140)
    if DELAY >= 0:
        stamp(img, label, 12, 300, 150)
    ff.stdin.write(img.tobytes())
ff.stdin.close()
ff.wait()
