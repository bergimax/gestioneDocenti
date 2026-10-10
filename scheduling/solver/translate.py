"""Unsat cores to Italian sentences (architecture 7.3, specs 9.2).

A core is a set of constraint keys (see ``model._Probe``). The sentences name
the class, the day, the teachers and the hours that are missing; the keys and
literal names never reach the text.
"""

from __future__ import annotations

from collections import defaultdict

from .context import (
    FIRST_HOUR,
    HOURS,
    OVERRIDE_AVAILABILITY,
    ScheduleContext,
    capacity,
)

DAY_WORDS = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì"]
DAY_KEYS = ["LUNEDI", "MARTEDI", "MERCOLEDI", "GIOVEDI", "VENERDI"]
OVERRIDE_LABEL = "Ignora la disponibilità"
GENERIC_CAUSE = "I vincoli di questa settimana non permettono un orario completo."


def _ore(n: int) -> str:
    return f"{n} ora" if n == 1 else f"{n} ore"


def _residue(n: int) -> str:
    return "1 ora residua" if n == 1 else f"{n} ore residue"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " e " + items[-1]


def _days_text(days: list[int]) -> str:
    return _join([DAY_WORDS[d] for d in days])


def translate(ctx: ScheduleContext, cores: list[set]) -> tuple[list[str], list[dict]]:
    """One message per core (no duplicates) and the derogations that unblock it."""
    translator = _Translator(ctx)
    messages, deroghe = [], []
    merged = {}  # cause signature -> core
    for core in sorted(cores, key=lambda c: sorted(map(repr, c))):
        signature = translator.signature(core)
        if signature is not None and signature in merged:
            merged[signature] |= core  # the same cause seen from another class
        else:
            merged[signature if signature is not None else id(core)] = set(core)
    for core in merged.values():
        message, suggestions = translator.core(core)
        if message not in messages:
            messages.append(message)
        for suggestion in suggestions:
            if suggestion not in deroghe:
                deroghe.append(suggestion)
    return messages, deroghe


class _Translator:
    def __init__(self, ctx: ScheduleContext):
        self.ctx = ctx
        self.by_id = {a.id: a for a in ctx.assignments}
        self.by_class = defaultdict(list)
        for a in ctx.assignments:
            self.by_class[a.class_id].append(a)
        by_key = {(a.teacher_id, a.class_id, a.subject_id): a for a in ctx.assignments}
        self.partner = {}  # assignment id -> assignment id of the paired class
        for p in ctx.pairings:
            a = by_key.get((p.teacher_id, p.class_a, p.subject_id))
            b = by_key.get((p.teacher_id, p.class_b, p.subject_id))
            if a is not None and b is not None:
                self.partner[a.id], self.partner[b.id] = b.id, a.id

    def klass(self, class_id) -> str:
        return self.ctx.class_names.get(class_id, str(class_id))

    def teacher(self, teacher_id) -> str:
        return self.ctx.teacher_names.get(teacher_id, str(teacher_id))

    def free_hours(self, teacher_id, day) -> int:
        free = (self.ctx.availability or {}).get(teacher_id, frozenset())
        return len({h for d, h in free if d == day})

    def signature(self, core: set):
        """What limits the teachers in ``core``, or None if it is not a plain cap.

        Two cores with the same signature are one cause: a paired lesson blocked
        for both of its classes, a contractor who is away for every class.
        """
        if any(key[0] in ("busy", "closed", "pair") for key in core):
            return None
        causes = set()
        for key in core:
            if key[0] == "avail":
                a = self.by_id[key[1]]
                causes.add(("avail", a.teacher_id, key[2]))
            elif key[0] == "hours":
                partner = self.partner.get(key[1])
                causes.add(("hours", frozenset({key[1], partner})))
            elif key[0] != "cap":
                causes.add(key)
        return frozenset(causes) or None

    def core(self, core: set) -> tuple[str, list[dict]]:
        of = defaultdict(set)
        for key in core:
            of[key[0]].add(key[1:])
        caps = sorted(of["cap"], key=lambda k: (self.klass(k[0]), k[1]))
        deroghe = self.deroghe(sorted(of["avail"], key=repr), caps)
        if of["busy"]:
            message = " ".join(
                self.overlap(t, d, caps, of) for t, d in sorted(of["busy"], key=repr)
            )
        elif len(core) == 1 and caps and self.does_not_fit(*caps[0]):
            message = self.block_message(*caps[0])
        else:
            message = self.shortfall(caps, of) or self.generic(caps)
        return message, deroghe

    def generic(self, caps) -> str:
        classes = _join(sorted({self.klass(c) for c, _ in caps}))
        return f"{GENERIC_CAUSE[:-1]} (classi: {classes})."

    def does_not_fit(self, class_id, day) -> bool:
        return self.min_hour(class_id) + capacity(self.ctx, class_id, day) > HOURS

    def min_hour(self, class_id) -> int:
        start = self.ctx.class_min_start.get(class_id, FIRST_HOUR)
        return max(0, min(HOURS - 1, start - FIRST_HOUR))

    def block_message(self, class_id, day) -> str:
        cap = capacity(self.ctx, class_id, day)
        return (
            f"La classe {self.klass(class_id)} {DAY_WORDS[day]} richiede "
            f"{_ore(cap)} consecutive ma la prima lezione non può iniziare prima "
            f"delle {FIRST_HOUR + self.min_hour(class_id):02d}:00: "
            f"non c'è spazio nella giornata."
        )

    def overlap(self, teacher_id, day, caps, of) -> str:
        """H1: one teacher needed by several classes in the same hours."""
        classes = [
            c
            for c, d in caps
            if d == day and any(a.teacher_id == teacher_id for a in self.by_class[c])
        ]
        name = self.teacher(teacher_id)
        needed = sum(capacity(self.ctx, c, day) for c in classes)
        available = HOURS
        if any(
            d == day and self.by_id[aid].teacher_id == teacher_id
            for aid, d in of["avail"]
        ):
            available = self.free_hours(teacher_id, day)
        names = _join([self.klass(c) for c in classes])
        text = (
            f"Il docente {name} {DAY_WORDS[day]} deve essere nelle classi {names} "
            f"nelle stesse ore"
        )
        if needed > available:
            text += (
                f": servono {_ore(needed)} ma ne può fare al massimo {available}. "
                f"Mancano {_ore(needed - available)}."
            )
        else:
            text += "."
        return text

    def shortfall(self, caps, of) -> str:
        """Hours a class-day needs against what its teachers can cover."""
        classes = sorted({c for c, _ in caps}, key=self.klass)
        days = {c: sorted(d for cc, d in caps if cc == c) for c in classes}
        demand = {c: sum(capacity(self.ctx, c, d) for d in days[c]) for c in classes}
        reasons = []
        rows = []  # [teacher_id, class_id, hours the teacher covers]
        for c in classes:
            for t in dict.fromkeys(a.teacher_id for a in self.by_class[c]):
                amount = self.coverage(c, t, days[c], of, reasons)
                rows.append([t, c, amount])
        for (t,) in sorted(of["weekly"], key=repr):
            cap = self.ctx.weekly_caps[t]
            excess = sum(r[2] for r in rows if r[0] == t) - cap
            for row in reversed([r for r in rows if r[0] == t]):
                cut = min(excess, row[2])
                row[2] -= max(0, cut)
                excess -= max(0, cut)
            reasons.append(f"{self.teacher(t)}: massimo {_ore(cap)} a settimana")
        covered = {
            c: min(demand[c], sum(r[2] for r in rows if r[1] == c)) for c in classes
        }
        missing = sum(demand[c] - covered[c] for c in classes)
        if missing <= 0:
            return ""
        parts = [
            f"{self.klass(c)} ({_days_text(days[c])})"
            if len(days[c]) > 1 or len(classes) > 1
            else f"{self.klass(c)} {_days_text(days[c])}"
            for c in classes
        ]
        total, got = sum(demand.values()), sum(covered.values())
        if len(classes) > 1:
            head = f"Le classi {_join(parts)} richiedono in tutto {_ore(total)}"
        elif len(days[classes[0]]) > 1:
            head = f"La classe {parts[0]} richiede in tutto {_ore(total)}"
        else:
            head = f"La classe {parts[0]} richiede {_ore(total)}"
        if got == 0:
            middle = ", ma nessun docente può coprirle"
        else:
            middle = f", ma i docenti disponibili ne coprono solo {got}"
        if not reasons:
            reasons = ["nessun docente assegnato alla classe"]
        return f"{head}{middle} ({'; '.join(reasons)}). Mancano {_ore(missing)}."

    def coverage(self, class_id, teacher_id, days, of, reasons) -> int:
        """Hours ``teacher_id`` can give ``class_id`` on ``days``; adds the reasons."""
        mine = [a for a in self.by_class[class_id] if a.teacher_id == teacher_id]
        name = self.teacher(teacher_id)
        pairs = [
            self.klass(self.by_id[self.partner[a.id]].class_id)
            for a in mine
            if a.id in self.partner
        ]
        who = name
        if pairs:
            who += f" (lezione in coppia con la classe {_join(pairs)})"
        unavailable_days = {
            d for aid, d in of["avail"] if self.by_id[aid].teacher_id == teacher_id
        }
        total = 0
        for d in days:
            limit = capacity(self.ctx, class_id, d)
            closed = self.closed_partner(mine, d, of)
            if closed is not None:
                limit = 0
                state = (
                    "in stage" if d in self.ctx.stage_days.get(closed, ()) else "chiusa"
                )
                reasons.append(
                    f"{name}: la sua lezione in coppia con la classe "
                    f"{self.klass(closed)} non si può svolgere {DAY_WORDS[d]} "
                    f"perché {self.klass(closed)} è {state}"
                )
            elif (teacher_id, d) in of["excl"]:
                limit = 0
                reasons.append(f"{who}: non insegna {DAY_WORDS[d]}")
            elif d in unavailable_days:
                free = self.free_hours(teacher_id, d)
                limit = min(limit, free)
                if free == 0:
                    reasons.append(f"{who}: non disponibile {DAY_WORDS[d]}")
                else:
                    reasons.append(
                        f"{who}: disponibile solo {_ore(free)} {DAY_WORDS[d]}"
                    )
            total += limit
        limited = {i for (i,) in of["hours"]}
        if mine and all(
            a.id in limited or self.partner.get(a.id) in limited for a in mine
        ):
            residual = sum(self.residual(a) for a in mine)
            total = min(total, residual)
            reasons.append(f"{who}: ha solo {_residue(residual)}")
        return total

    def residual(self, assignment) -> int:
        """Residual hours of an assignment; a pair is limited by the lower one."""
        value = max(0, assignment.residual_hours)
        partner = self.partner.get(assignment.id)
        if partner is not None:
            value = min(value, max(0, self.by_id[partner].residual_hours))
        return value

    def closed_partner(self, mine, day, of):
        """The closed class whose pairing blocks all of ``mine`` on ``day``."""
        blocked = None
        for a in mine:
            partner = self.partner.get(a.id)
            other = None if partner is None else self.by_id[partner].class_id
            if other is None or (other, day) not in of["closed"]:
                return None
            blocked = other
        return blocked

    def deroghe(self, avail, caps) -> list[dict]:
        """`override_availability` for each contractor day that is part of a core."""
        needed = {c for c, _ in caps}
        suggestions = []
        for aid, day in avail:
            a = self.by_id[aid]
            class_id = next(
                (
                    c
                    for c in sorted(needed, key=self.klass)
                    if any(x.teacher_id == a.teacher_id for x in self.by_class[c])
                ),
                a.class_id,
            )
            suggestions.append(
                {
                    "action_type": OVERRIDE_AVAILABILITY,
                    "label": OVERRIDE_LABEL,
                    "classe": class_id,
                    "docente": a.teacher_id,
                    "giorno": DAY_KEYS[day],
                }
            )
        return suggestions
