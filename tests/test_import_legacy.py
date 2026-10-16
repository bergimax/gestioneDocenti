"""manage.py import_legacy against a fixture source database.

The source is a throwaway Postgres database next to the test one, built here
from the table definitions of specs.md section 3 and dropped afterwards. No
real school data is used.
"""

import datetime
import hashlib
import io
import uuid
from urllib.parse import quote

import psycopg
import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from psycopg import sql
from psycopg.types.json import Jsonb

from accounts.models import User
from availability.models import Availability
from ledger.models import LedgerEntry
from scheduling.models import AuditLog, Slot, SoftWeightAdjustment, Timetable
from school import legacy_import
from school.management.commands.import_legacy import Command
from school.models import (
    Assignment,
    CalendarEntry,
    Class,
    ClassPairing,
    School,
    Subject,
    Teacher,
)

D = datetime.date
MON = D(2026, 10, 12)
T0 = datetime.datetime(2025, 3, 4, 10, 30, tzinfo=datetime.UTC)
PASSWORD = "s3cret-Pw!"

DDL = """
CREATE TABLE scuola (id text PRIMARY KEY,
  nome text NOT NULL, anno_formativo text NOT NULL,
  data_inizio_anno date NOT NULL, data_fine_anno date NOT NULL);
CREATE TABLE admin (id text PRIMARY KEY, scuola_id text, email text NOT NULL,
  password_hash text, role text);
CREATE TABLE classe (id text PRIMARY KEY, scuola_id text, nome text NOT NULL,
  n_studenti int DEFAULT 0, gruppo text);
CREATE TABLE docente (id text PRIMARY KEY,
  scuola_id text, nome text NOT NULL, email text,
  tipo text NOT NULL, active bool DEFAULT true, first_time_this_year bool DEFAULT true);
CREATE TABLE materia (id text PRIMARY KEY, scuola_id text, nome text, tipo text,
  peso_cognitivo text);
CREATE TABLE assegnazione (id text PRIMARY KEY, scuola_id text, docente_id text,
  classe_id text, materia_id text);
CREATE TABLE monte_ore_annuale (id text PRIMARY KEY, scuola_id text, classe_id text,
  materia_id text, docente_id text, ore_totali int NOT NULL, ore_erogate int DEFAULT 0,
  created_at timestamptz);
CREATE TABLE calendario_annuale (id text PRIMARY KEY,
  scuola_id text, data date NOT NULL,
  gruppo text, ore_max_giornata int DEFAULT 6, ora_inizio_min int, flag_chiusura bool,
  flag_stage_classe_id text, flag_stage_gruppo bool);
CREATE TABLE disponibilita_settimanale (id text PRIMARY KEY, scuola_id text,
  docente_id text, settimana_inizio date, giorni_fasce jsonb, created_at timestamptz,
  updated_at timestamptz);
CREATE TABLE classe_accoppiata (id text PRIMARY KEY, scuola_id text, classe_a_id text,
  classe_b_id text, materia_id text, docente_id text, note text);
CREATE TABLE orario_settimanale (id text PRIMARY KEY, scuola_id text,
  settimana_inizio date, stato text DEFAULT 'BOZZA', quality_score float,
  quality_level text, n_conflitti_soft int, approved_at timestamptz);
CREATE TABLE slot_lezione (id text PRIMARY KEY,
  orario_settimanale_id text, classe_id text,
  docente_id text, materia_id text, giorno text, ora_inizio int, ora_fine int,
  accoppiata bool, classe_accoppiata_id text);
CREATE TABLE chat_message (id text PRIMARY KEY, messaggio text);
CREATE TABLE preferenze_ai_memory (id text PRIMARY KEY, regola text);
CREATE TABLE conflitto_gestito (id text PRIMARY KEY, chiave text);
CREATE TABLE orario_feedback (id text PRIMARY KEY, voto int);
CREATE TABLE admin_session (token text PRIMARY KEY, admin_id text);
CREATE TABLE soft_weight_adjustment (id text PRIMARY KEY, kind text, old_weight float,
  new_weight float, evidenza jsonb, admin_id text, created_at timestamptz);
CREATE TABLE audit_log (id text PRIMARY KEY, scuola_id text, orario_settimanale_id text,
  azione text, admin_id text, dettagli jsonb, "timestamp" timestamptz);
"""
TABLES = (
    "scuola admin classe docente materia assegnazione monte_ore_annuale "
    "calendario_annuale disponibilita_settimanale classe_accoppiata "
    "orario_settimanale slot_lezione chat_message preferenze_ai_memory "
    "conflitto_gestito orario_feedback admin_session soft_weight_adjustment "
    "audit_log"
).split()


def grid():
    days = ("lunedi", "martedi", "mercoledi", "giovedi", "venerdi")
    return {
        d: [{"ora_inizio": "08:00", "ora_fine": "09:00", "disponibile": True}]
        for d in days
    }


class SourceDb:
    def __init__(self, admin_conn, name, url):
        self.admin_conn, self.name, self.url = admin_conn, name, url
        self.conn = psycopg.connect(url, autocommit=True)
        self.conn.execute(DDL)

    def add(self, table, **cols):
        cols = {
            k: Jsonb(v) if isinstance(v, dict | list) else v for k, v in cols.items()
        }
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(map(sql.Identifier, cols)),
            sql.SQL(", ").join(sql.Placeholder() * len(cols)),
        )
        self.conn.execute(query, list(cols.values()))

    def checksum(self):
        digest = hashlib.sha256()
        for table in TABLES:
            rows = self.conn.execute(
                sql.SQL("SELECT t::text FROM {} t ORDER BY 1").format(
                    sql.Identifier(table)
                )
            ).fetchall()
            digest.update(repr((table, rows)).encode())
        return digest.hexdigest()

    def close(self):
        self.conn.close()
        self.admin_conn.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(self.name))
        )
        self.admin_conn.execute(
            sql.SQL("DROP ROLE {}").format(sql.Identifier(self.name))
        )
        self.admin_conn.close()


@pytest.fixture
def src():
    db = settings.DATABASES["default"]
    admin_conn = psycopg.connect(
        dbname="postgres",
        user=db["USER"],
        password=db["PASSWORD"],
        host=db["HOST"],
        port=db["PORT"],
        autocommit=True,
    )
    name = f"legacy_src_{uuid.uuid4().hex[:10]}"
    # The source belongs to a role with a fake password, so the tests can check
    # that the password never reaches the output.
    admin_conn.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(name), sql.Literal(PASSWORD)
        )
    )
    admin_conn.execute(
        sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(name), sql.Identifier(name)
        )
    )
    url = f"postgres://{name}:{quote(PASSWORD)}@{db['HOST']}:{db['PORT']}/{name}"
    source = SourceDb(admin_conn, name, url)
    yield source
    source.close()


def populate(src):
    """A small coherent school; tests add or break rows on top of it."""
    src.add(
        "scuola",
        id="sch_1",
        nome="Scuola Uno",
        anno_formativo="2026-2027",
        data_inizio_anno=D(2026, 9, 1),
        data_fine_anno=D(2027, 6, 30),
    )
    src.add(
        "admin",
        id="ad1",
        scuola_id="sch_1",
        email="Boss@Example.com ",
        password_hash="$old$hash",
        role="ADMIN",
    )
    src.add(
        "admin",
        id="ad2",
        scuola_id="sch_1",
        email="segr@example.com",
        password_hash="$old$hash2",
        role="SEGRETERIA",
    )
    for cid, name, group in (
        ("c1", "1A", "PRIME"),
        ("c2", "1B", "PRIME"),
        ("c3", "2A", None),
    ):
        src.add(
            "classe", id=cid, scuola_id="sch_1", nome=name, n_studenti=20, gruppo=group
        )
    src.add(
        "docente",
        id="t1",
        scuola_id="sch_1",
        nome="ROSSI MARIO",
        email="rossi@example.com",
        tipo="ASSUNTO",
    )
    src.add(
        "docente",
        id="t2",
        scuola_id="sch_1",
        nome="BIANCHI ANNA",
        email=None,
        tipo="CONTRATTO",
        active=False,
        first_time_this_year=False,
    )
    src.add(
        "docente",
        id="t3",
        scuola_id="sch_1",
        nome="VERDI LUCA",
        email="v@x.it",
        tipo="ASSUNTO",
    )  # no assignments
    src.add(
        "materia",
        id="m1",
        scuola_id="sch_1",
        nome="Matematica",
        tipo="TEORIA",
        peso_cognitivo="ALTO",
    )
    src.add(
        "materia",
        id="m2",
        scuola_id="sch_1",
        nome="Laboratorio",
        tipo="PRATICA",
        peso_cognitivo="BASSO",
    )
    for aid, t, c, m in (
        ("a1", "t1", "c1", "m1"),
        ("a2", "t2", "c1", "m2"),
        ("a3", "t1", "c2", "m1"),
        ("a4", "t2", "c2", "m2"),
    ):
        src.add(
            "assegnazione",
            id=aid,
            scuola_id="sch_1",
            docente_id=t,
            classe_id=c,
            materia_id=m,
        )
    # a1: 30 total, 10 given; a2: 20 total, 0 given; a3: no hours row; a4: 15, 15
    src.add(
        "monte_ore_annuale",
        id="h1",
        scuola_id="sch_1",
        docente_id="t1",
        classe_id="c1",
        materia_id="m1",
        ore_totali=30,
        ore_erogate=10,
        created_at=T0,
    )
    src.add(
        "monte_ore_annuale",
        id="h2",
        scuola_id="sch_1",
        docente_id="t2",
        classe_id="c1",
        materia_id="m2",
        ore_totali=20,
        ore_erogate=0,
    )
    src.add(
        "monte_ore_annuale",
        id="h4",
        scuola_id="sch_1",
        docente_id="t2",
        classe_id="c2",
        materia_id="m2",
        ore_totali=15,
        ore_erogate=15,
    )
    src.add(
        "calendario_annuale",
        id="k1",
        scuola_id="sch_1",
        data=D(2026, 10, 12),
        gruppo=None,
        ore_max_giornata=6,
        flag_chiusura=False,
        flag_stage_gruppo=False,
    )
    src.add(
        "calendario_annuale",
        id="k2",
        scuola_id="sch_1",
        data=D(2026, 10, 13),
        gruppo="PRIME",
        ore_max_giornata=8,
        flag_chiusura=False,
        flag_stage_classe_id="c1",
        flag_stage_gruppo=False,
    )
    src.add(
        "disponibilita_settimanale",
        id="d1",
        scuola_id="sch_1",
        docente_id="t1",
        settimana_inizio=MON,
        giorni_fasce=grid(),
        created_at=T0,
        updated_at=T0 + datetime.timedelta(days=1),
    )
    src.add(
        "classe_accoppiata",
        id="p1",
        scuola_id="sch_1",
        classe_a_id="c2",
        classe_b_id="c1",
        materia_id="m1",
        docente_id=None,
        note="insieme",
    )
    src.add(
        "orario_settimanale",
        id="o1",
        scuola_id="sch_1",
        settimana_inizio=MON,
        stato="APPROVATO",
        quality_score=87.5,
        quality_level="B",
        n_conflitti_soft=3,
        approved_at=T0,
    )
    src.add(
        "orario_settimanale",
        id="o2",
        scuola_id="sch_1",
        settimana_inizio=MON + datetime.timedelta(days=7),
        stato="BOZZA",
    )
    src.add(
        "slot_lezione",
        id="s1",
        orario_settimanale_id="o1",
        classe_id="c1",
        docente_id="t1",
        materia_id="m1",
        giorno="LUNEDI",
        ora_inizio=8,
        ora_fine=9,
        accoppiata=True,
        classe_accoppiata_id="c2",
    )
    src.add(
        "slot_lezione",
        id="s2",
        orario_settimanale_id="o1",
        classe_id="c2",
        docente_id="t2",
        materia_id="m2",
        giorno="MARTEDI",
        ora_inizio=9,
        ora_fine=10,
        accoppiata=False,
    )
    src.add(
        "audit_log",
        id="l1",
        scuola_id="sch_1",
        orario_settimanale_id="o1",
        azione="GENERATE",
        admin_id="ad1",
        dettagli={"n": 1},
        timestamp=T0,
    )
    src.add(
        "audit_log",
        id="l2",
        scuola_id="sch_1",
        orario_settimanale_id=None,
        azione="QUICK_ACTION",
        admin_id="gone",
        dettagli={},
        timestamp=T0,
    )
    src.add(
        "soft_weight_adjustment",
        id="w1",
        kind="gaps",
        old_weight=10,
        new_weight=20,
        evidenza={"picks": 3},
        admin_id="ad2",
        created_at=T0 + datetime.timedelta(hours=1),
    )
    src.add("chat_message", id="x1", messaggio="hi")
    src.add("orario_feedback", id="f1", voto=4)
    src.add("orario_feedback", id="f2", voto=5)


def do_import(src, *args, **kw):
    out = io.StringIO()
    err = io.StringIO()
    try:
        call_command(
            "import_legacy", "--source", src.url, *args, stdout=out, stderr=err, **kw
        )
    except CommandError as exc:
        return out.getvalue(), str(exc), False
    return out.getvalue(), "", True


def run_ok(src, *args):
    out, err, ok = do_import(src, *args)
    assert ok, out + err
    return out


def target_counts():
    models = (
        School,
        Class,
        Teacher,
        Subject,
        Assignment,
        CalendarEntry,
        Availability,
        ClassPairing,
        Timetable,
        Slot,
        AuditLog,
        SoftWeightAdjustment,
        LedgerEntry,
    )
    return {m.__name__: m.objects.count() for m in models}


def skipped(out, table):
    return [ln for ln in out.splitlines() if ln.startswith(f"  {table} ")]


pytestmark = pytest.mark.django_db


def test_help_documents_options():
    text = Command().create_parser("manage.py", "import_legacy").format_help()
    assert "--source" in text and "--dry-run" in text
    assert "IMPORT_SOURCE_URL" in text and "Exit codes" in text


def test_env_var_is_used_when_no_source(src, monkeypatch):
    populate(src)
    monkeypatch.setenv("IMPORT_SOURCE_URL", src.url)
    call_command("import_legacy", stdout=io.StringIO())
    assert School.objects.count() == 1


def test_no_source_at_all_fails(monkeypatch):
    monkeypatch.delenv("IMPORT_SOURCE_URL", raising=False)
    with pytest.raises(CommandError, match="IMPORT_SOURCE_URL"):
        call_command("import_legacy", stdout=io.StringIO())


def test_env_example_documents_the_variable():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
    assert "# IMPORT_SOURCE_URL=" in text


def test_source_is_read_only_and_unchanged(src):
    populate(src)
    before = src.checksum()
    run_ok(src)
    assert src.checksum() == before
    conn = psycopg.connect(src.url, options="-c default_transaction_read_only=on")
    conn.read_only = True
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("DELETE FROM classe")
    conn.close()


def test_connection_opened_by_the_command_is_read_only(src, monkeypatch):
    populate(src)
    seen = {}
    real = legacy_import.Importer.run_all

    def spy(self):
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            self.src.conn.execute("DELETE FROM classe")
        self.src.conn.rollback()
        seen["ok"] = True
        real(self)

    monkeypatch.setattr(legacy_import.Importer, "run_all", spy)
    run_ok(src)
    assert seen["ok"]


def _school():
    return School.objects.create(
        name="X",
        formative_year="2026",
        year_start=D(2026, 1, 1),
        year_end=D(2026, 12, 1),
    )


def test_non_empty_target_is_refused_for_any_imported_table(src):
    populate(src)
    school = _school()
    Teacher.objects.create(school=school, name="Z", type="ASSUNTO")
    School.objects.filter(pk=school.pk)  # the teacher alone is enough? no: school too
    before = target_counts()
    out, err, ok = do_import(src)
    assert not ok and "not empty" in err and out == ""
    assert target_counts() == before


def test_refusal_names_the_table(src):
    populate(src)
    School.objects.create(
        name="X",
        formative_year="2026",
        year_start=D(2026, 1, 1),
        year_end=D(2026, 12, 1),
    )
    _, err, ok = do_import(src)
    assert not ok and "school_school" in err


def test_existing_users_do_not_make_the_target_non_empty(src):
    populate(src)
    User.objects.create_user("first@example.com", "pw-long-enough", "ADMIN")
    run_ok(src)
    assert School.objects.count() == 1


def test_dry_run_reports_and_writes_nothing(src):
    populate(src)
    out = run_ok(src, "--dry-run")
    assert "DRY RUN" in out
    assert all(n == 0 for n in target_counts().values())
    assert User.objects.count() == 0
    real = run_ok(src)
    # the same table of numbers
    assert [ln for ln in out.splitlines() if ln.startswith("assegnazione")] == [
        ln for ln in real.splitlines() if ln.startswith("assegnazione")
    ]


def test_failure_midway_rolls_back_and_second_run_succeeds(src, monkeypatch):
    populate(src)
    real = legacy_import.Importer.timetables

    def boom(self):
        real(self)
        raise RuntimeError("forced")

    monkeypatch.setattr(legacy_import.Importer, "timetables", boom)
    _, err, ok = do_import(src)
    assert not ok and "forced" in err
    assert all(n == 0 for n in target_counts().values())
    assert User.objects.count() == 0
    monkeypatch.setattr(legacy_import.Importer, "timetables", real)
    run_ok(src)
    assert Timetable.objects.count() == 2


def test_report_has_one_line_per_table(src):
    populate(src)
    out = run_ok(src)
    lines = {ln.split()[0]: ln.split() for ln in out.splitlines() if ln.strip()}
    assert lines["scuola"][1:4] == ["1", "1", "0"]
    assert lines["assegnazione"][1:4] == ["4", "4", "0"]
    assert lines["monte_ore_annuale"][1:4] == ["3", "3", "0"]
    assert lines["slot_lezione"][1:4] == ["2", "2", "0"]
    assert "orario_feedback" in lines and "no model yet" in out and "#79" in out
    assert lines["orario_feedback"][1] == "2"
    assert "admin_session" in lines and "not imported by design" in out


def test_verification_mismatch_fails_and_rolls_back(src, monkeypatch):
    populate(src)
    real = legacy_import.Importer.apply_hours

    def wrong(self):
        real(self)
        key = next(iter(self.expected_remaining))
        self.expected_remaining[key] += 1

    monkeypatch.setattr(legacy_import.Importer, "apply_hours", wrong)
    out, err, ok = do_import(src)
    assert not ok and "Verification" in err
    assert "Verification mismatches" in out and "remaining hours" in out
    assert all(n == 0 for n in target_counts().values())


def test_count_mismatch_fails(src, monkeypatch):
    populate(src)
    real = legacy_import.Importer.classes

    def extra(self):
        real(self)
        self.reports["classe"].created += 1

    monkeypatch.setattr(legacy_import.Importer, "classes", extra)
    out, _, ok = do_import(src)
    assert not ok and "classe:" in out


# -- hours ---------------------------------------------------------------------


def test_hours_become_opening_ledger_entries(src):
    populate(src)
    out = run_ok(src)
    a = {
        (x.teacher.name, x.school_class.name, x.subject.name): x
        for x in Assignment.objects.select_related("teacher", "school_class", "subject")
    }
    a1 = a["ROSSI MARIO", "1A", "Matematica"]
    assert a1.ore_totali == 30
    entries = list(a1.ledger_entries.all())
    assert [
        (e.hours, e.kind, e.week, e.timetable_id, e.created_by_id) for e in entries
    ] == [(10, "ADJUSTMENT", None, None, None)]
    assert entries[0].reason
    assert not a["BIANCHI ANNA", "1A", "Laboratorio"].ledger_entries.exists()
    a4 = a["BIANCHI ANNA", "1B", "Laboratorio"]
    assert a4.ledger_entries.get().hours == 15
    # no hours row: 0 total, warning
    a3 = a["ROSSI MARIO", "1B", "Matematica"]
    assert a3.ore_totali == 0 and not a3.ledger_entries.exists()
    assert "without monte_ore_annuale row" in out and "a3" in out
    assert LedgerEntry.objects.count() == 2


def test_invalid_hours_rows_are_skipped(src):
    populate(src)
    # a3 gets a null-erogate row, a duplicate triple for a1, an orphan triple
    src.add(
        "monte_ore_annuale",
        id="h5",
        scuola_id="sch_1",
        docente_id="t1",
        classe_id="c2",
        materia_id="m1",
        ore_totali=9,
        ore_erogate=None,
    )
    src.add(
        "monte_ore_annuale",
        id="h6",
        scuola_id="sch_1",
        docente_id="t1",
        classe_id="c1",
        materia_id="m1",
        ore_totali=99,
        ore_erogate=1,
        created_at=T0 + datetime.timedelta(days=1),
    )
    src.add(
        "monte_ore_annuale",
        id="h7",
        scuola_id="sch_1",
        docente_id="t3",
        classe_id="c3",
        materia_id="m1",
        ore_totali=9,
        ore_erogate=1,
    )
    src.add(
        "monte_ore_annuale",
        id="h8",
        scuola_id="sch_1",
        docente_id="t2",
        classe_id="c3",
        materia_id="m1",
        ore_totali=9,
        ore_erogate=-2,
    )
    out = run_ok(src)
    reasons = " ".join(skipped(out, "monte_ore_annuale"))
    for old in ("h5", "h6", "h7", "h8"):
        assert f"monte_ore_annuale {old}:" in reasons
    assert "second row" in reasons and "no matching" in reasons
    assert "invalid ore_erogate" in reasons
    a1 = Assignment.objects.get(teacher__name="ROSSI MARIO", school_class__name="1A")
    assert a1.ore_totali == 30  # never summed with the duplicate
    assert LedgerEntry.objects.count() == 2


# -- users -----------------------------------------------------------------------


def test_users_imported_without_password(src):
    populate(src)
    src.add("admin", id="ad3", scuola_id="sch_1", email="NoRole@x.it", role=None)
    src.add("admin", id="ad4", scuola_id="sch_1", email="weird@x.it", role="GOD")
    src.add(
        "admin",
        id="ad5",
        scuola_id="sch_1",
        email="boss@example.com",
        role="SEGRETERIA",
    )
    out = run_ok(src)
    users = {u.email: u for u in User.objects.all()}
    assert set(users) == {
        "boss@example.com",
        "segr@example.com",
        "norole@x.it",
        "weird@x.it",
    }
    assert users["boss@example.com"].role == "ADMIN"  # the first one wins
    assert users["segr@example.com"].role == "SEGRETERIA"
    assert users["norole@x.it"].role == "ADMIN" and users["weird@x.it"].role == "ADMIN"
    assert all(not u.has_usable_password() for u in users.values())
    assert "2 missing or unknown role mapped to ADMIN" in out
    assert "admin ad5: duplicate email" in out


def test_existing_target_user_is_skipped_and_unchanged(src):
    populate(src)
    mine = User.objects.create_user("segr@example.com", "pw-long-enough", "ADMIN")
    out = run_ok(src)
    assert "admin ad2: email segr@example.com already exists" in out
    mine.refresh_from_db()
    assert mine.role == "ADMIN" and mine.has_usable_password()
    assert User.objects.count() == 2


# -- mapping -------------------------------------------------------------------------


def test_ids_map_consistently(src):
    populate(src)
    run_ok(src)
    names = {
        "c1": "1A",
        "c2": "1B",
        "t1": "ROSSI MARIO",
        "t2": "BIANCHI ANNA",
        "m1": "Matematica",
        "m2": "Laboratorio",
    }
    rows = src.conn.execute("SELECT * FROM slot_lezione").fetchall()
    cols = [d.name for d in src.conn.execute("SELECT * FROM slot_lezione").description]
    for raw in rows:
        row = dict(zip(cols, raw, strict=True))
        slot = Slot.objects.get(
            timetable__week_start=MON,
            day=row["giorno"],
            start_hour=row["ora_inizio"],
        )
        assert slot.school_class.name == names[row["classe_id"]]
        assert slot.teacher.name == names[row["docente_id"]]
        assert slot.subject.name == names[row["materia_id"]]
        expected = names.get(row["classe_accoppiata_id"])
        assert (slot.paired_class.name if slot.paired_class else None) == expected
    for asg in Assignment.objects.all():
        assert asg.school_id == School.objects.get().pk
    assert Assignment.objects.filter(
        teacher__name="BIANCHI ANNA",
        school_class__name="1B",
        subject__name="Laboratorio",
    ).exists()


def test_field_mappings(src):
    populate(src)
    run_ok(src)
    school = School.objects.get()
    assert (school.name, school.formative_year) == ("Scuola Uno", "2026-2027")
    assert Teacher.objects.get(name="BIANCHI ANNA").email == ""
    t = Teacher.objects.get(name="BIANCHI ANNA")
    assert (t.type, t.active, t.first_time) == ("CONTRATTO", False, False)
    assert Class.objects.get(name="2A").group == ""
    assert Subject.objects.get(name="Laboratorio").cognitive_weight == "BASSO"
    whole = CalendarEntry.objects.get(group__isnull=True)
    assert whole.date == D(2026, 10, 12)
    stage = CalendarEntry.objects.get(group="PRIME")
    assert (stage.max_hours, stage.stage_class.name, stage.is_group_stage) == (
        8,
        "1A",
        False,
    )
    av = Availability.objects.get()
    assert av.slots == grid() and av.teacher.name == "ROSSI MARIO"
    pair = ClassPairing.objects.get()
    assert (pair.class_a.name, pair.class_b.name) == ("1A", "1B")  # canonical
    assert pair.teacher is None and pair.note == "insieme"
    tt = Timetable.objects.get(week_start=MON)
    assert (
        tt.state,
        tt.quality_score,
        tt.quality_level,
        tt.soft_conflicts,
        tt.approved_at,
        tt.version,
        tt.conflicts,
    ) == ("APPROVATO", 87.5, "B", 3, T0, 0, [])
    assert Timetable.objects.get(state="BOZZA").approved_at is None
    paired = Slot.objects.get(paired=True)
    assert paired.paired_class.name == "1B"
    audit = AuditLog.objects.get(action="GENERATE")
    assert audit.week_start == MON and audit.details == {"n": 1}
    assert audit.user.email == "boss@example.com"
    w = SoftWeightAdjustment.objects.get()
    assert (w.kind, w.old_weight, w.new_weight, w.evidence) == (
        "gaps",
        10,
        20,
        {"picks": 3},
    )
    assert w.user.email == "segr@example.com" and w.school == school


def test_edge_data_imports(src):
    populate(src)
    # zero-row tables, teacher without assignments (t3), null pair teacher,
    # null group, timetable without slots (o2), null email, null-timetable audit
    out = run_ok(src)
    assert Teacher.objects.filter(assignments__isnull=True).count() == 1
    assert Timetable.objects.filter(slots__isnull=True).count() == 1
    assert AuditLog.objects.filter(timetable__isnull=True).count() == 1
    assert "skipped" in out
    assert "Skipped rows" not in out


def test_second_school_and_no_sch_1_constant(src):
    populate(src)
    src.add(
        "scuola",
        id="other",
        nome="Altra",
        anno_formativo="2026-2027",
        data_inizio_anno=D(2026, 9, 1),
        data_fine_anno=D(2027, 6, 30),
    )
    src.add("classe", id="co", scuola_id="other", nome="1A", gruppo=None)
    out = run_ok(src)
    assert School.objects.count() == 2
    assert Class.objects.filter(name="1A").count() == 2  # per-school uniqueness
    assert "cannot tell the school" in out  # weight row, two schools
    import inspect

    assert "sch_1" not in inspect.getsource(legacy_import)
    assert "sch_1" not in inspect.getsource(Command)


# -- bad rows ---


def test_bad_rows_are_skipped_with_old_id_and_reason(src):
    populate(src)
    src.add("classe", id="cx", scuola_id="nope", nome="9Z")  # orphan school
    src.add("classe", id="cg", scuola_id="sch_1", nome="9G", gruppo="BOH")
    src.add(
        "assegnazione",
        id="ax",
        scuola_id="sch_1",
        docente_id="t1",
        classe_id="cx",
        materia_id="m1",
    )  # parent skipped
    src.add(
        "assegnazione",
        id="ay",
        scuola_id="sch_1",
        docente_id="ghost",
        classe_id="c1",
        materia_id="m1",
    )
    src.add(
        "orario_settimanale",
        id="on",
        scuola_id="sch_1",
        settimana_inizio=D(2026, 10, 14),
    )  # Wednesday
    src.add("orario_settimanale", id="od", scuola_id="sch_1", settimana_inizio=MON)
    src.add(
        "slot_lezione",
        id="sx",
        orario_settimanale_id="on",
        classe_id="c1",
        docente_id="t1",
        materia_id="m1",
        giorno="LUNEDI",
        ora_inizio=8,
        ora_fine=9,
        accoppiata=False,
    )  # slot of a skipped timetable
    src.add(
        "disponibilita_settimanale",
        id="dg",
        scuola_id="sch_1",
        docente_id="t2",
        settimana_inizio=MON,
        giorni_fasce={"lunedi": []},
    )
    src.add(
        "disponibilita_settimanale",
        id="dd",
        scuola_id="sch_1",
        docente_id="t1",
        settimana_inizio=MON,
        giorni_fasce=grid(),
    )  # duplicate teacher+week
    src.add(
        "classe_accoppiata",
        id="ps",
        scuola_id="sch_1",
        classe_a_id="c1",
        classe_b_id="c1",
        materia_id="m1",
    )
    src.add(
        "classe_accoppiata",
        id="pd",
        scuola_id="sch_1",
        classe_a_id="c1",
        classe_b_id="c2",
        materia_id="m1",
    )  # duplicate of p1 (null teacher)
    src.add(
        "soft_weight_adjustment",
        id="wf",
        kind="gaps",
        old_weight=1.5,
        new_weight=2,
        created_at=T0,
    )
    src.add(
        "soft_weight_adjustment",
        id="wn",
        kind="gaps",
        old_weight=1,
        new_weight=-2,
        created_at=T0,
    )
    src.add(
        "audit_log",
        id="lo",
        scuola_id="sch_1",
        orario_settimanale_id="on",
        azione="X",
        admin_id="ad1",
        timestamp=T0,
    )
    out = run_ok(src)
    expected = {
        "classe cx": "orphan scuola_id",
        "classe cg": "group",
        "assegnazione ax": "orphan classe_id",
        "assegnazione ay": "orphan docente_id",
        "orario_settimanale on": "lunedi",
        "orario_settimanale od": "esiste già",
        "slot_lezione sx": "orphan orario_settimanale_id",
        "disponibilita_settimanale dg": "slots",
        "disponibilita_settimanale dd": "esiste già",
        "classe_accoppiata ps": "itself",
        "classe_accoppiata pd": "esiste già",
        "soft_weight_adjustment wf": "non-negative integers",
        "soft_weight_adjustment wn": "non-negative integers",
        "audit_log lo": "orphan orario_settimanale_id",
    }
    for key, word in expected.items():
        table, old = key.split()
        line = [ln for ln in skipped(out, table) if f" {old}:" in ln]
        assert line and word in line[0], (key, out)
    assert not Class.objects.filter(name__in=["9Z", "9G"]).exists()
    assert Timetable.objects.count() == 2


def test_unknown_admin_gives_null_user_and_is_counted(src):
    populate(src)
    out = run_ok(src)
    assert AuditLog.objects.get(action="QUICK_ACTION").user is None
    assert "audit_log: 1 unknown admin_id imported with null user" in out
    assert "audit_log l2" not in out


# -- timestamps, append-only, secrets ---


def test_original_timestamps_are_kept(src):
    populate(src)
    run_ok(src)
    assert AuditLog.objects.get(action="GENERATE").created_at == T0
    assert SoftWeightAdjustment.objects.get().created_at == T0 + datetime.timedelta(
        hours=1
    )
    av = Availability.objects.get()
    assert av.created_at == T0 and av.updated_at == T0 + datetime.timedelta(days=1)
    stamped = LedgerEntry.objects.get(hours=10)
    assert stamped.created_at == T0
    other = LedgerEntry.objects.get(hours=15)
    assert other.created_at > T0 + datetime.timedelta(days=30)  # import time
    assert LedgerEntry.objects.filter(timetable__isnull=False).count() == 0


def test_append_only_models_still_refuse_changes(src):
    populate(src)
    run_ok(src)
    for qs in (
        AuditLog.objects.all(),
        SoftWeightAdjustment.objects.all(),
        LedgerEntry.objects.all(),
    ):
        with pytest.raises(NotImplementedError):
            qs.update(id=1)
        with pytest.raises(NotImplementedError):
            qs.delete()
        with pytest.raises(NotImplementedError):
            qs.first().delete()
        with pytest.raises(NotImplementedError):
            qs.first().save()


def test_no_url_or_password_in_any_output(src, monkeypatch):
    populate(src)
    out, err, ok = do_import(src, "--dry-run")
    assert ok and PASSWORD not in out + err and src.url not in out + err
    real = legacy_import.Importer.slots

    def boom(self):
        raise RuntimeError(f"leak {src.url} {PASSWORD}")

    monkeypatch.setattr(legacy_import.Importer, "slots", boom)
    out, err, ok = do_import(src)
    assert not ok and PASSWORD not in out + err and src.url not in out + err
    monkeypatch.setattr(legacy_import.Importer, "slots", real)


def test_connection_failure_shows_no_url_or_password(src):
    bad = type("S", (), {"url": src.url.replace("/" + src.name, "/nonexistent_db")})
    out, err, ok = do_import(bad)
    assert not ok and "Cannot connect" in err
    assert PASSWORD not in out + err and src.url not in err
