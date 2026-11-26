from django.db import models


class DayReason(models.TextChoices):
    """Why a day of the production comparison is not compared."""

    FORECAST_NOT_AVAILABLE = "forecast_not_available"
    ESTIMATED_ACTUALS_NOT_AVAILABLE = "estimated_actuals_not_available"
    INVALID_FORECAST_DATA = "invalid_forecast_data"
    INCOMPLETE_OR_INVALID_DAY = "incomplete_or_invalid_day"
