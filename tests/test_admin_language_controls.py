import pytest

from tests.test_admin_setup import admin_system
from tests.test_api import verified_admin


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
@pytest.mark.parametrize("language", ["en", "tr"])
def test_admin_language_controls_preserve_path_and_show_selection(language: str) -> None:
    admin_system()
    client, _ = verified_admin()
    response = client.get("/admin/plants/?status=pending", HTTP_ACCEPT_LANGUAGE=language)
    assert response.status_code == 200
    html = response.content.decode()
    assert html.count('action="/admin/language/"') == 1
    assert 'method="post" action="/admin/language/"' in html
    assert 'name="csrfmiddlewaretoken"' in html
    assert 'name="next" value="/admin/plants/?status=pending"' in html
    assert ">Türkçe</button>" in html
    for code in ("en", "tr"):
        pressed = str(language == code).lower()
        assert f'name="language" value="{code}" lang="{code}" aria-pressed="{pressed}"' in html
