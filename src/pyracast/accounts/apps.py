from django.apps import AppConfig
from django.contrib.auth.signals import user_logged_in


class AccountsConfig(AppConfig):
    name = "pyracast.accounts"

    def ready(self) -> None:
        from pyracast.accounts import checks  # noqa: F401 -- Register deployment checks.
        from pyracast.accounts.audit import connect_security_events
        from pyracast.accounts.security import start_user_session
        from pyracast.accounts.sessions import connect_session_revocation

        user_logged_in.connect(start_user_session, dispatch_uid="pyracast.start_user_session")
        connect_security_events()
        connect_session_revocation()
