"""Cross-file validation of parsed setup data (specs section 5.4). Pure."""

from dataclasses import dataclass, field

from .parsers import Message, normalize_name


@dataclass
class ValidationResult:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def can_proceed(self):
        return not self.errors


def _names(rows, key):
    return {normalize_name(r[key]) for r in rows}


def validate_all(parsed):
    result = ValidationResult()

    def err(file_type, row, text):
        result.errors.append(
            Message("error", file_type, row, f"{file_type} row {row}: {text}")
        )

    def warn(file_type, row, text):
        result.warnings.append(
            Message("warning", file_type, row, f"{file_type} row {row}: {text}")
        )

    teachers = parsed.get("teachers")
    classes = parsed.get("classes")
    subjects = parsed.get("subjects")
    assignments = parsed.get("assignments")
    pairings = parsed.get("pairings")
    calendar = parsed.get("calendar")

    known = {
        "teacher": (teachers, "nome"),
        "class": (classes, "nome"),
        "subject": (subjects, "nome"),
    }
    names = {
        k: _names(rows, col) for k, (rows, col) in known.items() if rows is not None
    }

    if assignments is not None:
        for r in assignments:
            for kind, col in (
                ("teacher", "docente_nome"),
                ("class", "classe_nome"),
                ("subject", "materia_nome"),
            ):
                if kind in names and normalize_name(r[col]) not in names[kind]:
                    err("assignments", r["row"], f"{kind} '{r[col]}' does not exist")

    if pairings is not None:
        for r in pairings:
            for col in ("classe_a", "classe_b"):
                if "class" in names and normalize_name(r[col]) not in names["class"]:
                    err("pairings", r["row"], f"class '{r[col]}' does not exist")
            if (
                "subject" in names
                and normalize_name(r["materia_nome"]) not in names["subject"]
            ):
                err(
                    "pairings",
                    r["row"],
                    f"subject '{r['materia_nome']}' does not exist",
                )

    if pairings is not None and assignments is not None:
        have = {
            (normalize_name(a["classe_nome"]), normalize_name(a["materia_nome"]))
            for a in assignments
        }
        for r in pairings:
            for col in ("classe_a", "classe_b"):
                key = (normalize_name(r[col]), normalize_name(r["materia_nome"]))
                if key not in have:
                    err(
                        "pairings",
                        r["row"],
                        f"class '{r[col]}' has no assignment for subject "
                        f"'{r['materia_nome']}'",
                    )

    if calendar is not None and "class" in names:
        for r in calendar:
            stage = r.get("stage_classe_id")
            if stage and normalize_name(stage) not in names["class"]:
                err("calendar", r["row"], f"stage class '{stage}' does not exist")

    if assignments is not None:
        used = {
            "teacher": _names(assignments, "docente_nome"),
            "class": _names(assignments, "classe_nome"),
            "subject": _names(assignments, "materia_nome"),
        }
        files = {"teacher": "teachers", "class": "classes", "subject": "subjects"}
        texts = {
            "teacher": "teacher '{}' has no assignments",
            "class": "class '{}' has no assignments",
            "subject": "subject '{}' is not used by any assignment",
        }
        for kind, (rows, col) in known.items():
            if rows is None:
                continue
            for r in rows:
                if normalize_name(r[col]) not in used[kind]:
                    warn(files[kind], r["row"], texts[kind].format(r[col]))

    return result
