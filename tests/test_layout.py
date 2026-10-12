import re
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import Client

User = get_user_model()
PW = "correct-horse"
ALL = ["Panoramica", "Disponibilita", "Orario", "Setup", "Dati scuola", "Pesi"]
PAGES = {
    "/disponibilita/": "Disponibilita",
    "/orario/": "Orario",
    "/setup/": "Setup",
    "/dati-scuola/": "Dati scuola",
}


def make_client(role):
    User.objects.create_user(f"{role.lower()}@example.com", PW, role)
    c = Client()
    c.force_login(User.objects.get(email=f"{role.lower()}@example.com"))
    return c


@pytest.fixture
def admin(db):
    return make_client("ADMIN")


@pytest.fixture
def seg(db):
    return make_client("SEGRETERIA")


def menu(html):
    nav = re.search(r"<nav.*?</nav>", html, re.S)
    assert nav, "no nav"
    return re.findall(r"<a [^>]*>([^<]+)</a>", nav.group(0))


def test_menu_admin(admin):
    assert menu(admin.get("/").content.decode()) == ALL


def test_menu_segreteria(seg):
    html = seg.get("/").content.decode()
    assert menu(html) == ALL[:3]
    assert "Setup" not in html.replace("Verifica", "")
    assert "Dati scuola" not in html


def test_menu_follows_predicates(seg):
    with mock.patch(
        "projects.context_processors.MENU", (("X", "home", lambda u: True),)
    ):
        assert menu(seg.get("/").content.decode()) == ["X"]


def test_anonymous_sees_no_menu_or_footer(client):
    html = client.get("/login/").content.decode()
    assert "<nav" not in html
    assert "Cambia password" not in html
    assert "Esci" not in html
    assert 'lang="it"' in html


@pytest.mark.parametrize("path,name", PAGES.items())
def test_placeholders_admin(admin, path, name):
    r = admin.get(path)
    assert r.status_code == 200
    assert f"<h1>{name}</h1>" in r.content.decode()


@pytest.mark.parametrize("path", ["/setup/", "/dati-scuola/"])
def test_admin_only_placeholders(seg, client, path):
    assert seg.get(path).status_code == 403
    r = client.get(path)
    assert r.status_code == 302
    assert r.url == f"/login/?next={path}"


@pytest.mark.parametrize("path", ["/disponibilita/", "/orario/"])
def test_shared_placeholders(seg, client, path):
    assert seg.get(path).status_code == 200
    assert client.get(path).status_code == 302


def test_active_entry_marked(admin):
    html = admin.get("/orario/").content.decode()
    assert len(re.findall('aria-current="page"', html)) == 1
    assert re.search(r'class="active" aria-current="page">Orario<', html)


def test_footer(seg):
    html = seg.get("/").content.decode()
    assert 'href="/password/">Cambia password' in html
    assert "segreteria@example.com" in html
    assert "Segreteria / Sola lettura" in html
    assert re.search(
        r'<form method="post" action="/logout/">\s*<input[^>]*csrfmiddlewaretoken', html
    )


def test_footer_admin_label(admin):
    html = admin.get("/").content.decode()
    assert "Amministratore" in html
    assert "Sola lettura" not in html


def test_logout_post_only(admin):
    assert admin.get("/logout/").status_code == 405
    assert "_auth_user_id" in admin.session
    admin.post("/logout/")
    assert "_auth_user_id" not in admin.session


def test_password_page_single_esci(admin):
    html = admin.get("/password/").content.decode()
    assert html.count(">Esci<") == 1


def test_messages_render(admin):
    from django.contrib.messages import constants
    from django.contrib.messages.storage.cookie import CookieStorage

    request = admin.get("/").wsgi_request
    storage = CookieStorage(request)
    storage.add(constants.SUCCESS, "ok-s")
    storage.add(constants.ERROR, "ok-e")
    storage.add(constants.WARNING, "ok-w")
    storage.add(constants.INFO, "<script>alert(1)</script>")
    response = admin.get("/")
    storage.update(response)
    admin.cookies.update(response.cookies)
    html = admin.get("/").content.decode()
    for lvl in ("success", "error", "warning", "info"):
        assert f"message-{lvl}" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "@click" in html and "x-data" in html


def test_password_change_message(admin):
    r = admin.post(
        "/password/",
        {
            "old_password": PW,
            "new_password1": "brand-new-pass-1",
            "new_password2": "brand-new-pass-1",
        },
        follow=True,
    )
    assert "Password modificata." in r.content.decode()


def test_static_files_found_and_referenced(admin):
    html = admin.get("/").content.decode()
    for name in (
        "css/app.css",
        "vendor/htmx-2.0.11.min.js",
        "vendor/alpine-3.17.4.min.js",
    ):
        assert finders.find(name)
        assert f'"/static/{name}"' in html
    assert not re.search(r'(src|href)="(https?:)?//', html)


def test_toggle_present(admin):
    html = admin.get("/").content.decode()
    assert "nav-toggle" in html and "aria-expanded" in html


def test_htmx_partial_with_csrf_enforced(admin):
    c = Client(enforce_csrf_checks=True)
    c.force_login(User.objects.get(email="admin@example.com"))
    page = c.get("/")
    token = re.search(r'"X-CSRFToken": "([^"]+)"', page.content.decode()).group(1)
    assert "hx-headers" in page.content.decode()
    ok = c.post("/partials/ping/", HTTP_HX_REQUEST="true", HTTP_X_CSRFTOKEN=token)
    assert ok.status_code == 200
    bad = c.post("/partials/ping/", HTTP_HX_REQUEST="true")
    assert bad.status_code == 403
