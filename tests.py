"""Quick self-check of the pure logic: python tests.py"""
from clip import group_words, rec_seconds
from spikes import Merger, evaluate, replay

# evaluate: warm-up, min-count, k*std
hist = [10, 12, 8, 10] * 3
assert not evaluate(hist[:5], 100)[3]                    # still warming up
assert evaluate(hist, 100)[3]                            # clear spike
assert not evaluate(hist, 14, min_count=15)[3]           # under min-count
assert not evaluate([20] * 12, 20)[3]                    # not above mean

# merger: spikes < 30 s apart are one moment at the loudest bucket
m = Merger()
out = [m.feed(t, n, s) for t, n, s in [(0, 50, 1), (10, 80, 1), (20, 5, 0), (30, 5, 0), (40, 60, 1), (70, 70, 1)]]
assert out[4] == {"epoch": 10, "count": 80, "last": 10}
assert out[5] == {"epoch": 40, "count": 60, "last": 40}
assert m.flush()["epoch"] == 70
moments, _ = replay([(i * 10, 10 + i % 2) for i in range(20)] + [(200, 90)], 2.5, 15, 12, 30)
assert [x["epoch"] for x in moments] == [200]

# wall clock -> recording seconds, +1 s max, clamps before start
samples = [(100.0, 0.0), (101.0, 1.0), (150.0, 2.0)]   # ad break: wall 101..150 recorded nothing
assert rec_seconds(samples, 99) == 0.0
assert rec_seconds(samples, 100.5) == 0.5
assert rec_seconds(samples, 130) == 2.0
assert rec_seconds(samples, 155) == 3.0

# captions: max 3 words, break on pause > 0.45 s and punctuation
w = [(0, .2, " Oh"), (.25, .4, " my"), (.45, .6, " god"), (.65, .8, " what"), (1.5, 1.7, " no,"), (1.75, 1.9, " way")]
assert group_words(w) == [(0, .6, "Oh my god"), (.65, .8, "what"), (1.5, 1.7, "no,"), (1.75, 1.9, "way")]
print("all tests passed")
