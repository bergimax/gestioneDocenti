"""Import of the old FastAPI system's PostgreSQL database (issue #45).

`run()` reads the source read-only and writes the equivalent records in one
transaction. Old ids are strings; `Importer.maps` keeps old id -> new object per
source table, in memory only. Row-level problems are skipped and reported with
the old id and the reason; any unexpected exception aborts and rolls back.
"""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import unquote, urlsplit

import psycopg
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone
from psycopg import sql
from psycopg.rows import dict_row

from accounts.models import User
from availability.models import Availability
from ledger.models import EntryKind, LedgerEntry
from scheduling.feedback import MAX_NOTE_LENGTH
from scheduling.models import (
    AuditLog,
    Slot,
    SoftWeightAdjustment,
    Timetable,
    TimetableFeedback,
)
from school.models import (
    Assignment,
    CalendarEntry,
    Class,
    ClassPairing,
    School,
    Subject,
    Teacher,
)

OPENING_REASON = "Ore erogate importate dal vecchio sistema"

# Imported tables, in report order.
IMPORTED = (
    "scuola",
    "admin",
    "classe",
    "docente",
    "materia",
    "monte_ore_annuale",
    "assegnazione",
    "calendario_annuale",
    "disponibilita_settimanale",
    "classe_accoppiata",
    "orario_settimanale",
    "slot_lezione",
    "audit_log",
    "orario_feedback",
    "soft_weight_adjustment",
)
NOT_IMPORTED = {
    "admin_session": "not imported by design",
    "chat_message": "not imported by design",
    "preferenze_ai_memory": "not imported by design",
    "conflitto_gestito": "not imported by design",
}
# Target tables that must be empty (users are excepted).
TARGET_MODELS = (
    ("scuola", School),
    ("classe", Class),
    ("docente", Teacher),
    ("materia", Subject),
    ("assegnazione", Assignment),
    ("calendario_annuale", CalendarEntry),
    ("disponibilita_settimanale", Availability),
    ("classe_accoppiata", ClassPairing),
    ("orario_settimanale", Timetable),
    ("slot_lezione", Slot),
    ("audit_log", AuditLog),
    ("orario_feedback", TimetableFeedback),
    ("soft_weight_adjustment", SoftWeightAdjustment),
    ("ledger", LedgerEntry),
)


class ImportRefused(Exception):
    """The run was refused or aborted; the message is safe to print."""


class _Rollback(Exception):
    pass


def scrub(text, url):
    """Remove the URL and its password from a message."""
    text = str(text)
    parts = urlsplit(url)
    secrets = [url, parts.password and unquote(parts.password), parts.password]
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


@dataclass
class TableReport:
    name: str
    source: int = 0
    created: int = 0
    skipped: list = field(default_factory=list)

    def skip(self, old_id, reason):
        self.skipped.append((old_id, reason))


def check_target_empty():
    for name, model in TARGET_MODELS:
        if model.objects.exists():
            raise ImportRefused(
                f"The target is not empty: table {model._meta.db_table} "
                f"({name}) already has rows. Nothing was written."
            )


def _ref(row, column):
    value = row.get(column)
    return None if value is None else str(value)


def _aware(value):
    if isinstance(value, datetime.datetime) and timezone.is_naive(value):
        return timezone.make_aware(value, datetime.UTC)
    return value


def _reason(exc):
    if hasattr(exc, "error_dict"):
        return "; ".join(
            f"{field}: {' '.join(messages)}"
            for field, messages in exc.message_dict.items()
        )
    return " ".join(exc.messages)


def _integral(value):
    """The value as a non-negative int, or None if it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float | Decimal):
        if value != int(value):
            return None
        value = int(value)
    if isinstance(value, int) and value >= 0:
        return value
    return None


class Source:
    """Read-only access to the old database."""

    def __init__(self, conn):
        self.conn = conn
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            )
            self.tables = {r["table_name"] for r in cur.fetchall()}

    def rows(self, table, *, required=True):
        if table not in self.tables:
            if required:
                raise ImportRefused(f"The source has no table {table}.")
            return []
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))
            rows = cur.fetchall()
        return sorted(rows, key=lambda r: str(r.get("id", "")))


class Importer:
    def __init__(self, source):
        self.src = source
        self.maps = {t: {} for t in IMPORTED}
        self.reports = {t: TableReport(t) for t in IMPORTED}
        self.notes = []
        self.mismatches = []
        self.stamps = {}  # model -> [(pk, {column: value})]
        self.expected_remaining = {}  # assignment pk -> old ore_totali - erogate
        self.ledger_created = 0
        self.monte = {}  # (docente, classe, materia) old ids -> usable row

    # -- helpers -----------------------------------------------------------
    def load(self, table):
        rows = self.src.rows(table)
        self.reports[table].source = len(rows)
        return rows

    def note(self, text):
        self.notes.append(text)

    def save(self, table, old_id, obj):
        """full_clean + save; a failing row is skipped with its reason."""
        rep = self.reports[table]
        try:
            obj.full_clean()
        except ValidationError as exc:
            rep.skip(old_id, _reason(exc))
            return False
        obj.save()
        self.maps[table][old_id] = obj
        rep.created += 1
        return True

    def lookup(self, table, row, column, parent_table):
        """(object, None) for a mapped parent, (None, reason) otherwise."""
        old = _ref(row, column)
        if old is None:
            return None, f"{column} is null"
        obj = self.maps[parent_table].get(old)
        if obj is None:
            return None, f"orphan {column} {old} (no such imported {parent_table})"
        return obj, None

    def stamp(self, model, obj, **columns):
        columns = {k: _aware(v) for k, v in columns.items() if v is not None}
        if columns:
            self.stamps.setdefault(model, []).append((obj.pk, columns))

    def apply_stamps(self):
        """Original timestamps by raw UPDATE: the models refuse update()."""
        with connection.cursor() as cur:
            for model, items in self.stamps.items():
                table = connection.ops.quote_name(model._meta.db_table)
                for pk, columns in items:
                    sets = ", ".join(
                        f"{connection.ops.quote_name(c)} = %s" for c in columns
                    )
                    cur.execute(
                        f"UPDATE {table} SET {sets} WHERE id = %s",  # noqa: S608
                        [*columns.values(), pk],
                    )

    # -- run ---------------------------------------------------------------
    def run_all(self):
        self.schools()
        self.users()
        self.classes()
        self.teachers()
        self.subjects()
        self.prepare_hours()
        self.assignments()
        self.apply_hours()
        self.calendar()
        self.availability()
        self.pairings()
        self.timetables()
        self.slots()
        self.audit()
        self.feedback()
        self.weights()
        self.apply_stamps()

    def schools(self):
        for row in self.load("scuola"):
            self.save(
                "scuola",
                str(row["id"]),
                School(
                    name=row.get("nome"),
                    formative_year=row.get("anno_formativo"),
                    year_start=row.get("data_inizio_anno"),
                    year_end=row.get("data_fine_anno"),
                ),
            )

    def users(self):
        rep = self.reports["admin"]
        seen = set()
        unknown_role = 0
        valid_roles = {value for value, _ in User._meta.get_field("role").choices}
        for row in self.load("admin"):
            old = str(row["id"])
            email = (row.get("email") or "").strip().lower()
            if not email:
                rep.skip(old, "empty email")
                continue
            if email in seen:
                rep.skip(old, f"duplicate email {email} in the source")
                continue
            if User.objects.filter(email__iexact=email).exists():
                rep.skip(old, f"email {email} already exists in the target")
                continue
            role = row.get("role")
            if role not in valid_roles:
                role = "ADMIN"  # the old system treated every other value so
                unknown_role += 1
            user = User(email=email, role=role)
            user.set_unusable_password()
            seen.add(email)
            if not self.save("admin", old, user):
                seen.discard(email)
        if unknown_role:
            self.note(f"admin: {unknown_role} missing or unknown role mapped to ADMIN")

    def classes(self):
        for row in self.load("classe"):
            school, why = self.lookup("scuola", row, "scuola_id", "scuola")
            old = str(row["id"])
            if why:
                self.reports["classe"].skip(old, why)
                continue
            n = row.get("n_studenti")
            self.save(
                "classe",
                old,
                Class(
                    school=school,
                    name=row.get("nome"),
                    student_count=0 if n is None else n,
                    group=row.get("gruppo") or "",
                ),
            )

    def teachers(self):
        for row in self.load("docente"):
            school, why = self.lookup("scuola", row, "scuola_id", "scuola")
            old = str(row["id"])
            if why:
                self.reports["docente"].skip(old, why)
                continue
            active, first = row.get("active"), row.get("first_time_this_year")
            self.save(
                "docente",
                old,
                Teacher(
                    school=school,
                    name=row.get("nome"),
                    email=row.get("email") or "",
                    type=row.get("tipo"),
                    active=True if active is None else active,
                    first_time=True if first is None else first,
                ),
            )

    def subjects(self):
        for row in self.load("materia"):
            school, why = self.lookup("scuola", row, "scuola_id", "scuola")
            old = str(row["id"])
            if why:
                self.reports["materia"].skip(old, why)
                continue
            self.save(
                "materia",
                old,
                Subject(
                    school=school,
                    name=row.get("nome"),
                    kind=row.get("tipo"),
                    cognitive_weight=row.get("peso_cognitivo"),
                ),
            )

    # -- hours -------------------------------------------------------------
    @staticmethod
    def _triple(row):
        triple = (
            _ref(row, "docente_id"),
            _ref(row, "classe_id"),
            _ref(row, "materia_id"),
        )
        return None if None in triple else triple

    def prepare_hours(self):
        """Pick the usable monte_ore_annuale row of each triple."""
        rep = self.reports["monte_ore_annuale"]
        rows = self.load("monte_ore_annuale")
        rows.sort(key=lambda r: (str(_aware(r.get("created_at")) or ""), str(r["id"])))
        self.monte_rows = rows
        self.monte_skipped = set()
        for row in rows:
            old = str(row["id"])
            triple = self._triple(row)
            if triple is None:
                rep.skip(old, "docente_id, classe_id or materia_id is null")
            elif _integral(row.get("ore_totali")) is None:
                rep.skip(old, f"invalid ore_totali {row.get('ore_totali')!r}")
            elif _integral(row.get("ore_erogate")) is None:
                rep.skip(old, f"invalid ore_erogate {row.get('ore_erogate')!r}")
            elif triple in self.monte:
                rep.skip(old, "second row for the same teacher+class+subject")
            else:
                self.monte[triple] = row
                continue
            self.monte_skipped.add(old)

    def assignments(self):
        rep = self.reports["assegnazione"]
        no_hours = []
        self.assignment_triples = {}
        for row in self.load("assegnazione"):
            old = str(row["id"])
            parents = {}
            for col, table in (
                ("scuola_id", "scuola"),
                ("docente_id", "docente"),
                ("classe_id", "classe"),
                ("materia_id", "materia"),
            ):
                parents[col], why = self.lookup(table, row, col, table)
                if why:
                    rep.skip(old, why)
                    break
            else:
                triple = self._triple(row)
                monte = self.monte.get(triple)
                hours = 0 if monte is None else _integral(monte["ore_totali"])
                obj = Assignment(
                    school=parents["scuola_id"],
                    teacher=parents["docente_id"],
                    school_class=parents["classe_id"],
                    subject=parents["materia_id"],
                    ore_totali=hours,
                )
                if self.save("assegnazione", old, obj):
                    if monte is None:
                        no_hours.append(old)
                    self.assignment_triples.setdefault(triple, obj)
        if no_hours:
            self.note(
                f"assegnazione: {len(no_hours)} without monte_ore_annuale row, "
                f"imported with 0 total hours (old ids: {', '.join(no_hours)})"
            )

    def apply_hours(self):
        rep = self.reports["monte_ore_annuale"]
        for row in self.monte_rows:
            old = str(row["id"])
            if old in self.monte_skipped:
                continue
            triple = self._triple(row)
            assignment = self.assignment_triples.get(triple)
            if assignment is None:
                rep.skip(old, "no matching imported assignment")
                continue
            rep.created += 1
            total, done = _integral(row["ore_totali"]), _integral(row["ore_erogate"])
            self.expected_remaining[assignment.pk] = total - done
            if done > 0:
                entry = LedgerEntry(
                    assignment=assignment,
                    hours=done,
                    kind=EntryKind.ADJUSTMENT,
                    reason=OPENING_REASON,
                )
                entry.save()
                self.ledger_created += 1
                self.stamp(LedgerEntry, entry, created_at=row.get("created_at"))
        for pk in (a.pk for a in self.assignment_triples.values()):
            self.expected_remaining.setdefault(pk, 0)

    # -- calendar and availability ------------------------------------------
    def calendar(self):
        rep = self.reports["calendario_annuale"]
        for row in self.load("calendario_annuale"):
            old = str(row["id"])
            school, why = self.lookup("scuola", row, "scuola_id", "scuola")
            stage = None
            if not why and _ref(row, "flag_stage_classe_id") is not None:
                stage, why = self.lookup(
                    "classe", row, "flag_stage_classe_id", "classe"
                )
            if why:
                rep.skip(old, why)
                continue
            max_hours, closure = row.get("ore_max_giornata"), row.get("flag_chiusura")
            group_stage = row.get("flag_stage_gruppo")
            self.save(
                "calendario_annuale",
                old,
                CalendarEntry(
                    school=school,
                    date=row.get("data"),
                    group=row.get("gruppo"),
                    max_hours=6 if max_hours is None else max_hours,
                    is_closure=bool(closure),
                    start_hour_min=row.get("ora_inizio_min"),
                    stage_class=stage,
                    is_group_stage=bool(group_stage),
                ),
            )

    def availability(self):
        rep = self.reports["disponibilita_settimanale"]
        for row in self.load("disponibilita_settimanale"):
            old = str(row["id"])
            school, why = self.lookup("scuola", row, "scuola_id", "scuola")
            teacher = None
            if not why:
                teacher, why = self.lookup("docente", row, "docente_id", "docente")
            if why:
                rep.skip(old, why)
                continue
            obj = Availability(
                school=school,
                teacher=teacher,
                week_start=row.get("settimana_inizio"),
                slots=row.get("giorni_fasce"),
            )
            if self.save("disponibilita_settimanale", old, obj):
                self.stamp(
                    Availability,
                    obj,
                    created_at=row.get("created_at"),
                    updated_at=row.get("updated_at") or row.get("created_at"),
                )

    def pairings(self):
        rep = self.reports["classe_accoppiata"]
        for row in self.load("classe_accoppiata"):
            old = str(row["id"])
            found = {}
            for col, table in (
                ("scuola_id", "scuola"),
                ("classe_a_id", "classe"),
                ("classe_b_id", "classe"),
                ("materia_id", "materia"),
            ):
                found[col], why = self.lookup(table, row, col, table)
                if why:
                    rep.skip(old, why)
                    break
            else:
                teacher = None
                if _ref(row, "docente_id") is not None:
                    teacher, why = self.lookup("docente", row, "docente_id", "docente")
                    if why:
                        rep.skip(old, why)
                        continue
                a, b = found["classe_a_id"], found["classe_b_id"]
                if a.pk == b.pk:
                    rep.skip(old, "a class paired with itself")
                    continue
                if a.pk > b.pk:
                    a, b = b, a
                self.save(
                    "classe_accoppiata",
                    old,
                    ClassPairing(
                        school=found["scuola_id"],
                        class_a=a,
                        class_b=b,
                        subject=found["materia_id"],
                        teacher=teacher,
                        note=row.get("note") or "",
                    ),
                )

    # -- timetables -----------------------------------------------------------
    def timetables(self):
        rep = self.reports["orario_settimanale"]
        for row in self.load("orario_settimanale"):
            old = str(row["id"])
            school, why = self.lookup("scuola", row, "scuola_id", "scuola")
            if why:
                rep.skip(old, why)
                continue
            soft = row.get("n_conflitti_soft")
            self.save(
                "orario_settimanale",
                old,
                Timetable(
                    school=school,
                    week_start=row.get("settimana_inizio"),
                    state=row.get("stato") or "BOZZA",
                    quality_score=row.get("quality_score"),
                    quality_level=row.get("quality_level"),
                    soft_conflicts=0 if soft is None else soft,
                    approved_at=_aware(row.get("approved_at")),
                    version=0,
                    conflicts=[],
                ),
            )

    def slots(self):
        rep = self.reports["slot_lezione"]
        for row in self.load("slot_lezione"):
            old = str(row["id"])
            found = {}
            for col, table in (
                ("orario_settimanale_id", "orario_settimanale"),
                ("classe_id", "classe"),
                ("docente_id", "docente"),
                ("materia_id", "materia"),
            ):
                found[col], why = self.lookup(table, row, col, table)
                if why:
                    rep.skip(old, why)
                    break
            else:
                paired = None
                if _ref(row, "classe_accoppiata_id") is not None:
                    paired, why = self.lookup(
                        "classe", row, "classe_accoppiata_id", "classe"
                    )
                    if why:
                        rep.skip(old, why)
                        continue
                self.save(
                    "slot_lezione",
                    old,
                    Slot(
                        timetable=found["orario_settimanale_id"],
                        school_class=found["classe_id"],
                        teacher=found["docente_id"],
                        subject=found["materia_id"],
                        day=row.get("giorno"),
                        start_hour=row.get("ora_inizio"),
                        end_hour=row.get("ora_fine"),
                        paired=bool(row.get("accoppiata")),
                        paired_class=paired,
                    ),
                )

    # -- append-only logs ---------------------------------------------------------
    def _user(self, row):
        """(user, unknown): the mapped user, a null user if the id is unknown."""
        old = _ref(row, "admin_id")
        if old is None:
            return None, False
        user = self.maps["admin"].get(old)
        return user, user is None

    @staticmethod
    def _sort_key(row, column):
        value = _aware(row.get(column))
        return (value is None, value.timestamp() if value else 0, str(row["id"]))

    def audit(self):
        rep = self.reports["audit_log"]
        unknown_admin = 0
        for row in sorted(
            self.load("audit_log"), key=lambda r: self._sort_key(r, "timestamp")
        ):
            old = str(row["id"])
            school, why = self.lookup("scuola", row, "scuola_id", "scuola")
            timetable = None
            if not why and _ref(row, "orario_settimanale_id") is not None:
                timetable, why = self.lookup(
                    "orario_settimanale",
                    row,
                    "orario_settimanale_id",
                    "orario_settimanale",
                )
            if why:
                rep.skip(old, why)
                continue
            user, unknown = self._user(row)
            details = row.get("dettagli")
            obj = AuditLog(
                school=school,
                timetable=timetable,
                week_start=timetable.week_start if timetable else None,
                action=row.get("azione"),
                user=user,
                details={} if details is None else details,
            )
            if self.save("audit_log", old, obj):
                unknown_admin += unknown
                self.stamp(AuditLog, obj, created_at=row.get("timestamp"))
        if unknown_admin:
            self.note(
                f"audit_log: {unknown_admin} unknown admin_id imported with null user"
            )

    def feedback(self):
        """orario_feedback -> TimetableFeedback: notes only, voto/motivi dropped."""
        rep = self.reports["orario_feedback"]
        rows = self.load("orario_feedback")
        voto = sum(1 for r in rows if r.get("voto") is not None)
        motivi = sum(1 for r in rows if r.get("motivi"))
        column = None
        if rows:
            column = next(
                (c for c in ("created_at", "timestamp") if c in rows[0]), None
            )
        unknown_admin = 0
        no_time = 0
        for row in sorted(rows, key=lambda r: self._sort_key(r, column)):
            old = str(row["id"])
            timetable, why = self.lookup(
                "orario_settimanale", row, "orario_settimanale_id", "orario_settimanale"
            )
            if why:
                rep.skip(old, why)
                continue
            note = (row.get("nota") or "").strip()
            if not note:
                rep.skip(old, "empty note")
                continue
            if len(note) > MAX_NOTE_LENGTH:
                rep.skip(old, f"note longer than {MAX_NOTE_LENGTH} characters")
                continue
            state = row.get("stato")
            if state not in ("BOZZA", "APPROVATO"):
                rep.skip(old, f"stato must be BOZZA or APPROVATO, not {state!r}")
                continue
            level = row.get("quality_level")
            if level is not None and level not in ("A", "B", "C"):
                rep.skip(old, f"quality_level must be A, B or C, not {level!r}")
                continue
            score = row.get("quality_score")
            if score is not None:
                if (
                    isinstance(score, bool)
                    or not isinstance(score, int | float | Decimal)
                    or score != score  # NaN
                ):
                    rep.skip(old, f"quality_score is not a number ({score!r})")
                    continue
                score = float(score)
            soft = row.get("n_conflitti_soft")
            user, unknown = self._user(row)
            stamp = row.get(column) if column else None
            obj = TimetableFeedback(
                school=timetable.school,
                timetable=timetable,
                week_start=timetable.week_start,
                user=user,
                note=note,
                quality_score=score,
                quality_level=level,
                soft_conflicts=0 if soft is None else soft,
                timetable_state=state,
            )
            if self.save("orario_feedback", old, obj):
                unknown_admin += unknown
                if stamp is None:
                    no_time += 1
                else:
                    self.stamp(TimetableFeedback, obj, created_at=stamp)
        if voto or motivi:
            self.note(
                f"orario_feedback: {voto} rows carried a voto, {motivi} rows "
                "carried motivi: not imported (no equivalent in the new model)"
            )
        if unknown_admin:
            self.note(
                f"orario_feedback: {unknown_admin} unknown admin_id imported "
                "with null user"
            )
        if no_time:
            self.note(
                f"orario_feedback: {no_time} rows without original timestamp "
                "got the import time"
            )

    def weights(self):
        rep = self.reports["soft_weight_adjustment"]
        schools = list(self.maps["scuola"].values())
        unknown_admin = 0
        for row in sorted(
            self.load("soft_weight_adjustment"),
            key=lambda r: self._sort_key(r, "created_at"),
        ):
            old = str(row["id"])
            if "scuola_id" in row and row["scuola_id"] is not None:
                school, why = self.lookup("scuola", row, "scuola_id", "scuola")
            elif len(schools) == 1:
                school, why = schools[0], None
            else:
                school, why = (
                    None,
                    "cannot tell the school (no scuola_id, not exactly one school)",
                )
            if why:
                rep.skip(old, why)
                continue
            old_w, new_w = (
                _integral(row.get("old_weight")),
                _integral(row.get("new_weight")),
            )
            if old_w is None or new_w is None:
                rep.skip(
                    old,
                    f"weights must be non-negative integers "
                    f"({row.get('old_weight')!r} -> {row.get('new_weight')!r})",
                )
                continue
            user, unknown = self._user(row)
            evidence = row.get("evidenza")
            obj = SoftWeightAdjustment(
                school=school,
                kind=row.get("kind"),
                old_weight=old_w,
                new_weight=new_w,
                evidence={} if evidence is None else evidence,
                user=user,
            )
            if self.save("soft_weight_adjustment", old, obj):
                unknown_admin += unknown
                self.stamp(SoftWeightAdjustment, obj, created_at=row.get("created_at"))
        if unknown_admin:
            self.note(
                f"soft_weight_adjustment: {unknown_admin} unknown admin_id "
                "imported with null user"
            )

    # -- verification ----------------------------------------------------------
    def verify(self):
        """Re-query the new database; return a list of mismatch descriptions."""
        problems = []
        counted = {
            "scuola": School.objects.count(),
            "admin": User.objects.filter(
                pk__in=[u.pk for u in self.maps["admin"].values()]
            ).count(),
            "classe": Class.objects.count(),
            "docente": Teacher.objects.count(),
            "materia": Subject.objects.count(),
            "assegnazione": Assignment.objects.count(),
            "calendario_annuale": CalendarEntry.objects.count(),
            "disponibilita_settimanale": Availability.objects.count(),
            "classe_accoppiata": ClassPairing.objects.count(),
            "orario_settimanale": Timetable.objects.count(),
            "slot_lezione": Slot.objects.count(),
            "audit_log": AuditLog.objects.count(),
            "orario_feedback": TimetableFeedback.objects.count(),
            "soft_weight_adjustment": SoftWeightAdjustment.objects.count(),
        }
        for table, rep in self.reports.items():
            if rep.source - len(rep.skipped) != rep.created:
                problems.append(
                    f"{table}: source {rep.source} - skipped {len(rep.skipped)} "
                    f"!= created {rep.created}"
                )
            if table in counted and counted[table] != rep.created:
                problems.append(
                    f"{table}: {counted[table]} rows in the new database, "
                    f"{rep.created} created"
                )
        if LedgerEntry.objects.count() != self.ledger_created:
            problems.append(
                f"ledger: {LedgerEntry.objects.count()} entries in the new "
                f"database, {self.ledger_created} created"
            )
        sums = dict(
            LedgerEntry.objects.values("assignment_id")
            .annotate(total=Sum("hours"))
            .values_list("assignment_id", "total")
        )
        for assignment in Assignment.objects.all():
            remaining = assignment.ore_totali - (sums.get(assignment.pk) or 0)
            expected = self.expected_remaining.get(assignment.pk)
            if remaining != expected:
                problems.append(
                    f"assignment {assignment.pk}: remaining hours {remaining}, "
                    f"expected {expected}"
                )
        self.mismatches = problems
        return problems

    # -- report --------------------------------------------------------------------
    def render(self, dry_run, problems):
        lines = []
        if problems:
            lines.append("FAILED: the import was rolled back, nothing was written.")
        elif dry_run:
            lines.append("DRY RUN: nothing was written. This is what would be created.")
        else:
            lines.append("Import done and verified.")
        lines.append("")
        lines.append(f"{'source table':<28}{'source':>8}{'created':>9}{'skipped':>9}")
        for table in IMPORTED:
            rep = self.reports[table]
            lines.append(
                f"{table:<28}{rep.source:>8}{rep.created:>9}{len(rep.skipped):>9}"
            )
        lines.append(f"{'ledger entries (opening)':<28}{'':>8}{self.ledger_created:>9}")
        for table, text in NOT_IMPORTED.items():
            count = (
                len(self.src.rows(table, required=False))
                if table in self.src.tables
                else 0
            )
            lines.append(f"{table:<28}{count:>8}  {text}")
        skipped = [(t, r) for t in IMPORTED for r in self.reports[t].skipped]
        if skipped:
            lines += ["", "Skipped rows:"]
            lines += [f"  {t} {old}: {why}" for t, (old, why) in skipped]
        if self.notes:
            lines += ["", "Notes:"] + [f"  {n}" for n in self.notes]
        if problems:
            lines += ["", "Verification mismatches:"] + [f"  {p}" for p in problems]
        return "\n".join(lines)


def run(url, *, dry_run=False):
    """Run the import; return (report text, ok). Raises ImportRefused."""
    check_target_empty()
    try:
        conn = psycopg.connect(
            url, row_factory=dict_row, options="-c default_transaction_read_only=on"
        )
    except Exception as exc:
        raise ImportRefused(
            f"Cannot connect to the source database: {scrub(exc, url)}"
        ) from None
    problems = []
    try:
        conn.read_only = True
        importer = Importer(Source(conn))
        try:
            with transaction.atomic():
                importer.run_all()
                problems = importer.verify()
                if problems or dry_run:
                    raise _Rollback
        except _Rollback:
            pass
        except ImportRefused:
            raise
        except Exception as exc:
            raise ImportRefused(
                f"Import aborted, nothing was written: "
                f"{type(exc).__name__}: {scrub(exc, url)}"
            ) from None
        report = importer.render(dry_run, problems)
    finally:
        conn.close()
    return report, not problems
