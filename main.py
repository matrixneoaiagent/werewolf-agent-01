#!/usr/bin/env python3
"""werewolf-agent-01 entry: a deterministic, standard-library-only Werewolf player.

Protocol
--------
The agent POSTs {} to MAKEFUN_ENV_URL/observe, answers every decision waiting
on its seat, and exits as soon as the env reports ``done``.  Speak decisions
become one folded line of at most 500 characters or a pass; vote, kill, protect
and inspect decisions name a seat from the decision's options or abstain.  A
refusal (400) is retried at once with the always-legal pass/abstain; a 409 means
the game moved on and is ignored.  Requests are serial and deterministic.

Strategy
--------
* Werewolf: match the partner's night pick when it is already recorded (the
  only way to guarantee a kill), otherwise kill a claimed seer/doctor or an
  active accuser.  By day, push the villager the table already suspects, keep
  the partner out of the vote, counterclaim only a seer who directly accuses
  us, and defer to a partner who has claimed the seer floor.
* Seer: inspect suspicious or influential players, never re-inspect a known
  result, claim on day one with a wolf find (or a clear, for doctor cover), and
  push the known wolf.  A rival seer claim is treated as false.
* Doctor: protect the most credible claimed seer, else a trusted vocal
  villager; never self or the same seat on consecutive nights; stay hidden
  unless the table is about to exile us.
* Villager: follow credible seer results, revealed roles, vote records and
  night-kill tells; avoid confirmed-good seats and do not start a random
  day-one bandwagon.

Every tie-break is sha256(MAKEFUN_TRIAL_SEED, label, game, day, seat); no wall
clock, no unseeded randomness, no model calls and no network beyond the env are
used.  The public log is parsed defensively, including claims split across
sentences ("I checked Lena. She is a wolf.").
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

MAX_STATEMENT_CHARS = 500
SEATS = 8


# ---------------------------------------------------------------------------
# small deterministic helpers


def fold_statement(text):
    """Fold a statement exactly as the env does: one line, no control chars."""
    if not isinstance(text, str):
        return ""
    text = re.sub(r"[\t\n\v\f\r\u0085\u2028\u2029]+", " ", text)
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", text)
    return text.strip()


def statement_len(text):
    """Length in Unicode code points."""
    return len(text)


def cut_statement(text, limit=MAX_STATEMENT_CHARS):
    """Fold and cut a statement to at most limit code points, preferring a sentence end."""
    text = fold_statement(text)
    if statement_len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut >= limit // 2:
        return head[: cut + 1].strip()
    space = head.rfind(" ")
    if space >= limit // 2:
        return head[:space].strip()
    return head.strip()


def seeded_int(seed, *parts):
    """A stable unsigned integer from the trial seed and a label tuple."""
    h = hashlib.sha256()
    h.update(str(seed).encode("utf-8"))
    for part in parts:
        h.update(b"\x00")
        h.update(str(part).encode("utf-8"))
    return int.from_bytes(h.digest()[:8], "big")


def log(message):
    """Diagnostics go to stderr; stdout stays quiet for the harness."""
    try:
        sys.stderr.write(str(message) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# env transport


class EnvClient:
    """Serial JSON-over-HTTP client for the contest env."""

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        # A trial has no proxy; urllib's default opener would consult env vars.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(self, path, payload, timeout=90):
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read()
                try:
                    return response.status, json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    return response.status, {"raw": raw.decode("utf-8", "replace")[:500]}
        except urllib.error.HTTPError as error:
            try:
                raw = error.read()
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    body = {"raw": raw.decode("utf-8", "replace")[:500]}
            except Exception:
                body = {}
            return error.code, body
        except (urllib.error.URLError, OSError, ValueError) as error:
            return 0, {"error": str(error)}


# ---------------------------------------------------------------------------
# per-game memory


class GameMemory:
    """What the agent said and did in one game, so its story stays consistent."""

    __slots__ = ("claim", "claim_day", "results", "push", "push_day", "last_vote", "last_vote_day")

    def __init__(self):
        self.claim = None
        self.claim_day = 0
        self.results = []
        self.push = None
        self.push_day = 0
        self.last_vote = None
        self.last_vote_day = 0


# ---------------------------------------------------------------------------
# public-view analysis


SEER_CLAIM_RE = re.compile(
    r"\bi\s*(?:'m|’m|am)\s+(?:the\s+|a\s+)?seer\b"
    r"|\bi\s+claim\s+(?:to\s+be\s+)?(?:the\s+)?seer\b"
    r"|\bclaim(?:ing)?\s+(?:the\s+)?seer\b"
    r"|\bseer\s+here\b"
    r"|\bthis\s+is\s+(?:the\s+)?seer\b"
    r"|\bmy\s+role\s+is\s+(?:the\s+)?seer\b",
    re.IGNORECASE,
)
DOCTOR_CLAIM_RE = re.compile(
    r"\bi\s*(?:'m|’m|am)\s+(?:the\s+|a\s+)?doctor\b"
    r"|\bi\s+claim\s+(?:to\s+be\s+)?(?:the\s+)?doctor\b"
    r"|\bclaim(?:ing)?\s+(?:the\s+)?doctor\b"
    r"|\bdoctor\s+here\b"
    r"|\bthis\s+is\s+(?:the\s+)?doctor\b"
    r"|\bmy\s+role\s+is\s+(?:the\s+)?doctor\b",
    re.IGNORECASE,
)
VILLAGER_CLAIM_RE = re.compile(
    r"\bi\s*(?:'m|’m|am)\s+(?:a\s+|just\s+a\s+)?villager\b"
    r"|\bjust\s+a\s+villager\b",
    re.IGNORECASE,
)
WOLF_CLAIM_RE = re.compile(
    r"\bi\s*(?:'m|’m|am)\s+(?:a\s+)?werewolf\b",
    re.IGNORECASE,
)
INSPECT_RE = re.compile(
    r"\b(?:inspect(?:ed|ing|s)?|check(?:ed|ing|s)?|scan(?:ned|ning|s)?"
    r"|investigat(?:ed|ing|es)?|looked\s+at|visited)\b",
    re.IGNORECASE,
)
WOLF_WORD_RE = re.compile(r"\b(?:werewolves?|werewolf|wolves?|wolf)\b", re.IGNORECASE)
CLEAR_RE = re.compile(
    r"\b(?:not\s+(?:a\s+|the\s+)?(?:werewolf|wolf)"
    r"|isn'?t\s+(?:a\s+|the\s+)?(?:werewolf|wolf)"
    r"|aren'?t\s+(?:a\s+|the\s+)?(?:werewolf|wolf)"
    r"|innocent|cleared?|is\s+(?:a\s+|the\s+)?villager"
    r"|are\s+(?:both\s+)?villagers?|reads?\s+(?:as\s+)?(?:a\s+)?villager"
    r"|trustworthy|good\s+(?:guy|player|read)|seems?\s+good"
    r"|on\s+the\s+village\s+side)\b",
    re.IGNORECASE,
)
ACCUSE_RE = re.compile(
    r"\b(?:is|are|was|were|seems?|looks?)\s+(?:like\s+)?(?:a\s+|the\s+)?(?:werewolf|wolf|wolves)\b"
    r"|\b(?:suspect(?:s|ed)?|suspicious|wolfish|evil"
    r"|not\s+(?:a\s+)?villager|with\s+the\s+wolves"
    r"|defended\s+(?:a\s+|the\s+)?wolf|covered\s+for\s+(?:a\s+|the\s+)?wolf"
    r"|wolf\s+partner|teammate\s+of\s+(?:a\s+)?wolf)\b",
    re.IGNORECASE,
)
VOTE_INTENT_RE = re.compile(
    r"\b(?:vote|voting|exile|eliminate|remove|banish)\s+(?:for\s+)?(?:seat\s*)?",
    re.IGNORECASE,
)
NIGHT_RE = re.compile(r"\bnight\s*(\d+)\b", re.IGNORECASE)
DEATH_CONTEXT_RE = re.compile(
    r"\b(?:killed|died|dead|murdered|eaten|victim)\b", re.IGNORECASE
)
CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+")


def split_clauses(text):
    return [clause.strip() for clause in CLAUSE_SPLIT_RE.split(text) if clause.strip()]


class Analysis:
    """Everything the seat can learn from one view, parsed defensively.

    The log is public; role claims, accusations, clears, votes and revealed
    roles are the only evidence.  Nothing here depends on wall time or on any
    other game.
    """

    def __init__(self, view):
        self.view = view if isinstance(view, dict) else {}
        you = self.view.get("you")
        self.you = you if isinstance(you, dict) else {}
        self.seat = self.you.get("seat")
        self.role = self.you.get("role")
        # v3 gating: only the seer uses the v2 claim-credibility model.  Wolf,
        # doctor and villager keep v1's analysis, which the dev data shows is
        # better for those seats.
        self.claim_model_v2 = self.role == "seer"
        self.alive = bool(self.you.get("alive"))
        partner = self.you.get("partner")
        self.partner = partner if isinstance(partner, dict) else None
        self.partner_seat = self.partner.get("seat") if self.partner else None
        players = self.view.get("players")
        self.players = []
        for player in players if isinstance(players, list) else []:
            if isinstance(player, dict) and isinstance(player.get("seat"), int):
                self.players.append(player)
        self.by_seat = {player["seat"]: player for player in self.players}
        self.names = {}
        for seat, player in self.by_seat.items():
            name = player.get("name")
            self.names[seat] = name if isinstance(name, str) and name else f"seat {seat}"
        self.alive_seats = [p["seat"] for p in self.players if p.get("alive")]
        self.dead_roles = {
            p["seat"]: p.get("role")
            for p in self.players
            if not p.get("alive") and isinstance(p.get("role"), str)
        }
        self.day = self.view.get("day") if isinstance(self.view.get("day"), int) else 1
        self.phase = self.view.get("phase")
        log = self.view.get("log")
        self.log = log if isinstance(log, list) else []
        seer_results = self.view.get("seerResults")
        self.seer_results = seer_results if isinstance(seer_results, list) else []
        last_protect = self.view.get("lastProtect")
        self.last_protect = last_protect if isinstance(last_protect, dict) else None
        wolf_votes = self.view.get("wolfVotes")
        self.wolf_votes = wolf_votes if isinstance(wolf_votes, list) else []
        self.speeches = []
        self.votes = []
        self.exiles = []
        self.dawns = []
        self.debate_orders = {}
        self.voting_orders = {}
        self._parse_log()
        self.name_patterns = []
        for seat, name in self.names.items():
            if name:
                self.name_patterns.append(
                    (seat, re.compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])", re.IGNORECASE))
                )
        self.seer_claims = {}
        self.doctor_claims = {}
        self.villager_claims = {}
        self.results = []
        self.accusations = []
        self.clears = []
        self._parse_statements()

    # -- log and statements ------------------------------------------------

    def _parse_log(self):
        for entry in self.log:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            day = entry.get("day")
            if kind == "speech" and isinstance(entry.get("text"), str):
                self.speeches.append(
                    {
                        "day": day,
                        "seat": entry.get("seat"),
                        "round": entry.get("round"),
                        "text": entry["text"],
                    }
                )
            elif kind == "pass":
                self.speeches.append(
                    {"day": day, "seat": entry.get("seat"), "round": entry.get("round"), "text": ""}
                )
            elif kind == "vote":
                self.votes.append(
                    {"day": day, "seat": entry.get("seat"), "target": entry.get("target")}
                )
            elif kind == "exile":
                self.exiles.append(
                    {
                        "day": day,
                        "seat": entry.get("seat"),
                        "role": entry.get("role"),
                        "tally": entry.get("tally") if isinstance(entry.get("tally"), list) else [],
                    }
                )
            elif kind == "dawn":
                self.dawns.append(
                    {"day": day, "died": entry.get("died"), "role": entry.get("role")}
                )
            elif kind == "debate" and isinstance(entry.get("order"), list):
                self.debate_orders[day] = [s for s in entry["order"] if isinstance(s, int)]
            elif kind == "voting" and isinstance(entry.get("order"), list):
                self.voting_orders[day] = [s for s in entry["order"] if isinstance(s, int)]

    def mentioned_seats(self, text):
        found = {}
        for seat, pattern in self.name_patterns:
            match = pattern.search(text)
            if match is not None and seat not in found:
                found[seat] = match.start()
        for match in re.finditer(r"\b(?:seat|player)\s*#?\s*(\d+)\b", text, re.IGNORECASE):
            seat = int(match.group(1))
            if 1 <= seat <= SEATS:
                found.setdefault(seat, match.start())
        for match in re.finditer(r"#(\d+)\b", text):
            seat = int(match.group(1))
            if 1 <= seat <= SEATS:
                found.setdefault(seat, match.start())
        return found

    @staticmethod
    def _night_number(clause):
        match = NIGHT_RE.search(clause)
        return int(match.group(1)) if match else None

    @staticmethod
    def _is_weak(clause):
        """A social read with no concrete check, vote record or revealed role."""
        lower = clause.lower()
        return not any(
            token in lower
            for token in (
                "voted",
                "exiled",
                "defended",
                "checked",
                "inspected",
                "dead seer",
                "confirmed",
            )
        )

    @staticmethod
    def _nearest(mentions, anchor):
        if not mentions:
            return None
        return min(mentions, key=lambda seat: (abs(mentions[seat] - anchor), mentions[seat]))

    def _record_result(self, speaker, day, target, is_wolf, clause, round_no, speech_index):
        night = self._night_number(clause)
        # v2 dedupe is seer-only.  Non-seer seats keep v1's full result list.
        if self.claim_model_v2:
            for existing in self.results:
                if (
                    existing.get("speaker") == speaker
                    and existing.get("target") == target
                    and bool(existing.get("werewolf")) == bool(is_wolf)
                ):
                    return
        self.results.append(
            {
                "speaker": speaker,
                "day": day,
                "night": night,
                "target": target,
                "werewolf": bool(is_wolf),
                "explicit": True,
                "speech_index": speech_index,
            }
        )
        self.seer_claims.setdefault(speaker, []).append(
            {"day": day, "round": round_no, "text": clause}
        )
        bucket = self.accusations if is_wolf else self.clears
        bucket.append(
            {
                "day": day,
                "speaker": speaker,
                "target": target,
                "strength": 1.5,
                "kind": "inspection",
                "weak": False,
                "speech_index": speech_index,
            }
        )

    def _parse_statements(self):
        for speech_index, speech in enumerate(self.speeches):
            text = speech.get("text") or ""
            if not text:
                continue
            speaker = speech.get("seat")
            if not isinstance(speaker, int):
                continue
            day = speech.get("day")
            round_no = speech.get("round")
            speech_is_seer_claim = bool(SEER_CLAIM_RE.search(text))
            # A house player may split "I checked Lena." / "She is a wolf.";
            # remember the checked seat until the verdict sentence arrives.
            pending_inspection = None
            for clause in split_clauses(text):
                if SEER_CLAIM_RE.search(clause):
                    self.seer_claims.setdefault(speaker, []).append(
                        {"day": day, "round": round_no, "text": clause}
                    )
                if DOCTOR_CLAIM_RE.search(clause):
                    self.doctor_claims.setdefault(speaker, []).append({"day": day, "text": clause})
                if VILLAGER_CLAIM_RE.search(clause):
                    self.villager_claims.setdefault(speaker, []).append({"day": day, "text": clause})
                if WOLF_CLAIM_RE.search(clause):
                    self.villager_claims.setdefault(speaker, []).append({"day": day, "text": clause})

                mentions = self.mentioned_seats(clause)
                targets = {
                    seat: pos
                    for seat, pos in mentions.items()
                    if seat != speaker and seat in self.by_seat
                }
                inspection = INSPECT_RE.search(clause)
                wolf_word = WOLF_WORD_RE.search(clause)
                clear = CLEAR_RE.search(clause)
                death_context = DEATH_CONTEXT_RE.search(clause) is not None
                seer_context = speaker in self.seer_claims or speech_is_seer_claim
                result_claim = (
                    not death_context
                    and (wolf_word is not None or clear is not None)
                    and (
                        inspection is not None
                        or (self._night_number(clause) is not None and seer_context)
                    )
                )

                if not targets:
                    if (
                        pending_inspection is not None
                        and (wolf_word is not None or clear is not None)
                        and not death_context
                    ):
                        is_wolf = wolf_word is not None and clear is None
                        self._record_result(
                            speaker,
                            day,
                            pending_inspection,
                            is_wolf,
                            clause,
                            round_no,
                            speech_index,
                        )
                        pending_inspection = None
                    continue

                if result_claim:
                    anchor = wolf_word.start() if wolf_word is not None else clear.start()
                    target = self._nearest(targets, anchor)
                    is_wolf = wolf_word is not None and clear is None
                    self._record_result(
                        speaker, day, target, is_wolf, clause, round_no, speech_index
                    )
                    pending_inspection = None
                    continue

                if inspection is not None:
                    if death_context:
                        pending_inspection = None
                        continue
                    after = [seat for seat, pos in targets.items() if pos >= inspection.end()]
                    pending_inspection = (
                        min(after, key=lambda s: targets[s])
                        if after
                        else self._nearest(targets, inspection.start())
                    )
                    continue

                # A named sentence without an inspection starts a new subject.
                pending_inspection = None

                if clear is not None:
                    target = self._nearest(targets, clear.start())
                    self.clears.append(
                        {
                            "day": day,
                            "speaker": speaker,
                            "target": target,
                            "strength": 1.0,
                            "kind": "clear",
                            "weak": self._is_weak(clause),
                            "speech_index": speech_index,
                        }
                    )
                    continue

                accuse = ACCUSE_RE.search(clause)
                if accuse is not None:
                    anchor = wolf_word.start() if wolf_word is not None else accuse.start()
                    target = self._nearest(targets, anchor)
                    self.accusations.append(
                        {
                            "day": day,
                            "speaker": speaker,
                            "target": target,
                            "strength": 1.0,
                            "kind": "accuse",
                            "weak": self._is_weak(clause),
                            "speech_index": speech_index,
                        }
                    )
                    continue

                vote_intent = VOTE_INTENT_RE.search(clause)
                if vote_intent is not None:
                    after = [seat for seat, pos in targets.items() if pos >= vote_intent.end()]
                    target = (
                        min(after, key=lambda s: targets[s])
                        if after
                        else self._nearest(targets, vote_intent.start())
                    )
                    self.accusations.append(
                        {
                            "day": day,
                            "speaker": speaker,
                            "target": target,
                            "strength": 1.0,
                            "kind": "vote-intent",
                            "weak": self._is_weak(clause),
                            "speech_index": speech_index,
                        }
                    )

    # -- small public facts ------------------------------------------------

    def tally_today(self):
        counts = {}
        for vote in self.votes:
            if vote.get("day") == self.day and isinstance(vote.get("target"), int):
                target = vote["target"]
                counts[target] = counts.get(target, 0) + 1
        return counts

    def votes_by(self, seat, day=None):
        return [
            vote
            for vote in self.votes
            if vote.get("seat") == seat and (day is None or vote.get("day") == day)
        ]

    def speeches_by(self, seat, day=None):
        return [
            speech
            for speech in self.speeches
            if speech.get("seat") == seat and (day is None or speech.get("day") == day)
        ]

    def accusations_by(self, seat, days=None):
        if days is None:
            return [a for a in self.accusations if a.get("speaker") == seat]
        low = self.day - days + 1
        return [
            a
            for a in self.accusations
            if a.get("speaker") == seat and isinstance(a.get("day"), int) and a["day"] >= low
        ]

    def accusations_against(self, seat, days=None):
        if days is None:
            return [a for a in self.accusations if a.get("target") == seat]
        low = self.day - days + 1
        return [
            a
            for a in self.accusations
            if a.get("target") == seat and isinstance(a.get("day"), int) and a["day"] >= low
        ]

    def accusation_pressure(self, seat, days=2):
        total = 0.0
        for accusation in self.accusations_against(seat, days):
            total += float(accusation.get("strength") or 1.0)
        return total

    def weak_pile_on(self):
        """The target with the most distinct generic accusers today, if any."""
        by_target = {}
        for accusation in self.accusations:
            if accusation.get("day") != self.day or not accusation.get("weak"):
                continue
            target = accusation.get("target")
            speaker = accusation.get("speaker")
            if (
                isinstance(target, int)
                and isinstance(speaker, int)
                and speaker != self.seat
            ):
                by_target.setdefault(target, set()).add(speaker)
        best = None
        for target, speakers in by_target.items():
            if len(speakers) >= 2 and (best is None or len(speakers) > len(best[1])):
                best = (target, speakers)
        return best

    def is_claimed_seer(self, seat):
        return seat in self.seer_claims

    def is_claimed_doctor(self, seat):
        return seat in self.doctor_claims

    def claimed_seers(self, alive_only=False):
        seats = [seat for seat in self.seer_claims if isinstance(seat, int)]
        if alive_only:
            seats = [seat for seat in seats if seat in self.alive_seats]
        return seats

    # -- claim credibility -------------------------------------------------

    def _first_seer_claim_index(self, seat):
        for index, speech in enumerate(self.speeches):
            if speech.get("seat") == seat and SEER_CLAIM_RE.search(speech.get("text") or ""):
                return index
        return None

    def seer_credibility(self):
        """How much a seer claim is supported by revealed roles and votes."""
        credibility = {}
        living_claimants = [
            seat
            for seat in self.seer_claims
            if isinstance(seat, int) and seat in self.alive_seats and seat != self.seat
        ]
        multiple = len(living_claimants) >= 2
        for seat in self.seer_claims:
            if not isinstance(seat, int):
                continue
            if seat == self.seat:
                credibility[seat] = 100.0 if self.role == "seer" else 0.0
                continue
            if seat not in self.alive_seats:
                credibility[seat] = 100.0 if self.dead_roles.get(seat) == "seer" else -100.0
                continue
            if self.role == "seer":
                credibility[seat] = -100.0
                continue
            value = 1.0
            if self.claim_model_v2:
                # A player repeating the same claim in both debate rounds is
                # not new evidence.  Count each unique target/verdict once.
                unique_results = {}
                for result in self.results:
                    if result.get("speaker") != seat:
                        continue
                    key = (
                        result.get("target"),
                        bool(result.get("werewolf")),
                    )
                    unique_results[key] = result
                for result in unique_results.values():
                    target = result.get("target")
                    if target in self.dead_roles:
                        actual_wolf = self.dead_roles[target] == "werewolf"
                        if result.get("werewolf") and actual_wolf:
                            value += 2.0
                        elif result.get("werewolf") and not actual_wolf:
                            value -= 3.0
                        elif not result.get("werewolf") and actual_wolf:
                            value -= 3.0
                        else:
                            # A wolf can truthfully clear a villager too, so
                            # only a confirmed wolf hit is proof.
                            value += 0.0
                    else:
                        value += 0.2
            else:
                # v1 scoring for wolf, doctor and villager.
                for result in self.results:
                    if result.get("speaker") != seat:
                        continue
                    target = result.get("target")
                    if target in self.dead_roles:
                        actual_wolf = self.dead_roles[target] == "werewolf"
                        if result.get("werewolf") and actual_wolf:
                            value += 2.0
                        elif result.get("werewolf") and not actual_wolf:
                            value -= 3.0
                        elif not result.get("werewolf") and actual_wolf:
                            value -= 3.0
                        else:
                            value += 1.0
                    else:
                        value += 0.3
            for accusation in self.accusations:
                if accusation.get("speaker") != seat:
                    continue
                target = accusation.get("target")
                if target in self.dead_roles:
                    if self.dead_roles[target] == "werewolf":
                        value += 1.0
                    else:
                        value -= 0.5
            for vote in self.votes:
                if vote.get("seat") != seat or vote.get("target") is None:
                    continue
                target = vote["target"]
                if target in self.dead_roles:
                    if self.dead_roles[target] == "werewolf":
                        value += 0.5
                    else:
                        value -= 0.3

            # A claim that appears only after the claimant is under pressure is
            # more likely a wolf's escape hatch than a seer's first reveal.
            claim_days = [
                entry.get("day")
                for entry in self.seer_claims.get(seat, [])
                if isinstance(entry.get("day"), int)
            ]
            first_claim_day = min(claim_days) if claim_days else None
            if first_claim_day is not None and first_claim_day > 1:
                value -= 0.4
                accused_before = any(
                    accusation.get("target") == seat
                    and isinstance(accusation.get("day"), int)
                    and accusation["day"] < first_claim_day
                    for accusation in self.accusations
                )
                if accused_before:
                    value -= 0.8

            # A dead seer's explicit clear is public truth; calling that player
            # a wolf cannot be a real inspection.
            for other in self.results:
                other_speaker = other.get("speaker")
                if other_speaker == seat:
                    continue
                if not (
                    other_speaker in self.dead_roles
                    and self.dead_roles.get(other_speaker) == "seer"
                    and other.get("explicit")
                ):
                    continue
                cleared = other.get("target")
                if other.get("werewolf") or cleared not in self.alive_seats:
                    continue
                if any(
                    result.get("speaker") == seat
                    and result.get("werewolf")
                    and result.get("target") == cleared
                    for result in self.results
                ):
                    value -= 5.0

            # A later claimant who was directly accused by an earlier seer
            # claimant is usually the wolf counterclaiming, not the real seer.
            claim_index = self._first_seer_claim_index(seat)
            if claim_index is not None:
                counter_penalty = 0.0
                for other in self.seer_claims:
                    if other == seat:
                        continue
                    other_index = self._first_seer_claim_index(other)
                    if other_index is None or other_index >= claim_index:
                        continue
                    direct = False
                    for accusation in self.accusations:
                        if accusation.get("speaker") != other or accusation.get("target") != seat:
                            continue
                        acc_index = accusation.get("speech_index")
                        if isinstance(acc_index, int) and acc_index <= claim_index:
                            direct = True
                            break
                    for result in self.results:
                        if (
                            result.get("speaker") == other
                            and result.get("target") == seat
                            and result.get("werewolf")
                        ):
                            res_index = result.get("speech_index")
                            if isinstance(res_index, int) and res_index <= claim_index:
                                direct = True
                                counter_penalty = max(counter_penalty, 1.5)
                                break
                    if direct:
                        counter_penalty = max(counter_penalty, 1.0)
                value -= counter_penalty

            # Base-rate prior.  A real seer draws a clear 5 times in 7 on the
            # first night, while a wolf who fake-claims wants a body to bury.
            # When two players claim the role, the claim whose public result is
            # only a clear is the more plausible story.
            claimed_wolf = any(
                result.get("speaker") == seat and result.get("werewolf")
                for result in self.results
            )
            claimed_clear = any(
                result.get("speaker") == seat and not result.get("werewolf")
                for result in self.results
            )
            if multiple:
                if claimed_wolf and not claimed_clear:
                    value -= 0.4
                elif claimed_clear and not claimed_wolf:
                    value += 0.7
            else:
                value += 0.5
            credibility[seat] = value
        return credibility

    def credible_seer_claims(self):
        return {
            seat: value
            for seat, value in self.seer_credibility().items()
            if seat in self.alive_seats and value > 0.5
        }

    def best_seer_claim(self):
        claims = self.credible_seer_claims()
        if not claims:
            return None
        if not self.claim_model_v2:
            # v1 tie-break for wolf, doctor and villager.
            return max(claims, key=lambda seat: (claims[seat], -seat))
        # v2: credibility only decides when it is clearly better.  Two
        # near-identical stories are separated by who took the floor first.
        top = max(claims.values())
        contenders = [seat for seat, value in claims.items() if value >= top - 0.25]

        def key(seat):
            index = self._first_seer_claim_index(seat)
            return (-(index if index is not None else 10**9), -seat)

        return max(contenders, key=key)

    def dead_seer_revealed(self):
        return any(role == "seer" for role in self.dead_roles.values())

    def clear_seer_conflict(self):
        """Two or more credible living seer claims, none with a wolf result."""
        credibility = self.seer_credibility()
        claims = [
            seat
            for seat in self.claimed_seers(alive_only=True)
            if seat != self.seat and credibility.get(seat, 0) > 0.5
        ]
        if len(claims) < 2:
            return False
        for seat in claims:
            if any(
                result.get("speaker") == seat and result.get("werewolf")
                for result in self.results
            ):
                return False
        return True

    # -- exact knowledge ---------------------------------------------------

    def exact_known(self):
        """Wolves and cleared players known from our own or a dead seer's results."""
        known_wolves = set()
        known_good = set()
        if self.role == "seer":
            for result in self.seer_results:
                if not isinstance(result, dict):
                    continue
                target = result.get("seat")
                if not isinstance(target, int) or target not in self.alive_seats:
                    continue
                if result.get("werewolf"):
                    known_wolves.add(target)
                else:
                    known_good.add(target)
        for result in self.results:
            speaker = result.get("speaker")
            if speaker in self.dead_roles and self.dead_roles.get(speaker) == "seer" and result.get("explicit"):
                target = result.get("target")
                if not isinstance(target, int) or target not in self.alive_seats:
                    continue
                if result.get("werewolf"):
                    known_wolves.add(target)
                else:
                    known_good.add(target)
        known_wolves.discard(self.seat)
        known_good.discard(self.seat)
        return known_wolves, known_good - known_wolves

    # -- suspicion ---------------------------------------------------------

    def speaker_weight(self, speaker, public=False):
        if speaker == self.seat:
            return 0.0
        if speaker in self.dead_roles:
            role = self.dead_roles[speaker]
            if role == "seer":
                return 3.0
            if role == "werewolf":
                return -3.0
            return 1.5
        weight = 1.0
        claims = self.seer_credibility()
        if speaker in claims:
            rivals = [
                value
                for seat, value in claims.items()
                if seat in self.alive_seats and seat != self.seat
            ]
            best = max(rivals, default=0.0)
            if claims[speaker] < -0.5:
                weight -= 1.5
            elif claims[speaker] < best - 0.8:
                # A second, weaker seer claim is more likely the wolf's story.
                weight -= 1.5
            elif claims[speaker] >= best - 0.3:
                weight += 1.5
            else:
                weight += 0.3
        for vote in self.votes:
            if vote.get("seat") != speaker or vote.get("target") is None:
                continue
            target = vote["target"]
            if target in self.dead_roles:
                if self.dead_roles[target] == "werewolf":
                    weight += 0.5
                else:
                    weight -= 0.3
        for accusation in self.accusations:
            if (
                accusation.get("target") == speaker
                and accusation.get("speaker") in self.dead_roles
                and self.dead_roles.get(accusation.get("speaker")) == "seer"
            ):
                weight -= 1.0
                break
        return max(-3.0, min(4.0, weight))

    def suspicion_scores(self, public=False):
        score = {seat: 0.0 for seat in self.alive_seats if seat != self.seat}
        if not score:
            return score

        # Our own seer results are exact.
        if not public and self.role == "seer":
            for result in self.seer_results:
                if not isinstance(result, dict):
                    continue
                target = result.get("seat")
                if target in score:
                    score[target] += 1000.0 if result.get("werewolf") else -1000.0

        # A dead seer's explicit results are exact.
        for result in self.results:
            speaker = result.get("speaker")
            if speaker == self.seat:
                continue
            if speaker in self.dead_roles and self.dead_roles.get(speaker) == "seer":
                target = result.get("target")
                if target in score:
                    if result.get("explicit"):
                        score[target] += 500.0 if result.get("werewolf") else -500.0
                    else:
                        score[target] += 25.0 if result.get("werewolf") else -25.0

        # A dead seer's ordinary accusations and clears are strong evidence.
        for accusation in self.accusations:
            speaker = accusation.get("speaker")
            if speaker in self.dead_roles and self.dead_roles.get(speaker) == "seer":
                if accusation.get("target") in score:
                    score[accusation["target"]] += 20.0
        for clear in self.clears:
            speaker = clear.get("speaker")
            if speaker in self.dead_roles and self.dead_roles.get(speaker) == "seer":
                if clear.get("target") in score:
                    score[clear["target"]] -= 20.0

        # A dead wolf's words are usually misdirection.
        for accusation in self.accusations:
            speaker = accusation.get("speaker")
            if speaker in self.dead_roles and self.dead_roles.get(speaker) == "werewolf":
                if accusation.get("target") in score:
                    score[accusation["target"]] -= 8.0
        for clear in self.clears:
            speaker = clear.get("speaker")
            if speaker in self.dead_roles and self.dead_roles.get(speaker) == "werewolf":
                if clear.get("target") in score:
                    score[clear["target"]] += 12.0

        # Living seer claims, weighted by their credibility.
        credibility = self.seer_credibility()
        for seat, value in credibility.items():
            if seat == self.seat or seat not in self.alive_seats or value <= 0.5:
                continue
            weight = min(value, 3.0) * 4.0
            for result in self.results:
                if result.get("speaker") != seat:
                    continue
                target = result.get("target")
                if target in score:
                    score[target] += weight * 1.5 if result.get("werewolf") else -weight * 1.5
            for accusation in self.accusations:
                if (
                    accusation.get("speaker") == seat
                    and accusation.get("kind") != "inspection"
                    and accusation.get("target") in score
                ):
                    score[accusation["target"]] += weight * 0.5
            for clear in self.clears:
                if (
                    clear.get("speaker") == seat
                    and clear.get("kind") != "inspection"
                    and clear.get("target") in score
                ):
                    score[clear["target"]] -= weight * 0.5

        # Ordinary public accusations and clears.
        for accusation in self.accusations:
            target = accusation.get("target")
            speaker = accusation.get("speaker")
            if target not in score or speaker == self.seat:
                continue
            if speaker in self.dead_roles and self.dead_roles.get(speaker) == "seer":
                continue
            weight = 1.0 if public else (0.45 if accusation.get("weak") else 1.0)
            score[target] += (
                float(accusation.get("strength") or 1.0)
                * self.speaker_weight(speaker, public)
                * 2.0
                * weight
            )
        for clear in self.clears:
            target = clear.get("target")
            speaker = clear.get("speaker")
            if target not in score or speaker == self.seat:
                continue
            if speaker in self.dead_roles and self.dead_roles.get(speaker) == "seer":
                continue
            weight = 1.0 if public else (0.6 if clear.get("weak") else 1.0)
            score[target] -= (
                float(clear.get("strength") or 1.0)
                * self.speaker_weight(speaker, public)
                * 2.0
                * weight
            )

        # Before any seer result, a coordinated generic pile-on is a wolf's
        # easiest opening.  Discount the target and look harder at the pushers.
        if not public and self.day <= 2 and not self.credible_seer_claims():
            known_wolves, _ = self.exact_known()
            if not known_wolves:
                weak_by_target = {}
                for accusation in self.accusations:
                    if accusation.get("day") != self.day or not accusation.get("weak"):
                        continue
                    target = accusation.get("target")
                    speaker = accusation.get("speaker")
                    if target in score and isinstance(speaker, int) and speaker != self.seat:
                        weak_by_target.setdefault(target, set()).add(speaker)
                for target, speakers in weak_by_target.items():
                    if len(speakers) < 2:
                        continue
                    penalty = min(3.0, float(len(speakers)))
                    score[target] -= penalty
                    for speaker in speakers:
                        if speaker in score:
                            score[speaker] += penalty

        # Votes by players whose role is known.
        for vote in self.votes:
            voter = vote.get("seat")
            target = vote.get("target")
            if target is None:
                continue
            if voter in self.dead_roles:
                role = self.dead_roles[voter]
                if role == "werewolf":
                    if target in score:
                        score[target] -= 4.0
                elif role in ("villager", "doctor", "seer"):
                    if target in score:
                        score[target] += 3.0

        # How living players treated players whose roles are now known.
        for exiled in self.exiles:
            exiled_seat = exiled.get("seat")
            exiled_role = exiled.get("role")
            if not isinstance(exiled_seat, int) or exiled_role is None:
                continue
            for accusation in self.accusations:
                speaker = accusation.get("speaker")
                if accusation.get("target") != exiled_seat or speaker == self.seat or speaker not in score:
                    continue
                if exiled_role == "werewolf":
                    score[speaker] -= 4.0
                elif exiled_role in ("villager", "doctor", "seer"):
                    score[speaker] += 1.5
            for clear in self.clears:
                speaker = clear.get("speaker")
                if clear.get("target") != exiled_seat or speaker == self.seat or speaker not in score:
                    continue
                if exiled_role == "werewolf":
                    score[speaker] += 8.0
                elif exiled_role in ("villager", "doctor", "seer"):
                    score[speaker] -= 1.0

        # A villager killed at night usually accused a wolf the day before.
        for dawn in self.dawns:
            died = dawn.get("died")
            role = dawn.get("role")
            if not isinstance(died, int) or role not in ("villager", "doctor", "seer"):
                continue
            for accusation in self.accusations:
                if accusation.get("speaker") == died and accusation.get("target") in score:
                    score[accusation["target"]] += 6.0
            for clear in self.clears:
                if clear.get("speaker") == died and clear.get("target") in score:
                    score[clear["target"]] -= 4.0

        # A claim that our own role proves false, or that contradicts the dead.
        if not public:
            if self.role == "seer":
                for seat in self.seer_claims:
                    if seat != self.seat and seat in score:
                        score[seat] += 40.0
            if self.role == "doctor":
                for seat in self.doctor_claims:
                    if seat != self.seat and seat in score:
                        score[seat] += 20.0
        for seat, value in credibility.items():
            if seat in score and value < -0.5:
                score[seat] += 40.0

        # When two players claim seer and one has only produced a body to bury
        # while the other has only cleared someone, the aggressive claimant is
        # the likelier wolf.  Make that claimant a real vote candidate.
        living_claim_values = [
            value
            for seat, value in credibility.items()
            if seat in self.alive_seats and seat != self.seat
        ]
        best_claim = max(living_claim_values, default=0.0)
        for seat, value in credibility.items():
            if seat not in score or seat == self.seat or value >= best_claim - 0.8:
                continue
            has_wolf = any(
                result.get("speaker") == seat and result.get("werewolf")
                for result in self.results
            )
            has_clear = any(
                result.get("speaker") == seat and not result.get("werewolf")
                for result in self.results
            )
            if has_wolf and not has_clear:
                score[seat] += 15.0

        for seat in list(score):
            score[seat] = max(-200.0, min(1000.0, score[seat]))
        return score

    # -- evidence phrases for statements ----------------------------------

    def evidence_against(self, target):
        """Short, public reasons a player looks like a wolf."""
        phrases = []
        for accusation in self.accusations:
            if (
                accusation.get("target") == target
                and accusation.get("speaker") in self.dead_roles
                and self.dead_roles.get(accusation.get("speaker")) == "seer"
            ):
                phrases.append("the dead seer named them")
                break
        for clear in self.clears:
            if clear.get("speaker") == target:
                cleared = clear.get("target")
                if cleared in self.dead_roles and self.dead_roles.get(cleared) == "werewolf":
                    phrases.append("they defended %s, a wolf" % self.names.get(cleared, "a wolf"))
        for vote in self.votes:
            if vote.get("seat") == target:
                voted = vote.get("target")
                if voted in self.dead_roles and self.dead_roles.get(voted) in ("villager", "doctor", "seer"):
                    phrases.append("they voted out %s, a villager" % self.names.get(voted, "a villager"))
        for accusation in self.accusations:
            if (
                accusation.get("speaker") == target
                and accusation.get("target") in self.dead_roles
                and self.dead_roles.get(accusation.get("target")) == "seer"
            ):
                phrases.append("they went after the seer")
        best_claim = self.best_seer_claim()
        for result in self.results:
            if (
                result.get("speaker") == best_claim
                and result.get("target") == target
                and result.get("werewolf")
            ):
                phrases.append("the seer's read is on them")
        if self.role == "seer" and target in self.seer_claims:
            phrases.append("their seer claim is false")
        if self.role == "doctor" and target in self.doctor_claims:
            phrases.append("their doctor claim is false")
        known_wolves, known_good = self.exact_known()
        for accusation in self.accusations:
            if accusation.get("speaker") == target and accusation.get("target") in known_good:
                phrases.append(
                    "they pushed %s, a cleared player"
                    % self.names.get(accusation["target"], "a cleared player")
                )
        for vote in self.votes:
            if vote.get("seat") == target and vote.get("target") in known_good:
                phrases.append(
                    "they voted for %s, a cleared player"
                    % self.names.get(vote["target"], "a cleared player")
                )
        for exiled in self.exiles:
            if exiled.get("role") != "werewolf":
                continue
            for vote in self.votes:
                if (
                    vote.get("day") == exiled.get("day")
                    and vote.get("seat") == target
                    and vote.get("target") is None
                ):
                    phrases.append("they sat out the vote on a wolf")
        for accusation in self.accusations:
            if accusation.get("speaker") == target and accusation.get("target") == self.seat:
                phrases.append("they are pushing me")
        if not phrases:
            phrases.append("their votes keep helping the wolves")
        return phrases


# ---------------------------------------------------------------------------
# the player


class Agent:
    """The entry's player.  One instance handles every live game."""

    def __init__(self, seed):
        self.seed = seed
        self.memory = {}
        self.decisions = 0

    def memory_for(self, game):
        memory = self.memory.get(game)
        if memory is None:
            memory = GameMemory()
            self.memory[game] = memory
        return memory

    # -- decision entry point ---------------------------------------------

    def act_for(self, pending):
        """Return (action_body, meta) for one pending decision."""
        game = pending.get("game")
        decision = pending.get("decision")
        view = pending.get("view")
        if not isinstance(game, int) or not isinstance(view, dict):
            return self.fallback(pending), None

        if decision == "speak":
            text, meta = self.speak(game, view, pending)
            if text is None:
                return {"game": game, "decision": "speak", "pass": True}, meta
            text = cut_statement(text, pending.get("max_chars") or MAX_STATEMENT_CHARS)
            if not text:
                return {"game": game, "decision": "speak", "pass": True}, meta
            return {"game": game, "decision": "speak", "text": text}, meta

        if decision in ("vote", "kill", "protect", "inspect"):
            options = pending.get("options")
            if not isinstance(options, list):
                options = []
            options = [s for s in options if isinstance(s, int)]
            target = self.choose_target(game, view, decision, options)
            if target not in options:
                target = None
            return {"game": game, "decision": decision, "target": target}, None

        return self.fallback(pending), None

    @staticmethod
    def fallback(pending):
        game = pending.get("game")
        decision = pending.get("decision")
        if decision == "speak":
            return {"game": game, "decision": "speak", "pass": True}
        return {"game": game, "decision": decision, "target": None}

    # -- speaking ----------------------------------------------------------

    def speak(self, game, view, pending):
        analysis = Analysis(view)
        if analysis.role == "werewolf":
            return self.speak_wolf(game, analysis)
        if analysis.role == "seer":
            return self.speak_seer(game, analysis)
        if analysis.role == "doctor":
            return self.speak_doctor(game, analysis)
        return self.speak_villager(game, analysis)

    def _seer_results(self, analysis):
        results = []
        for result in analysis.seer_results:
            if isinstance(result, dict) and isinstance(result.get("seat"), int):
                night = result.get("night")
                results.append(
                    (
                        night if isinstance(night, int) else 0,
                        result["seat"],
                        bool(result.get("werewolf")),
                    )
                )
        results.sort(key=lambda item: (item[0], item[1]))
        return results

    def _add_fake_result(self, analysis, results, target, want_wolf):
        """Add one invented night result without mutating committed memory."""
        if not isinstance(target, int) or target in (analysis.seat, analysis.partner_seat):
            return False
        if target not in analysis.alive_seats:
            return False
        if any(seat == target for night, seat, is_wolf in results):
            return False
        used = {night for night, seat, is_wolf in results if isinstance(night, int)}
        for night in range(analysis.day, 0, -1):
            if night not in used:
                results.append((night, target, bool(want_wolf)))
                results.sort(key=lambda item: (item[0], item[1]))
                return True
        return False

    def _choose_true_clear(self, analysis, results):
        candidates = [
            seat
            for seat in analysis.alive_seats
            if seat not in (analysis.seat, analysis.partner_seat)
            and not any(result_seat == seat for night, result_seat, is_wolf in results)
        ]
        if not candidates:
            return None
        public = analysis.suspicion_scores(public=True)
        return min(
            candidates,
            key=lambda seat: (public.get(seat, 0), seeded_int(self.seed, "clear", seat, analysis.day)),
        )

    def _wolf_exposed(self, analysis, memory):
        """Whether the public record has already proved the fake seer story false."""
        if memory.claim != "seer":
            return False
        for dead_seat, role in analysis.dead_roles.items():
            if role == "seer" and dead_seat != analysis.seat:
                return True
        for night, target, claimed_wolf in memory.results:
            if target in analysis.dead_roles:
                actual_wolf = analysis.dead_roles[target] == "werewolf"
                if bool(claimed_wolf) != actual_wolf:
                    return True
        return False

    def _wolf_seer_text(self, analysis, results, push, hostile=None):
        parts = ["I'm the seer."]
        shown = sorted(results, key=lambda item: (item[0], item[1]))[-4:]
        for night, target, is_wolf in shown:
            if target not in analysis.names:
                continue
            night_text = str(night) if night else "last night"
            parts.append(
                "Night %s I checked %s: %s." % (
                    night_text,
                    analysis.names[target],
                    "werewolf" if is_wolf else "not a wolf",
                )
            )
        if isinstance(hostile, int) and hostile in analysis.names:
            parts.append("%s is lying." % analysis.names[hostile])
        if isinstance(push, int) and push in analysis.names:
            has_fake_wolf_result = any(
                target == push and is_wolf for night, target, is_wolf in results
            )
            if not has_fake_wolf_result and push != hostile:
                reasons = analysis.evidence_against(push)
                if reasons:
                    parts.append(
                        "The case on %s: %s."
                        % (analysis.names[push], reasons[0])
                    )
            parts.append("Vote %s." % analysis.names[push])
        return " ".join(parts)

    def _wolf_scarecrow(self, analysis, exclude=None):
        excluded = set(exclude or ())
        excluded.add(analysis.seat)
        excluded.add(analysis.partner_seat)
        candidates = [seat for seat in analysis.alive_seats if seat not in excluded]
        if not candidates:
            return None
        public = analysis.suspicion_scores(public=True)

        def score(seat):
            value = 0.8 * public.get(seat, 0)
            value += 3.0 * analysis.accusation_pressure(seat, days=2)
            if analysis.is_claimed_seer(seat):
                value += 5.0
            if analysis.is_claimed_doctor(seat):
                value += 4.0
            return value

        return min(
            candidates,
            key=lambda seat: (-score(seat), seeded_int(self.seed, "scarecrow", seat, analysis.day)),
        )

    def _wolf_reason(self, analysis, target):
        phrases = analysis.evidence_against(target)
        if not phrases:
            phrases = ["their pushes keep landing on villagers"]
        name = analysis.names.get(target, "seat %d" % target)
        return "%s: %s." % (name, "; ".join(phrases[:2]))

    def _public_reason(self, analysis, target):
        phrases = analysis.evidence_against(target)
        strong = [
            phrase
            for phrase in phrases
            if phrase not in ("they are pushing me", "their votes keep helping the wolves")
        ]
        if strong:
            phrases = strong
        if not phrases:
            phrases = ["their voting record keeps helping the wolves"]
        name = analysis.names.get(target, "seat %d" % target)
        return "%s: %s." % (name, "; ".join(phrases[:2]))

    def _push_village(self, game, analysis, known_wolves, known_good, memory):
        candidates = [
            seat for seat in analysis.alive_seats if seat != analysis.seat and seat not in known_good
        ]
        if not candidates:
            candidates = [seat for seat in analysis.alive_seats if seat != analysis.seat]
        if not candidates:
            return None
        tally = analysis.tally_today()
        if known_wolves:
            wolves = [seat for seat in known_wolves if seat in candidates]
            if wolves:
                public = analysis.suspicion_scores(public=True)
                return min(
                    wolves,
                    key=lambda seat: (
                        -(2.0 * tally.get(seat, 0) + public.get(seat, 0)),
                        seeded_int(self.seed, game, "pushwolf", seat),
                    ),
                )
        suspicion = analysis.suspicion_scores()

        # A credible living seer's explicit wolf result is the strongest lead.
        # If two players claim the role, follow only the stronger story.
        credibility = analysis.credible_seer_claims()
        best_cred = max(credibility.values(), default=0.0)
        best_claim_seat = analysis.best_seer_claim()
        clear_conflict = analysis.clear_seer_conflict()
        leads = []
        if (
            isinstance(best_claim_seat, int)
            and best_claim_seat != analysis.seat
            and best_claim_seat in credibility
        ):
            cred = credibility[best_claim_seat]
            for result in analysis.results:
                if (
                    result.get("speaker") == best_claim_seat
                    and result.get("werewolf")
                    and result.get("target") in candidates
                ):
                    leads.append((result["target"], cred + 5.0, 2.0))
            # A seer's ordinary suspicion is not an inspection result; it is
            # already counted, with modest weight, by suspicion_scores().
        if leads:
            return min(
                leads,
                key=lambda item: (
                    -(item[1] + item[2] + 0.1 * suspicion.get(item[0], 0)),
                    seeded_int(self.seed, game, "lead", item[0]),
                ),
            )[0]

        def score(seat):
            value = suspicion.get(seat, 0)
            if seat in credibility and credibility[seat] >= best_cred - 0.15:
                value -= 30.0
            if clear_conflict and seat in credibility:
                value -= 60.0
            if memory.push == seat:
                value += 2.0
            return value

        return min(
            candidates,
            key=lambda seat: (-score(seat), seeded_int(self.seed, game, "push", seat)),
        )

    def _villager_line(self, analysis, push, known_wolves, known_good, memory):
        seat = analysis.seat
        if push is None:
            return "I'm a villager. I have no strong read yet; I'll follow the seer."
        name = analysis.names.get(push, "seat %d" % push)
        reasons = self._public_reason(analysis, push)
        pile = analysis.weak_pile_on() if not analysis.credible_seer_claims() else None
        if pile is not None:
            pile_target, pushers = pile
            if push in pushers:
                target_name = analysis.names.get(pile_target, "someone")
                return (
                    "I'm a villager. %s is being piled on with thin evidence; "
                    "the pushers are the wolves. %s is one of them. Vote %s."
                    % (target_name, name, name)
                )
        if known_wolves and push in known_wolves:
            return "I'm a villager. The seer's result is clear: %s is a werewolf. Vote %s." % (name, name)
        clear_conflict = analysis.clear_seer_conflict()
        claims = set(analysis.claimed_seers(alive_only=True)) - {seat}
        if clear_conflict and push is not None and push not in claims:
            return (
                "I'm a villager. Two seer claims and only clears; I'm not voting "
                "either until one names a wolf. %s Vote %s." % (reasons, name)
            )
        seer = analysis.best_seer_claim()
        if isinstance(seer, int) and seer != seat:
            for result in analysis.results:
                if (
                    result.get("speaker") == seer
                    and result.get("werewolf")
                    and result.get("target") == push
                ):
                    seer_name = analysis.names.get(seer, "seat %d" % seer)
                    return "I'm a villager. I believe %s's seer claim: %s is a werewolf. Vote %s." % (
                        seer_name,
                        name,
                        name,
                    )
            # The seer has no wolf result on this target.  Do not borrow the
            # authority of an inspection that never happened.
        accusers = [
            accusation
            for accusation in analysis.accusations
            if accusation.get("target") == seat and accusation.get("day") == analysis.day
        ]
        if accusers:
            accuser = accusers[-1].get("speaker")
            if isinstance(accuser, int) and accuser != seat:
                accuser_name = analysis.names.get(accuser, "seat %d" % accuser)
                return "I'm a villager. %s is pushing me on nothing; the real wolf is %s. %s Vote %s." % (
                    accuser_name,
                    name,
                    reasons,
                    name,
                )
        return "I'm a villager. %s Vote %s." % (reasons, name)

    def _choose_push_from_wolves(self, analysis, wolves):
        tally = analysis.tally_today()
        public = analysis.suspicion_scores(public=True)
        return min(
            wolves,
            key=lambda seat: (
                -(2.0 * tally.get(seat, 0) + public.get(seat, 0)),
                seeded_int(self.seed, "seerwolf", seat, analysis.day),
            ),
        )

    def speak_seer(self, game, analysis):
        memory = self.memory_for(game)
        results = self._seer_results(analysis)
        known_wolves, known_good = analysis.exact_known()
        alive = set(analysis.alive_seats)
        impostors = [seat for seat in analysis.claimed_seers(alive_only=True) if seat != analysis.seat]
        wolf_targets = [target for night, target, is_wolf in results if is_wolf and target in alive]
        pressure = analysis.accusation_pressure(analysis.seat, days=1)

        # A day-one clear is worth publishing for the doctor's protection, but
        # it is not a wolf read: claim the role, share the clear, and leave the
        # vote to the table rather than leading a mis-exile.
        if not wolf_targets and not impostors and pressure < 2.0 and analysis.day < 2:
            # Take the seer floor before a wolf can.  Share the clear, ask the
            # doctor for cover, and warn off a counterclaim, but do not invent
            # a wolf read or push a social suspect.
            parts = ["I'm the seer.", "Doctor, please protect me tonight."]
            for night, target, is_wolf in results:
                if target in analysis.names:
                    parts.append(
                        "Night %s: %s is not a wolf."
                        % (str(night) if night else "last night", analysis.names[target])
                    )
            parts.append("Anyone who counterclaims me is the wolf.")
            parts.append("I don't have a wolf yet; I'll vote with the table.")
            meta_results = [
                (night if isinstance(night, int) else 0, target, is_wolf)
                for night, target, is_wolf in results
            ]
            return " ".join(parts), {"claim": "seer", "results": meta_results, "push": None}

        if wolf_targets:
            push = self._choose_push_from_wolves(analysis, wolf_targets)
        elif impostors:
            suspicion = analysis.suspicion_scores()
            push = min(
                impostors,
                key=lambda seat: (
                    -suspicion.get(seat, 0),
                    seeded_int(self.seed, game, "impostor", seat),
                ),
            )
        else:
            # With only clears there is no wolf to push.  Naming a social
            # suspect under seer authority too often turns a clear claim into
            # a mis-exile; leave the vote to the table and keep the clears.
            push = None

        parts = ["I'm the seer.", "Doctor, please protect me tonight."]
        if analysis.day >= 2 and memory.claim != "seer":
            parts.append("I stayed quiet to survive.")
        shown = [result for result in results if result[2]]
        shown.extend(result for result in results if not result[2] and result[1] in alive)
        for night, target, is_wolf in shown[:3]:
            if target not in analysis.names:
                continue
            night_text = str(night) if night else "last night"
            parts.append(
                "Night %s: %s %s." % (
                    night_text,
                    analysis.names[target],
                    "is a werewolf" if is_wolf else "is not a wolf",
                )
            )
        if not wolf_targets and not impostors:
            parts.append("Anyone who counterclaims me is the wolf.")
            parts.append("I have no wolf yet; I'll vote with the table.")
        if wolf_targets:
            parts.append("That is a confirmed werewolf.")
        elif push is not None:
            reasons = analysis.evidence_against(push)
            if reasons:
                parts.append("The case on %s: %s." % (
                    analysis.names.get(push, "seat %d" % push),
                    reasons[0],
                ))
        impostors = [seat for seat in analysis.claimed_seers(alive_only=True) if seat != analysis.seat]
        if impostors:
            if push in impostors:
                parts.append("%s is lying." % analysis.names.get(push, "that claimant"))
            else:
                parts.append(
                    "%s also claims seer, but I am the real one."
                    % analysis.names.get(impostors[0], "another player")
                )
        if push is not None:
            parts.append("Vote %s." % analysis.names.get(push, "seat %d" % push))
        if wolf_targets:
            parts.append("If I die tonight, remember this.")
        meta_results = [(night if isinstance(night, int) else 0, target, is_wolf) for night, target, is_wolf in results]
        return " ".join(parts), {"claim": "seer", "results": meta_results, "push": push}

    def speak_wolf(self, game, analysis):
        memory = self.memory_for(game)
        seat = analysis.seat
        partner = analysis.partner_seat
        alive = set(analysis.alive_seats)
        day = analysis.day
        claimants = [
            claimant
            for claimant in analysis.claimed_seers(alive_only=True)
            if claimant not in (seat, partner)
        ]
        self_pressure = analysis.accusation_pressure(seat, days=2)
        partner_pressure = (
            analysis.accusation_pressure(partner, days=2) if partner in alive else 0.0
        )
        hostile = None
        hostile_strength = 0.0
        for claimant in claimants:
            strength = 0.0
            for accusation in analysis.accusations:
                if accusation.get("speaker") == claimant and accusation.get("target") in (seat, partner):
                    if isinstance(accusation.get("day"), int) and accusation["day"] >= day - 1:
                        strength += float(accusation.get("strength") or 1.0)
            if strength > hostile_strength:
                hostile = claimant
                hostile_strength = strength
        exposed = self._wolf_exposed(analysis, memory)

        # If the partner has the seer floor, back their story instead of
        # producing a second wolf claim.  Two wolves claiming seer is fatal.
        partner_claimed_seer = (
            partner in analysis.seer_claims
            and partner in alive
            and not analysis.dead_seer_revealed()
        )
        if partner_claimed_seer and not exposed:
            partner_target = None
            for accusation in analysis.accusations:
                if accusation.get("speaker") != partner:
                    continue
                target = accusation.get("target")
                if target not in alive or target in (seat, partner):
                    continue
                if any(
                    result.get("speaker") == partner
                    and result.get("target") == target
                    and not result.get("werewolf")
                    for result in analysis.results
                ):
                    continue
                partner_target = target
            push = partner_target if partner_target is not None else self._wolf_scarecrow(analysis)
            partner_name = analysis.names.get(partner, "my partner")
            if push is None:
                return "I'm a villager. I believe %s's seer claim." % partner_name, {
                    "claim": "villager",
                    "push": None,
                }
            reason = self._wolf_reason(analysis, push)
            push_name = analysis.names.get(push, "seat %d" % push)
            if memory.claim == "seer":
                text = "I'm a villager. I bluffed seer to draw the kill; %s is the real seer. %s Vote %s." % (
                    partner_name,
                    reason,
                    push_name,
                )
            else:
                text = "I'm a villager. I believe %s's seer claim. %s Vote %s." % (
                    partner_name,
                    reason,
                    push_name,
                )
            return text, {"claim": "villager", "push": push}

        if memory.claim == "seer" and not exposed:
            fake_results = list(memory.results)
            cleared_targets = {
                target for night, target, is_wolf in fake_results if not is_wolf
            }
            push = (
                memory.push
                if memory.push in alive
                and memory.push != partner
                and memory.push not in cleared_targets
                else None
            )
            if hostile is not None and hostile in alive and hostile not in cleared_targets:
                self._add_fake_result(analysis, fake_results, hostile, True)
                push = hostile
            if push is None:
                fake_wolves = [
                    target
                    for night, target, is_wolf in fake_results
                    if is_wolf and target in alive and target != partner
                ]
                if fake_wolves:
                    public = analysis.suspicion_scores(public=True)
                    push = min(
                        fake_wolves,
                        key=lambda target: (
                            -public.get(target, 0),
                            seeded_int(self.seed, game, "fakepush", target),
                        ),
                    )
                else:
                    push = self._wolf_scarecrow(analysis, exclude=cleared_targets)
            text_hostile = hostile if hostile not in cleared_targets else None
            return self._wolf_seer_text(analysis, fake_results, push, text_hostile), {
                "claim": "seer",
                "results": fake_results,
                "push": push,
            }

        if memory.claim == "seer" and exposed:
            push = self._wolf_scarecrow(analysis)
            reason = (
                self._wolf_reason(analysis, push)
                if push is not None
                else "the real seer's results are what matter"
            )
            text = "I'm a villager. I bluffed seer to draw the wolves' kill, and it backfired. %s Vote %s." % (
                reason,
                analysis.names.get(push, "the best lead"),
            )
            return text, {"claim": "villager", "push": push}

        # First-mover seer bluff: on day one, before the real seer has spoken,
        # the lower-seat wolf takes the floor with a true clear.  This is the
        # one fake claim that exploits the table's first-claim advantage
        # instead of fighting it.  The higher-seat wolf stays silent so the
        # pair never produces two claims.
        if (
            memory.claim is None
            and analysis.day == 1
            and not analysis.claimed_seers(alive_only=True)
            and self_pressure < 1.0
            and partner_pressure < 1.0
            and not analysis.dead_seer_revealed()
            and (partner not in alive or seat < partner)
        ):
            fake_results = list(memory.results)
            clear_target = self._choose_true_clear(analysis, fake_results)
            if clear_target is not None:
                self._add_fake_result(analysis, fake_results, clear_target, False)
                clear_name = analysis.names.get(clear_target, "someone")
                text = (
                    "I'm the seer. Night 1: %s is not a wolf. "
                    "I don't have a wolf yet; I'll follow the table." % clear_name
                )
                return text, {"claim": "seer", "results": fake_results, "push": None}

        # A desperate seer bluff is only plausible when nobody has claimed the
        # role yet and the table is already converging on us.
        if (
            self_pressure >= 2.5
            and not analysis.claimed_seers(alive_only=True)
            and not analysis.dead_seer_revealed()
        ):
            fake_results = list(memory.results)
            clear_target = self._choose_true_clear(analysis, fake_results)
            if clear_target is not None:
                self._add_fake_result(analysis, fake_results, clear_target, False)
            push = self._wolf_scarecrow(analysis)
            text = self._wolf_seer_text(analysis, fake_results, push, None)
            if clear_target is not None:
                text += " I cleared %s because I checked them." % analysis.names.get(clear_target, "them")
            return text, {"claim": "seer", "results": fake_results, "push": push}

        push = self._wolf_scarecrow(analysis)
        if hostile is not None and hostile in alive and hostile != partner:
            push = hostile
        if push is None:
            return "I'm a villager. I have no strong read yet.", {"claim": "villager", "push": None}
        reason = self._wolf_reason(analysis, push)
        push_name = analysis.names.get(push, "seat %d" % push)
        hostile_is_seer = (
            isinstance(hostile, int)
            and hostile in alive
            and hostile != partner
            and analysis.is_claimed_seer(hostile)
        )
        if hostile_is_seer and 2.0 <= partner_pressure < 4.0 and partner in alive:
            partner_name = analysis.names.get(partner, "my partner")
            text = (
                "I'm a villager. %s's night result is unverified and easy to fake; "
                "%s is being set up. Vote %s."
                % (push_name, partner_name, push_name)
            )
        elif hostile_is_seer and self_pressure >= 1.0:
            text = (
                "I'm a villager. %s's night result is unverified and easy to fake; "
                "this push is a wolf's move. Vote %s." % (push_name, push_name)
            )
        elif 2.0 <= partner_pressure < 4.0 and partner in alive:
            partner_name = analysis.names.get(partner, "my partner")
            text = "I'm a villager. %s is being set up; the case there is thin. %s Vote %s." % (
                partner_name,
                reason,
                push_name,
            )
        elif self_pressure >= 1.0:
            text = "I'm a villager. The push on me is noise. %s Vote %s." % (reason, push_name)
        else:
            text = "I'm a villager. %s Vote %s." % (reason, push_name)
        return text, {"claim": "villager", "push": push}

    def speak_doctor(self, game, analysis):
        memory = self.memory_for(game)
        known_wolves, known_good = analysis.exact_known()
        if (
            analysis.day == 1
            and memory.claim != "doctor"
            and not known_wolves
            and not known_good
            and not analysis.credible_seer_claims()
            and analysis.weak_pile_on() is None
            and analysis.accusation_pressure(analysis.seat, days=1) < 2.0
        ):
            return (
                "I'm a villager. I don't have a strong read yet; I'll listen to the seer.",
                {"claim": "villager", "push": None},
            )
        push = self._push_village(game, analysis, known_wolves, known_good, memory)
        pressure = analysis.accusation_pressure(analysis.seat, days=1)
        rivals = [
            analysis.accusation_pressure(seat, days=1)
            for seat in analysis.alive_seats
            if seat != analysis.seat
        ]
        top_pressure = max(rivals, default=0.0)
        # A doctor claim is a death sentence at night; only spend it when the
        # table is actually about to exile us.
        imminent = pressure >= 4.0 and pressure >= top_pressure
        if memory.claim == "doctor" or imminent:
            last = analysis.last_protect or {}
            last_seat = last.get("seat") if isinstance(last, dict) else None
            if isinstance(last_seat, int):
                cover = "I protected %s last night." % analysis.names.get(last_seat, "a key villager")
            else:
                cover = "I have been keeping key villagers alive."
            if push is None:
                return "I'm the doctor. %s I'll follow the seer's lead." % cover, {
                    "claim": "doctor",
                    "push": None,
                }
            reason = self._public_reason(analysis, push)
            return "I'm the doctor. %s %s Vote %s." % (
                cover,
                reason,
                analysis.names.get(push, "seat %d" % push),
            ), {"claim": "doctor", "push": push}
        text = self._villager_line(analysis, push, known_wolves, known_good, memory)
        return text, {"claim": "villager", "push": push}

    def speak_villager(self, game, analysis):
        memory = self.memory_for(game)
        known_wolves, known_good = analysis.exact_known()
        # On the first day, with no claim or confirmed result, a random push
        # only helps whichever wolf started the bandwagon.
        if (
            analysis.day == 1
            and not known_wolves
            and not known_good
            and not analysis.credible_seer_claims()
            and analysis.weak_pile_on() is None
            and analysis.accusation_pressure(analysis.seat, days=1) < 2.0
        ):
            return (
                "I'm a villager. I don't have a strong read yet; I'll listen to the seer.",
                {"claim": "villager", "push": None},
            )
        push = self._push_village(game, analysis, known_wolves, known_good, memory)
        text = self._villager_line(analysis, push, known_wolves, known_good, memory)
        return text, {"claim": "villager", "push": push}

    # -- target decisions --------------------------------------------------

    def choose_target(self, game, view, decision, options):
        analysis = Analysis(view)
        memory = self.memory_for(game)
        if decision == "kill":
            return self.kill_wolf(game, analysis, memory, options)
        if decision == "protect":
            return self.protect_doctor(game, analysis, memory, options)
        if decision == "inspect":
            return self.inspect_seer(game, analysis, memory, options)
        if decision == "vote":
            if analysis.role == "werewolf":
                return self.vote_wolf(game, analysis, memory, options)
            return self.vote_village(game, analysis, memory, options)
        return None

    def kill_wolf(self, game, analysis, memory, options):
        partner = analysis.partner_seat
        # A wolf kill needs both wolves to name the same seat.  If the partner
        # has already named someone tonight, matching is the only safe move.
        for vote in analysis.wolf_votes:
            if (
                isinstance(vote, dict)
                and vote.get("night") == analysis.day
                and vote.get("seat") == partner
                and vote.get("target") in options
            ):
                return vote["target"]
        candidates = [seat for seat in options if seat != partner]
        if not candidates:
            return None
        public = analysis.suspicion_scores(public=True)
        credibility = analysis.seer_credibility()

        # The night after a seer first claims, the doctor is likely on them.
        # A player the seer cleared is a safer, still-useful victim.
        clear_bait = set()
        claimed_doctor_present = any(analysis.is_claimed_doctor(seat) for seat in options)
        if not claimed_doctor_present:
            for claimant in analysis.claimed_seers(alive_only=True):
                claim_days = [
                    entry.get("day")
                    for entry in analysis.seer_claims.get(claimant, [])
                    if isinstance(entry.get("day"), int)
                ]
                if not claim_days or min(claim_days) != analysis.day - 1:
                    continue
                for result in analysis.results:
                    if (
                        result.get("speaker") == claimant
                        and not result.get("werewolf")
                        and result.get("target") in options
                    ):
                        clear_bait.add(result["target"])

        def score(seat):
            value = 0.0
            if analysis.is_claimed_seer(seat):
                # The credible claimant is more likely the real seer.
                value += 100.0 + 10.0 * max(0.0, min(credibility.get(seat, 0.0), 3.0))
            if analysis.is_claimed_doctor(seat):
                value += 90.0
            if seat in clear_bait:
                value += 120.0
            for accusation in analysis.accusations:
                if accusation.get("speaker") == seat and accusation.get("target") in (analysis.seat, partner):
                    if isinstance(accusation.get("day"), int) and accusation["day"] >= analysis.day - 1:
                        value += 22.0
            value += 2.0 * len(analysis.speeches_by(seat))
            value += 1.0 * len(analysis.votes_by(seat))
            # A villager the table already suspects is more useful alive as a
            # scapegoat than dead by night.
            value -= 0.7 * min(public.get(seat, 0), 25.0)
            return value

        return min(
            candidates,
            key=lambda seat: (-score(seat), seeded_int(self.seed, game, analysis.day, "kill", seat)),
        )

    def protect_doctor(self, game, analysis, memory, options):
        candidates = [seat for seat in options if seat != analysis.seat]
        if not candidates:
            return None
        last = analysis.last_protect or {}
        last_seat = last.get("seat") if isinstance(last, dict) else None
        candidates = [seat for seat in candidates if seat != last_seat] or candidates

        credibility = analysis.credible_seer_claims()
        known_wolves, known_good = analysis.exact_known()
        seer_wolves = set()
        best_claim_seat = analysis.best_seer_claim()
        if (
            isinstance(best_claim_seat, int)
            and best_claim_seat != analysis.seat
            and best_claim_seat in credibility
        ):
            for result in analysis.results:
                if (
                    result.get("speaker") == best_claim_seat
                    and result.get("werewolf")
                    and result.get("target") in candidates
                ):
                    seer_wolves.add(result["target"])
        safe = [
            seat
            for seat in candidates
            if seat not in known_wolves and seat not in seer_wolves
        ]
        candidates = safe or [seat for seat in candidates if seat not in known_wolves] or candidates

        seers = [seat for seat in candidates if seat in credibility and seat != analysis.seat]
        if seers:
            return min(
                seers,
                key=lambda seat: (
                    -credibility[seat],
                    seeded_int(self.seed, game, analysis.day, "protectseer", seat),
                ),
            )
        public = analysis.suspicion_scores(public=True)

        def score(seat):
            value = -0.6 * public.get(seat, 0)
            value += 0.8 * len(analysis.speeches_by(seat))
            value += 0.4 * len(analysis.votes_by(seat))
            for accusation in analysis.accusations:
                if accusation.get("speaker") == seat:
                    target = accusation.get("target")
                    if target in analysis.dead_roles and analysis.dead_roles.get(target) == "werewolf":
                        value += 4.0
            if seat in analysis.doctor_claims:
                value -= 2.0
            return value

        return min(
            candidates,
            key=lambda seat: (-score(seat), seeded_int(self.seed, game, analysis.day, "protect", seat)),
        )

    def inspect_seer(self, game, analysis, memory, options):
        candidates = [seat for seat in options if seat != analysis.seat]
        if not candidates:
            return None
        known_wolves, known_good = analysis.exact_known()
        inspected = set()
        for result in analysis.seer_results:
            if isinstance(result, dict) and isinstance(result.get("seat"), int):
                inspected.add(result["seat"])
        # Never spend a night on a player we already know, or already checked.
        available = [
            seat
            for seat in candidates
            if seat not in known_good and seat not in known_wolves and seat not in inspected
        ]
        if not available:
            available = [
                seat for seat in candidates if seat not in known_good and seat not in known_wolves
            ]
        if not available:
            available = [seat for seat in candidates if seat not in known_good]
        if not available:
            available = candidates
        public = analysis.suspicion_scores(public=True)
        impostors = [
            seat for seat in analysis.claimed_seers(alive_only=True) if seat != analysis.seat
        ]

        def score(seat):
            value = 0.0
            # A rival seer claim is a direct role conflict.  Inspecting the
            # claimant resolves whether the village is being led by a wolf.
            if seat in impostors:
                value += 60.0

            # Active accusers drive the table.  A wolf usually has to accuse
            # villagers; weak/generic accusations and pushes on cleared players
            # are especially telling.
            made = [
                accusation
                for accusation in analysis.accusations
                if accusation.get("speaker") == seat
            ]
            value += 3.0 * len(made)
            for accusation in made:
                if accusation.get("weak"):
                    value += 3.0
                target = accusation.get("target")
                if target in known_good:
                    value += 10.0
                if target in analysis.dead_roles and analysis.dead_roles.get(target) in (
                    "villager",
                    "doctor",
                    "seer",
                ):
                    value += 6.0

            # Defending a revealed wolf or a wolf we already found is a strong
            # partner tell.
            for clear in analysis.clears:
                if clear.get("speaker") != seat:
                    continue
                target = clear.get("target")
                if target in analysis.dead_roles and analysis.dead_roles.get(target) == "werewolf":
                    value += 35.0
                if target in known_wolves:
                    value += 35.0

            # Votes for revealed villagers or cleared players are suspicious.
            for vote in analysis.votes:
                if vote.get("seat") != seat or vote.get("target") is None:
                    continue
                target = vote["target"]
                if target in analysis.dead_roles and analysis.dead_roles.get(target) in (
                    "villager",
                    "doctor",
                    "seer",
                ):
                    value += 10.0
                if target in known_good:
                    value += 10.0

            # Association with a wolf we already found.
            for wolf in known_wolves:
                for clear in analysis.clears:
                    if clear.get("speaker") == wolf and clear.get("target") == seat:
                        value += 25.0
                    if clear.get("speaker") == seat and clear.get("target") == wolf:
                        value += 25.0
                for accusation in analysis.accusations:
                    if accusation.get("speaker") == wolf and accusation.get("target") == seat:
                        value -= 10.0
                    if accusation.get("speaker") == seat and accusation.get("target") == wolf:
                        value -= 10.0

            value += 0.2 * public.get(seat, 0)
            if not analysis.speeches_by(seat):
                value -= 5.0
            return value

        return min(
            available,
            key=lambda seat: (-score(seat), seeded_int(self.seed, game, analysis.day, "inspect", seat)),
        )

    def vote_wolf(self, game, analysis, memory, options):
        partner = analysis.partner_seat
        candidates = [seat for seat in options if seat != analysis.seat and seat != partner]
        if not candidates:
            return None
        if memory.claim == "seer":
            cleared = {target for night, target, is_wolf in memory.results if not is_wolf}
            filtered = [seat for seat in candidates if seat not in cleared]
            if filtered:
                candidates = filtered
        tally = analysis.tally_today()
        public = analysis.suspicion_scores(public=True)
        if memory.claim == "seer" and memory.push in candidates:
            # A fake seer must vote the player it publicly named, or the story
            # collapses.  The only exception is a vote that can save the partner.
            partner_tally = tally.get(partner, 0)
            leader_tally = max(tally.values(), default=0)
            if partner_tally == leader_tally and partner_tally > 0:
                alternatives = [seat for seat in candidates if seat != memory.push]
                if alternatives:
                    best_alt = max(alternatives, key=lambda seat: tally.get(seat, 0))
                    if tally.get(best_alt, 0) >= partner_tally - 1:
                        return best_alt
            return memory.push

        def score(seat):
            value = 0.7 * public.get(seat, 0)
            value += 1.2 * min(tally.get(seat, 0), 6)
            if analysis.is_claimed_seer(seat):
                value += 6.0
            if analysis.is_claimed_doctor(seat):
                value += 4.0
            if memory.push == seat:
                value += 1.5
            return value

        return min(
            candidates,
            key=lambda seat: (-score(seat), seeded_int(self.seed, game, analysis.day, "votewolf", seat)),
        )

    def vote_village(self, game, analysis, memory, options):
        known_wolves, known_good = analysis.exact_known()
        candidates = [
            seat for seat in options if seat != analysis.seat and seat not in known_good
        ]
        if not candidates:
            candidates = [seat for seat in options if seat != analysis.seat]
        if not candidates:
            return None
        tally = analysis.tally_today()
        public = analysis.suspicion_scores(public=True)
        if known_wolves:
            wolves = [seat for seat in known_wolves if seat in candidates]
            if wolves:
                return min(
                    wolves,
                    key=lambda seat: (
                        -(2.0 * tally.get(seat, 0) + public.get(seat, 0)),
                        seeded_int(self.seed, game, analysis.day, "votewolfexact", seat),
                    ),
                )
        suspicion = analysis.suspicion_scores()
        credibility = analysis.credible_seer_claims()
        best_cred = max(credibility.values(), default=0.0)
        best_claim_seat = analysis.best_seer_claim()
        clear_conflict = analysis.clear_seer_conflict()
        seer_wolves = set()
        if (
            isinstance(best_claim_seat, int)
            and best_claim_seat != analysis.seat
            and best_claim_seat in credibility
        ):
            for result in analysis.results:
                if (
                    result.get("speaker") == best_claim_seat
                    and result.get("werewolf")
                    and result.get("target") in candidates
                ):
                    seer_wolves.add(result["target"])
        if seer_wolves:
            return min(
                seer_wolves,
                key=lambda seat: (
                    -(2.0 * tally.get(seat, 0) + suspicion.get(seat, 0)),
                    seeded_int(self.seed, game, analysis.day, "voteseerwolf", seat),
                ),
            )

        def score(seat):
            value = suspicion.get(seat, 0)
            if seat in credibility and credibility[seat] >= best_cred - 0.15:
                value -= 25.0
            if clear_conflict and seat in credibility:
                value -= 60.0
            if memory.push == seat:
                # Stay on the target the last statement named: a village that
                # sees its accuser switch targets trusts it less.
                value += 3.0
            value += 0.8 * min(tally.get(seat, 0), 3)
            return value

        return min(
            candidates,
            key=lambda seat: (-score(seat), seeded_int(self.seed, game, analysis.day, "votevillage", seat)),
        )

    # -- commit after a successful act ------------------------------------

    def commit(self, pending, action, meta):
        game = pending.get("game")
        view = pending.get("view") or {}
        if not isinstance(game, int):
            return
        memory = self.memory_for(game)
        decision = pending.get("decision")
        if decision == "speak":
            if action.get("pass"):
                return
            if not isinstance(meta, dict):
                return
            claim = meta.get("claim")
            if claim:
                memory.claim = claim
                memory.claim_day = view.get("day") or 0
            for result in meta.get("results") or []:
                if isinstance(result, tuple) and len(result) == 3 and result not in memory.results:
                    memory.results.append(result)
            push = meta.get("push")
            if isinstance(push, int):
                memory.push = push
                memory.push_day = view.get("day") or 0
        elif decision == "vote":
            target = action.get("target")
            if isinstance(target, int):
                memory.last_vote = target
                memory.last_vote_day = view.get("day") or 0


# ---------------------------------------------------------------------------
# main loop


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").strip()
    if not env_url:
        log("MAKEFUN_ENV_URL is not set")
        return 2
    seed = os.environ.get("MAKEFUN_TRIAL_SEED", "").strip() or "missing-seed"

    # The brief is optional for a scripted agent, but read it when the harness
    # provides it: the file also confirms the trial mounted its input.
    prompt_file = os.environ.get("MAKEFUN_PROMPT_FILE", "").strip()
    if prompt_file:
        try:
            with open(prompt_file, "r", encoding="utf-8") as handle:
                handle.read()
        except OSError:
            pass

    client = EnvClient(env_url)
    agent = Agent(seed)
    failures = 0
    idle_observes = 0

    while True:
        status, observed = client.post("/observe", {})
        if status != 200 or not isinstance(observed, dict):
            failures += 1
            if failures > 30:
                log(f"observe kept failing (last status {status})")
                return 1
            time.sleep(min(0.5, 0.02 * failures))
            continue
        failures = 0

        if observed.get("done") is True:
            log(
                "werewolf: done; games=%s finished=%s decisions=%s"
                % (observed.get("games"), observed.get("finished"), getattr(agent, "decisions", 0))
            )
            return 0

        pending = observed.get("pending")
        if not isinstance(pending, list):
            pending = []
        if not pending:
            idle_observes += 1
            # The real env long-polls, so this is rare; avoid a busy spin anyway.
            if idle_observes > 50:
                time.sleep(0.1)
            continue
        idle_observes = 0

        for item in pending:
            if not isinstance(item, dict):
                continue
            game = item.get("game")
            if not isinstance(game, int):
                continue
            try:
                action, meta = agent.act_for(item)
            except Exception as error:  # never let one bad view stall every game
                log(f"decision error game {game}: {error!r}")
                action, meta = agent.fallback(item), None

            status, body = client.post("/act", action)
            if status == 200:
                agent.commit(item, action, meta)
                agent.decisions = getattr(agent, "decisions", 0) + 1
                continue
            if status == 409:
                # The decision moved on between observe and act: nothing to do.
                continue

            # A refusal changes nothing, so answer with the always-legal fallback.
            fallback = agent.fallback(item)
            status2, body2 = client.post("/act", fallback)
            if status2 == 200:
                agent.commit(item, fallback, None)
                agent.decisions = getattr(agent, "decisions", 0) + 1
            elif status2 != 409:
                log(f"act refused game {game}: {status} {body!r}; fallback: {status2} {body2!r}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
