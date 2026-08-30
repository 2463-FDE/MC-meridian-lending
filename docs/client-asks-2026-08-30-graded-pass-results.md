# Client asks — 2026-08-30 working log, graded pass results

**Audience:** Dana (VP Lending Ops, Meridian) · **Sent:** — · **Answered:** —

**Status: DRAFTED, NOT SENT.** The evaluation pass ran on 2026-08-30 and the results
are final. The email-fence copy and the report itself are held outside this repository
(they carry her packet paths, her case ids and her corpus structure, and this is a
public fork). Four asks below come out of the run; three of them are decisions only she
can make.

## Context she needs to have the asks make sense

The pass ran once over the 28 officer questions. Under the terms already agreed, each
question gets one bounded pass, so nothing below can be re-measured without her saying
so — which is what makes asks 2 and 3 decisions rather than engineering choices.

Two results qualify the numbers, and both are disclosed in the report rather than left
for her to find:

- Thirteen verdicts read `human review`. That is our own 240-character limit on the
  grader's written explanation, not uncertainty about her corpus. Five questions
  (q01, q04, q13, q14, q16) lost their model-graded verdicts to it, because we discard
  rather than truncate — truncating would still retain 240 characters of her policy
  text on disk.
- Every graded verdict on every target came back positive, and the set contains no
  question with a deliberately wrong expected conclusion. So the pass does not
  evidence that the grader can return a negative verdict.

## Asks

| # | Ask | Dana's response |
|---|-----|-----------------|
| 1 | May we add negative controls — questions carrying a knowingly wrong expected conclusion — to the next gold set? Without them we cannot evidence that the grader discriminates, and a row of 1.00s has to be read with that caveat attached. | |
| 2 | The grader's written explanation is capped at 240 characters, because we retain no model text beyond one line. The model writes past it: five of 28 questions lost their verdicts that way, and the median explanation on the ones that survived is 189 characters against a maximum of exactly 240. Do you want the cap raised, or the grader asked for a shorter explanation? Either is cheap; the cap is a retention decision, so it is yours. | |
| 3 | Recovering those five questions means grading them a second time. One pass per question is your term, not our judgement — do you authorise a second pass limited to q01, q04, q13, q14 and q16? | |
| 4 | Four topics — debt to income, fee schedule, interest rate, APR and finance charge — carry exactly one question each. All four graded cleanly this time, but a single uncertain verdict would have left the topic with no rate at all rather than a low one. May we add questions to those four before the next pass? | |
| 5 | The retrieval threshold trades two errors against each other. At the value we derived, two answerable questions would be wrongly declined and none of the unanswerable ones are answered with false confidence; the keyword baseline reverses that exactly (0 and 5). We chose to eliminate the false-confident answers. Do you weigh those two errors the same way, or should we re-derive the threshold against a stated preference? | |

## Notes for whoever picks this up

- Ask 5 is the one with a real engineering consequence: the threshold is a single
  number bound to one (corpus, embedding model) pair, and it must be re-derived
  whenever either side moves. It is not portable and should never be copied forward.
- Asks 2 and 3 travel together. Raising the cap without a second pass changes nothing
  about this run's numbers; a second pass without raising the cap would lose the same
  five questions again.
- Nothing here should be answered by us on her behalf. Where a previous ask went
  unanswered we recorded the working assumption; these five are all live.
