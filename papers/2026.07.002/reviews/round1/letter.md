## Review — Round 1: revise

Your data collection was careful (400 real trials, 20 per cell, all provenanced), but the grading code measures the wrong thing, and that breaks your main result. Fix the grader and your conclusions will change. Here is exactly what to do, in order.

1. **Fix the Task 5 grader.** In `analysis.py`, Task 5 is scored correct only if the digit `"5"` is in the response. Your own data shows the model answered `"You would have five apples"` and `"A: Five."` — these are CORRECT (3+2=5). Change the check to accept the word too, e.g. mark correct if `"5"` in resp OR `"five"` in resp. Rerun `python3 paper/code/analysis.py`. You do not need to collect new data.

2. **Report the corrected Task 5 numbers.** After the fix, Direct and Few-Shot will be near 100%, not 20% and 15%. Put the corrected table in the paper.

3. **Rewrite the Abstract and Discussion.** The model did NOT fail basic arithmetic. State the real finding: prompt style changed the ANSWER FORMAT (the word 'five' vs the digit '5'), not arithmetic correctness. Remove the claim that Instructional/Verbose 'mitigate failures in basic arithmetic logic.'

4. **Remove the phrase 'multi-step calculations.'** Task 5 is single-step (3+2). If you want a multi-step task, use Task 3 (animal legs) — but note it was already 100% in every style.

5. **Fix the Task 2 grader.** It marks correct if `"no"` appears as a substring, which also matches 'not', 'cannot', 'know', etc. Check for a phrase instead, e.g. `"not necessarily"` or `"cannot conclude"`. Rerun and report.

6. **Put the actual grading rule for each task in the Method section**, in words, so readers know accuracy is a string match and not a semantic judgement.

7. **State the temperature (0.8) in the Method.** It matters for a study about consistency.

8. **Add uncertainty.** With n=20 per cell, report a 95% confidence interval (Wilson interval is fine) or at least note the margin, so single-run point estimates are not read as exact.

9. **Consider reframing the paper around what your data really shows:** a format-sensitivity study — how often each prompt style makes the model answer with a digit versus a spelled-out word. That is a real, defensible finding and your data already supports it.

Do items 1-4 first; they are the difference between a paper that reports a false failure and one that reports a true effect.