# Failure taxonomy

The 30-task external slice produced 12 passes, 14 failures, and 4 unknowns. The categories below are assigned from the saved trajectory, not from the final answer alone.

## Environment

The site is unavailable or does not expose the content the task needs.

- Allrecipes--7, --9, and --13: the browser received an access-issue page instead of a recipe. All three finished `blocked` and are graded `unknown`.
- Wolfram--5: the input page loaded, but the result area never rendered; the agent finished `blocked` and is graded `unknown`.
- ArXiv--6: one attempt received `Error 503 first byte timeout`; the task could not be checked.

Do not count these as model failures. They are environment unknowns.

## Observation

The page is reachable, but the observation sent to the model is too large or loses the relevant evidence.

- Cambridge--0 before compaction: the sustainability page produced a 134,297-character observation from 263 interactive elements. The next request exceeded the 96k-character budget at step 3.
- After observation compaction, the same page produced a 35,018-character model view and the run finished in 4 steps with the correct IPA and definitions.

This category is about the model input, not the browser action.

## Navigation

The agent reaches a page but takes the wrong path, or the action is outside the declared scope.

- Apple--12 before the support origin was added: the agent clicked a repair link to `support.apple.com`, which was outside the allowed origin. Three attempts were rejected as invalid actions.
- Hugging Face--13: the agent reached the Llama docs, but not the LlamaTokenizer page; it then scrolled instead of using the sidebar link.

## Loop

The agent repeats an action without making progress.

- Hugging Face--13: 38 identical `scroll down` actions on the same Llama page.
- Wolfram--11: repeated `Compute` clicks and waits, reaching 50 steps before the progress guard.
- ArXiv--10: repeated clicks on the same search control, reaching 50 steps before the progress guard.

After target-identity progress detection, Wolfram stopped at 22 steps and ArXiv at 16; both still failed, but the cost is bounded.

## Budget

The task is still running when a step or time limit is reached.

- Apple--1: exhausted 50 steps while trying to check iOS 17 compatibility.
- Coursera--5: exhausted 50 steps while searching for a C++ Specialization.

Budget stops are failures of the attempt, not proof that the task is impossible.

## Protocol

The model returns actions that do not match the closed tool contract or the current page state.

- ArXiv--11: three consecutive invalid actions, ending the run.
- Coursera--11: three consecutive invalid actions, ending the run.
- A stale element reference is a normal invalid action; it becomes a protocol stop only after three consecutive invalid decisions.

## Provider and infrastructure

- One DeepSeek attempt returned HTTP 402 when the account balance was exhausted. That is a provider failure, not an agent decision.
- A local disk interruption and a cancelled run are recorded separately; they are not pooled into model success.

## How to read the numbers

- Environment unknowns are excluded from known-result success but included in coverage.
- Loop and budget stops are failures.
- Protocol stops are failures, but the reason is the action contract, not the task answer.
- Do not compare the 30-task rate with WebVoyager leaderboard numbers. The task set, grading, and origin policy differ.
