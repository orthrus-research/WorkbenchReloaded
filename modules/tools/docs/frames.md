# orthrus-frames

Compares two directories of captured PNG frames and passes or fails each pair under a
tolerance profile. Needs the `frames` extra (NumPy and Pillow).

## Use

```sh
orthrus-frames measure run-1 run-2 run-3 --suggest-profile noise.json
orthrus-frames compare reference candidate --profile noise.json --out diff/
orthrus-frames compare reference candidate --profile exact --recursive --json
```

Frames are matched by file name, or by relative path with `--recursive`. A frame missing from
either side, or of a different size, fails. No frames at all is a failure, not a pass. Exit
status: 0 all passed, 1 any failed, 2 usage error, 3 numpy or Pillow missing. With `--json` the
report is the `result` of one [result object](contract.md).

With `--out`, failed frames get a difference image (the reference dimmed to gray, changed pixels
in red scaled by their difference) and the whole result goes to `report.json`.

## Profiles

```json
{ "channelThreshold": 8, "maxChangedFraction": 0.001, "maxMeanAbsolute": 0.05 }
```

A pixel counts as changed when any channel differs by more than `channelThreshold` (0 to 255).
A frame passes when the changed fraction and the mean absolute difference are both within their
limits. Bundled profiles: `exact` (bit-identical), `near` (8, 0.1%, 0.05) and `repeat`
(8, 2%, 1.0).

Choose limits from measurements, not guesses: capture the same views several times without
changing anything, run `measure`, and set limits above that noise. A tolerance below the noise
of two identical runs can never pass. `--suggest-profile` writes limits 1.5 times the worst
measured noise as a starting point.
