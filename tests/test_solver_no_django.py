import ast
import subprocess
import sys
from pathlib import Path

import scheduling.solver

PACKAGE = Path(scheduling.solver.__file__).parent
ALLOWED = {
    "ortools",
    "collections",
    "dataclasses",
    "enum",
    "math",
    "time",
    "uuid",
    "__future__",
}


def test_solver_imports_with_django_blocked():
    code = (
        "import sys; sys.modules['django'] = None\n"
        "import scheduling, scheduling.solver\n"
        "assert not any(m.split('.')[0] == 'django' for m in sys.modules "
        "if sys.modules[m] is not None)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_solver_files_import_only_stdlib_and_ortools():
    for path in PACKAGE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                roots = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots = {node.module.split(".")[0]}
            else:
                continue
            assert roots <= ALLOWED, (path.name, roots)
