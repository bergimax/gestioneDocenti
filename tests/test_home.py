import pytest
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.fixture
def admin_client(client, db):
    User.objects.create_user("a@example.com", "pw-correct-horse", "ADMIN")
    client.login(username="a@example.com", password="pw-correct-horse")
    return client


def test_home_returns_200_when_logged_in(admin_client):
    assert admin_client.get("/").status_code == 200


def test_home_anonymous_redirects_to_login(client):
    response = client.get("/")

    assert response.status_code == 302
    assert response.url == "/login/?next=/"
