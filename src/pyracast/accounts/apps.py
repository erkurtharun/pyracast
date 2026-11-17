from django.apps import AppConfig
from django.contrib.auth.signals import user_logged_in


class AccountsConfig(AppConfig):
    name = "pyracast.accounts"

    def ready(self) -> None:
        from allauth.mfa import signals as mfa_signals

        from pyracast.accounts import checks  # noqa: F401 -- Register deployment checks.
        from pyracast.accounts.adapters import count_failed_second_factor
        from pyracast.accounts.audit import connect_security_events
        from pyracast.accounts.security import start_user_session
        from pyracast.accounts.sessions import connect_session_revocation

        user_logged_in.connect(start_user_session, dispatch_uid="pyracast.start_user_session")
        mfa_signals.authentication_failed.connect(
            count_failed_second_factor, dispatch_uid="pyracast.count_failed_second_factor"
        )
        connect_security_events()
        connect_session_revocation()
