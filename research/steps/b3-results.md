# Step B3: false-accept benchmark, first session (2026-10-16)

## Capture

- `tools/dev/b3-capture.sh`, run after the segfault fix.
- 10 fingers × 20 touches = 200 images, 144x56, kept in local/b3/me/ (gitignored, 0600).
- Right index was captured first, alone, as a format check; the other 9 fingers came in one run with 4 "touch again" prompts. No capture was rejected by the keypoint gate.

## Analysis (`tools/dev/b3-analyze.sh`, results/b3-20261016-113218.json)

- **Scorer:** the A5 harness from fork 40cda345, the same commit as the installed package. Parity with the 2026-10-15 live data is exact: 32 captures, 219 stage scores.
- **Matrix:** 40,000 comparisons.
- **Trials:** K=5 random 18-image templates per finger, seed 0, verify semantics (max over 18 stages). That gives 100 genuine and 9000 impostor trials.

### Impostor score distribution

- Per trial (max over stages): p50 0, p90 0, p99 4, p99.9 13, **max 20**.
- At stage level: 128 of 36,000 pairs score above 0, with a max of 20.
- Top impostors:
  - template left-ring ← probe right-middle #19 (20);
  - template right-middle ← probe right-ring #12 (15);
  - template right-ring ← probe right-index #14 (13).

### FAR and FRR by threshold

FAR upper bounds are one-sided at 95%:
- (a) all trials, assuming independence;
- (b) one template per identity × probe, n = 1800;
- (c) per finger pair, n = 45, the most conservative.

| Threshold | FAR | FRR (same session) | (a) | (b) | (c) |
|---|---|---|---|---|---|
| 105 | 0/9000 | 2/100 | 0.033% | 0.166% | 6.44% |
| 75 | 0/9000 | 2/100 | 0.033% | 0.166% | 6.44% |
| 50 | 0/9000 | 2/100 | 0.033% | 0.166% | 6.44% |
| 30 | 0/9000 | 2/100 | 0.033% | 0.166% | 6.44% |
| 20 | 4/9000 | 2/100 | 0.102% | 0.263% | 10.1% |
| 10 | 23/9000 | 2/100 | 0.362% | 0.583% | 19.2% |
| 5 | 80/9000 | 2/100 | 1.07% | 1.41% | 46.6% |

The 2 genuine failures do not depend on the threshold, so their score is far below every listed threshold (effectively 0). These are coverage misses, not borderline scores.

### The tool's recommendation, and the decision

- The D6 margin rule (impostor max plus 2 rounds of consistent matches) now gives **210**, because the impostor max of 20 is about 4.2 consistent matches.
- At 210 the same-session FRR is 4/100. On the next-day data (a6-switch.md), 2 of 20 genuine rounds (152 and 182) would have failed.
- **Decision: keep 105.**
  - At 105 the observed impostor margin is 5x in score, about 1.8 consistent matches above the worst wrong finger.
  - FAR is 0/9000, and the bound (b) is 0.17% per attempt.
  - Raising the threshold costs real next-day rejections for a margin the data does not show is needed.
  - Lowering it gains nothing: the genuine failures are coverage misses at score about 0, not near-misses.
- The D6 rule stays a heuristic, not a FAR target. Revisit if a guest or cross-day session moves the impostor max.

## Limits

- One person: the impostors are the tester's own other fingers. A consenting guest (`--person guest1`) would be the independent test.
- One session for the genuine side. Cross-day FRR needs a second-day capture of the index fingers: `tools/dev/b3-capture.sh --fingers right-index,left-index` on a later date. Sessions are keyed by local date.
- The bound (c) is driven by n = 45 finger pairs, not by the observed scores.
