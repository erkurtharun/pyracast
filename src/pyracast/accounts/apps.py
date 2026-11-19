from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "pyracast.accounts"

    def ready(self) -> None:
        from pyracast.accounts import checks  # noqa: F401 -- Register deployment checks.
        from pyracast.accounts.audit import connect_security_events
        from pyracast.accounts.sessions import connect_session_lifecycle
        from pyracast.accounts.social import connect_identity_recovery

        connect_security_events()
        connect_session_lifecycle()
        connect_identity_recovery()
