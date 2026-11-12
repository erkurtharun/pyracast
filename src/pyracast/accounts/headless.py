from allauth.headless.account import views

from pyracast.accounts import inputs


class LoginView(views.LoginView):
    input_class = inputs.LoginInput


class RequestPasswordResetView(views.RequestPasswordResetView):
    input_class = inputs.RequestPasswordResetInput


class ResetPasswordView(views.ResetPasswordView):
    input_class = inputs.ResetPasswordInput


class ChangePasswordView(views.ChangePasswordView):
    input_class = inputs.ChangePasswordInput


class ReauthenticateView(views.ReauthenticateView):
    input_class = inputs.ReauthenticateInput
