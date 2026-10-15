import re
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory
from django.views import View

from projects import permissions as p

User = get_user_model()
ROOT = Path(__file__).resolve().parent.parent

# predicate -> (ADMIN, SEGRETERIA)
MATRIX = {
    p.can_view_timetable: (True, True),
    p.can_view_school_data: (True, True),
    p.can_edit_availability: (True, True),
    p.can_manage_availability_links: (True, True),
    p.can_generate: (True, False),
    p.can_move_lesson: (True, False),
    p.can_assign_slot: (True, False),
    p.can_decide_conflicts: (True, False),
    p.can_apply_quick_action: (True, False),
    p.can_repair: (True, False),
    p.can_choose_alternative: (True, False),
    p.can_export_timetable: (True, True),
    p.can_approve: (True, False),
    p.can_undo_approval: (True, False),
    p.can_adjust_hours: (True, False),
    p.can_view_audit_log: (True, False),
    p.can_manage_feeds: (True, False),
    p.can_run_setup: (True, False),
    p.can_edit_school_data: (True, False),
    p.can_edit_teachers: (True, False),
    p.can_manage_soft_weights: (True, False),
}


def make(role, active=True):
    return User(
        email=f"{(role or 'none').lower()}@example.com", role=role, is_active=active
    )


def test_matrix_covers_every_predicate():
    assert set(MATRIX) == set(p.PREDICATES)


@pytest.mark.parametrize("pred", MATRIX, ids=lambda f: f.__name__)
@pytest.mark.parametrize("obj", [None, object()])
def test_predicate_table(pred, obj):
    admin, segr = MATRIX[pred]
    assert pred(make("ADMIN"), obj) is admin
    assert pred(make("SEGRETERIA"), obj) is segr
    assert pred(make("ADMIN", active=False), obj) is False
    assert pred(make("SEGRETERIA", active=False), obj) is False
    assert pred(AnonymousUser(), obj) is False
    assert pred(None, obj) is False
    assert pred(make("BOSS"), obj) is False
    assert pred(make(""), obj) is False
    assert pred(make(None), obj) is False
    assert pred(object(), obj) is False
    assert pred(make("ADMIN")) is admin


@pytest.mark.parametrize("pred", MATRIX, ids=lambda f: f.__name__)
def test_predicate_default_obj(pred):
    assert pred(None) is False


@p.permission_required(p.can_generate)
def dummy_view(request):
    dummy_view.calls += 1
    return HttpResponse("ok")


dummy_view.calls = 0


class DummyCBV(p.PredicatePermissionMixin, View):
    permission_predicate = staticmethod(p.can_generate)
    calls = 0

    def dispatch_body(self):
        DummyCBV.calls += 1
        return HttpResponse("ok")

    get = post = delete = lambda self, request: self.dispatch_body()


USERS = {
    "admin": (lambda: make("ADMIN"), 200),
    "segreteria": (lambda: make("SEGRETERIA"), 403),
    "inactive": (lambda: make("ADMIN", active=False), 403),
    "invalid": (lambda: make("BOSS"), 403),
}


def call(kind, method, user):
    rf = RequestFactory()
    request = getattr(rf, method)("/dummy/")
    request.user = user
    request.session = {}
    if kind == "fn":
        return dummy_view(request)
    return DummyCBV.as_view()(request)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["fn", "cbv"])
@pytest.mark.parametrize("method", ["get", "post", "delete"])
@pytest.mark.parametrize("who", USERS)
def test_enforcement(kind, method, who):
    build, status = USERS[who]
    dummy_view.calls = DummyCBV.calls = 0
    response = call(kind, method, build())
    assert response.status_code == status
    body_ran = dummy_view.calls + DummyCBV.calls
    assert body_ran == (1 if status == 200 else 0)
    if status == 403:
        text = response.content.decode()
        assert "Permesso negato" in text
        assert "ADMIN" not in text and "SEGRETERIA" not in text


@pytest.mark.parametrize("kind", ["fn", "cbv"])
@pytest.mark.parametrize("method", ["get", "post"])
def test_anonymous_redirects_to_login(kind, method):
    dummy_view.calls = DummyCBV.calls = 0
    response = call(kind, method, AnonymousUser())
    assert response.status_code == 302
    assert response.url.startswith("/login/")
    assert dummy_view.calls + DummyCBV.calls == 0


def test_get_object_runs_before_body_and_feeds_predicate():
    seen = []

    def pred(user, obj):
        seen.append(obj)
        return False

    @p.permission_required(pred, get_object=lambda request, pk: f"obj{pk}")
    def view(request, pk):
        raise AssertionError("body must not run")

    request = RequestFactory().post("/x/")
    request.user = make("ADMIN")
    assert view(request, 7).status_code == 403
    assert seen == ["obj7"]


def test_mixin_get_permission_object():
    seen = []

    class V(p.PredicatePermissionMixin, View):
        permission_predicate = staticmethod(lambda user, obj: seen.append(obj))

        def get_permission_object(self):
            return self.kwargs["pk"]

    request = RequestFactory().get("/x/")
    request.user = make("ADMIN")
    assert V.as_view()(request, pk=3).status_code == 403
    assert seen == [3]


def test_no_role_comparison_outside_permissions_module():
    allowed = {
        ROOT / "projects" / "permissions.py",
        ROOT / "accounts" / "models.py",
        ROOT / "accounts" / "management" / "commands" / "create_user.py",
    }
    skip = {"tests", "migrations", "backend", "frontend", "_docs", ".venv", ".git"}
    pattern = re.compile(r"\.role\b|\bRole\b")
    offenders = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if skip & set(rel.parts) or path in allowed:
            continue
        if pattern.search(path.read_text()):
            offenders.append(str(rel))
    assert offenders == []
