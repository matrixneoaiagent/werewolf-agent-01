# Werewolf agent for `werewolf-agent-01`

A deterministic, model-free Python 3.13 player for the 8-seat Werewolf
competition. It uses only the standard library, makes no model calls, and
speaks to nothing except the contest env at `MAKEFUN_ENV_URL`.

## Run

The grader runs it as:

```sh
python3 -P -s /bot/main.py
```

It reads `MAKEFUN_ENV_URL` and `MAKEFUN_TRIAL_SEED`; `MAKEFUN_PROMPT_FILE` is
read when present but is not required by the strategy. It polls
`POST /observe`, answers every pending `speak`/`vote`/`kill`/`protect`/`inspect`
decision, and exits when the env reports `done`.

A refusal is never fatal: a 400 (`bad_target`, `empty`, `too_long`, ...) is
retried at once with the always-legal pass or abstain, and a 409 (`not_pending`)
is ignored because the game has already moved on.

## Strategy

- **Werewolf.** At night, match the partner's recorded pick when it exists (the
  only way to guarantee a kill), otherwise kill a claimed seer/doctor, an active
  accuser, or a vocal villager; the table's current scapegoat is kept alive as a
  future exile. By day, push the villager the public record already suspects,
  defend the partner only while the defense can still help, never vote the
  partner, and avoid a second fake seer claim against a first claimant. A seer
  bluff is reserved for a table that is already converging on us and has no
  real claim on the floor.
- **Seer.** Inspect suspicious or influential players and never re-inspect a
  known result. Claim on day one with a wolf find (or a clear, for doctor
  cover), and on day two with two results. A rival seer claim is treated as
  false; the known wolf is pushed and voted.
- **Doctor.** Protect the most credible claimed seer, else a trusted vocal
  villager; never self and never the same seat on consecutive nights. Stay
  hidden unless the table is actually about to exile us.
- **Villager.** Follow explicit seer findings, revealed roles, vote records and
  night-kill tells. Generic day-one pile-ons are discounted, and their authors
  become the preferred vote. Confirmed-good seats are never voted.

The public log is parsed defensively. It recognises role claims, inspection
results with or without an "I checked" verb, results split across sentences
("I checked Lena. She is a wolf."), accusations, clears, vote intents and the
revealed roles of the dead. A dead seer's explicit results are treated as
ground truth; a later seer claimant who was directly accused by an earlier
claimant is treated as the likely counterclaiming wolf.

## Determinism

Every tie-break is derived from
`sha256(MAKEFUN_TRIAL_SEED, label, game, day, seat)`. The agent reads no clock,
uses no unseeded randomness, sends one request at a time, and keeps all memory
per game. The same views therefore produce the same actions in the live pass
and the replay.

## Testing

The repository ships only `main.py`, `LICENSE` and `README.md`. During
development the agent was exercised against a local Python mock of the env
(`/tmp/opencode/werewolf_mock.py`, not part of the submission) with both a
random house and a self-play house. The mock verifies that all 30 games finish,
that every action is accepted (no 400/409 loop), and that two runs with the
same seed produce byte-identical action/state exports.
