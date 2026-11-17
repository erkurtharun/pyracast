from django.apps import AppConfig
from django.contrib.auth.signals import user_logged_in
from django.core.signals import request_finished, request_started


class AccountsConfig(AppConfig):
    name = "pyracast.accounts"

    def ready(self) -> None:
        from pyracast.accounts import checks  # noqa: F401 -- Register deployment checks.
        from pyracast.accounts.adapters import deliver_held_account_mail, hold_account_mail
        from pyracast.accounts.audit import connect_security_events
        from pyracast.accounts.security import start_user_session
        from pyracast.accounts.sessions import connect_session_revocation

        user_logged_in.connect(start_user_session, dispatch_uid="pyracast.start_user_session")
        # A WSGI server closes the response, which sends request_finished, after writing it.
        request_started.connect(hold_account_mail, dispatch_uid="pyracast.hold_account_mail")
        request_finished.connect(
            deliver_held_account_mail, dispatch_uid="pyracast.deliver_held_account_mail"
        )
        connect_security_events()
        connect_session_revocation()
