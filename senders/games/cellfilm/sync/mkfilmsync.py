"""The film itself, with the second it is showing written on it.

The counting clip measures the lag exactly but is not what anyone
watches; the film is what anyone watches but says nothing about which
moment is on the glass. This is the film with a number stamped into it
and the spoken count in place of its sound, so the gap can be read off
whole numbers on real material rather than judged against lips.
"""
import subprocess, sys, numpy as np
sys.path.insert(0, ".")
from bitwall import letters

SRC = "media/Finding Nemo (2003).mp4"
AT, SECS, W, H, FPS = 1680, 300, 640, 360, 25

grab = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(AT),
     "-t", str(SECS), "-i", SRC, "-vf", f"scale={W}:{H}", "-r", str(FPS),
     "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)

out = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
     "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
     "-r", str(FPS), "-i", "-",
     "-f", "s16le", "-ar", "48000", "-ac", "1",
     "-stream_loop", str(SECS // 12), "-i", "/tmp/count12.raw",
     "-map", "0:v", "-map", "1:a",
     "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
     "-c:a", "aac", "-shortest", "media/sync-film.mp4"],
    stdin=subprocess.PIPE)

n = W * H * 3
for f in range(FPS * SECS):
    buf = grab.stdout.read(n)
    if len(buf) < n:
        break
    img = np.frombuffer(buf, np.uint8).reshape(H, W, 3).copy()
    rows = letters.rows(str(int(f / FPS) % 12 + 1), letters.BLOCK,
                        spacing="mono", cell=1.0)
    gh, gw = len(rows), len(rows[0])
    cell = 14
    x0, y0 = (W - gw * cell) // 2, H - gh * cell - 20
    # Cleared behind, so the number survives whatever the film is doing.
    img[y0 - 6:y0 + gh * cell + 6, x0 - 6:x0 + gw * cell + 6] = 0
    for r, line in enumerate(rows):
        for c, ink in enumerate(line):
            if ink is not None:
                img[y0 + r*cell:y0 + (r+1)*cell,
                    x0 + c*cell:x0 + (c+1)*cell] = 255
    out.stdin.write(img.tobytes())
out.stdin.close()
out.wait()
print("  media/sync-film.mp4")
