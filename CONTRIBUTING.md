# Contributing

Issues and pull requests are welcome.

- **Keep the non-negotiables** in the [README](README.md#non-negotiables): TOD makes every
  decision; options come only from the screen; one image per request; no scripted sequences or
  fixed coordinates as actions; nothing leaked into option text; ground truth never in a request.
  A change that makes code pick an input will not be merged, even if it raises the score.
- **Show the evidence.** For a behaviour change, include the run id(s) and the tick numbers that
  show the before/after, ideally an offline dry run on saved frames
  (`python -m tod_papers.loop --frames runs/<ts>/raw_NNNN.png --day N`) and the
  `tools/report.py` table of a live run.
- **Never commit** `.env`, API keys, model weights, `runs/`, captures or game files. Check with
  `git grep` before pushing.
- Code style: plain Python 3.13, no new dependencies without a pinned entry in the matching
  `requirements-*.txt`.

By contributing you agree that your contribution is licensed under Apache-2.0 (see
[LICENSE](LICENSE)).
