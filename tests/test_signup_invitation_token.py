import pytest

from pyracast.accounts.forms import AccountSignupForm


@pytest.mark.parametrize("token", ["", " ", "\t\r\n", "\u2003"])
def test_blank_invitation_token_preserves_workspace_name(token: str) -> None:
    form = AccountSignupForm(
        data={"company_name": "  Sunfield   Energy  ", "invitation_token": token}
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["company_name"] == "Sunfield Energy"
    assert form.cleaned_data["invitation_token"] == ""
