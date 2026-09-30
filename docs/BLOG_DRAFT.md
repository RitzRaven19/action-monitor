# Watching what AI agents do, not what they say

*Draft — the real-usage section is waiting on two to three weeks of data.*

AI agents now read files, send email, move money and run shell commands on our behalf. The usual
way to check an agent is to read its reasoning. But an agent's final answer only contains what it
chose to say. In one of my test runs, an agent summarised a document exactly as asked — and quietly
posted that summary to an outside server, because an instruction hidden in its input told it to.
Its answer never mentioned the upload.

So I built a monitor that ignores what the agent says and judges what it does: every tool call,
checked against the scope of the task the user actually asked for.

## The idea: a declared scope, then watch for divergence

For each task, the monitor works out a *declared scope* from the user's own words, never from
anything the agent reads along the way (that's where injected instructions live). "Read
`notes.txt` and summarise it" declares reading that file, and nothing else. Every tool call is
then judged against it:

- a call using a tool the task never asked for → **high severity** (sending data to a server during a
  summary task);
- the right kind of tool on something the task didn't name → **low severity**, logged (reading a
  different file).

The log it judges from sits outside the agent's reach: the agent has no tool that can read, edit or
suppress it.

On my own demo tasks, the action judge caught every injected attack with no false alarms on clean
tasks. A baseline that scans the agent's text for suspicious words caught 60% — it missed exactly the
cases where the agent simply didn't mention what it did.

## The part I care about: slow scope creep

Per-call checks miss a quieter pattern: many small, individually harmless steps outside the task.
Reading one extra file is fine. Quietly reading a new file every turn, across many conversations, is
not — but no single step looks wrong.

So the monitor tracks the same signal at four levels: this turn, this conversation, this agent
identity's whole history, and that history weighted by how sensitive the resources look (credentials
and personal data count for more than meeting notes).

Then I attacked my own detector. Staying under a per-turn threshold evaded it. Spreading the steps
across turns evaded the turn-level check, which a conversation-level check closed. Spreading across
conversations evaded that too, which identity-level tracking closed — but tracking a whole history
also fires on an ordinary busy agent that touched many unrelated things over time. Every step closed
one gap and opened a cost. That trade-off is the honest result, not a solved problem.

## Testing it on a real benchmark, for free

[AgentDojo](https://agentdojo.spylab.ai) publishes recordings of real models under prompt-injection
attack. I replayed 7,008 of them, from 8 models, through the monitor — no model calls, no cost. My
scoring reproduced AgentDojo's own published attack-success rate for GPT-4o exactly (47.69%), which
checked that I was reading the data correctly.

- With blocking on, attack success fell from 21% to 5% overall, and for GPT-4o from 48% to 14%.
- The same rules held on four models I hadn't looked at while designing them.
- For context, AgentDojo's best published defenses on GPT-4o reach about 7%; mine uses no extra model
  calls, and doesn't match them.

The weak spot was instructive: attacks that use the *right* tool with the *wrong* target — paying an
attacker when the task is to pay a bill. Treating an unnamed recipient on money, messaging and
sharing tools as a violation fixed most of that, at the cost of interrupting more legitimate work.

A replay can't show what a live agent does after it's blocked, so I ran a small live test. Without
the monitor, the model fell for 7 of 8 attacks; with it, 0 of 6. Every refused call was a payment to
the attacker. The agent did try another route — switching from sending money to scheduling a
payment — and that was blocked too. The cost the replay missed: after a refusal, the agent often gave
up on the user's own task. Eight cases is a sanity check, not a statistic, but it's a real cost of
blocking.

## Watching a real agent

I also wired the monitor into Claude Code, the coding agent I use daily, through its hooks. It
records every tool call across all my projects.

Real use found things my tests hadn't. It flagged my own checks of my own deployed site as data
leaks; the word "curl" inside a commit message as a network call; and mentioning the monitor's config
file as tampering with it. Each was a rule matching text that merely *mentioned* something, fixed and
re-checked against the same recorded sessions.

**[Real-usage numbers go here after 2–3 weeks: how often the identity-level checks false-alarm on
ordinary work.]**

## What's out there, and what isn't

Per-call guardrails for agents already exist, several stronger than mine (Progent, CaMeL, and
open-source firewalls such as cordum and agentjail). Research in 2026 has started on attacks split
across sessions (Magnet), but without public code. What I couldn't find was an open-source monitor
that tracks accumulating scope creep across sessions with inspectable rules — or a public benchmark
that tests it. No dataset I checked combines recorded agent tool calls with cross-session grouping,
so my distinctive layer is exactly the one no outside benchmark can yet test.

## Limits

- The declared scope comes from keyword rules; unusual phrasing can fool it.
- An action that stays inside the declared scope — subtle sabotage — is invisible to it by design.
- Obfuscated commands, network access inside script files, and data hidden in URL paths can get past
  the coding-agent rules.
- The cross-session layers haven't been validated on outside data.

The code, the evaluation scripts and every report are open:
[github.com/RitzRaven19/action-monitor](https://github.com/RitzRaven19/action-monitor). It installs as
a Claude Code plugin in two commands.
