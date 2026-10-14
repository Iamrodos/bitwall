"""Speak the number the picture is showing, so the gap can be read off.

A beep says when, but not which: hearing one tone and seeing one digit
gives half a measurement, and the other half is a pitch nobody can name
without a reference. Spoken, both halves are legible -- "seven" against a
screen reading eleven is four seconds, read rather than judged.

The count cycles every twelve seconds and so does the picture, so twelve
seconds of speech looped for the length of the film stays aligned with
it wherever the film is started.
"""
import subprocess, os, numpy as np

RATE, N = 48000, 12
WORDS = ["one", "two", "three", "four", "five", "six",
         "seven", "eight", "nine", "ten", "eleven", "twelve"]

track = np.zeros(RATE * N, np.int16)
for i, word in enumerate(WORDS):
    aiff = f"/tmp/say{i}.aiff"
    subprocess.run(["say", "-o", aiff, word], check=True)
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", aiff,
         "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"],
        capture_output=True, check=True).stdout
    say = np.frombuffer(raw, np.int16)
    # Spoken on the second, and clipped if it would run into the next
    # one -- the edge that matters is where the word starts.
    say = say[:RATE]
    track[i * RATE:i * RATE + len(say)] = say
    os.unlink(aiff)

with open("/tmp/count12.raw", "wb") as fh:
    fh.write(track.tobytes())
print(f"  {N}s of speech, one number a second")
