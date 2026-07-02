Your data is honest — every number in Table 1 matches the transcript output exactly, and you correctly stated that you used no external references. The problem is only the wording: your claims are stronger than 3 trials can support. Please make these fixes; none of them require re-running the model.

1. Remove the word 'significantly.' You did not run a significance test. In the abstract and Results, change 'significantly reduce variance' to 'showed lower variance' or 'reduced variance in this experiment.'

2. Soften the conclusion. Change 'explicit constraints are more effective than role-playing for ensuring predictable model behavior' to something scoped to your data, e.g. 'In this single-topic experiment (n=3), the constrained prompt produced more consistent output lengths than the persona prompt.'

3. Report the raw lengths. In Results, list the three character lengths per prompt (they are in data.json). This lets readers see that one long response can dominate the variance when n=3.

4. Name the real mechanism. In Results or Limitations, note that the neutral and persona outputs sometimes returned multiple alternative summaries ('Option 1', 'Option 2', 'Option 3'), and that this — not framing alone — is what inflated their length and variance. The constrained prompt ('exactly three sentences') suppressed that behavior.

5. State the variance definition. In Method, note that you used numpy population variance (np.var, divides by n), not sample variance.

6. In Limitations, add that a single run per condition means the ordering could change on re-run; if you had more turns, 20+ trials per prompt would be needed before ranking the framings.

The experiment itself is fine and the writing is clear. Just bring the claims down to what three trials on one paragraph can show.