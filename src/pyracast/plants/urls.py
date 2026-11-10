from django.urls import path

from pyracast.api.trash import RestorePlantView
from pyracast.plants.api import (
    ActivateView,
    CalibrationsView,
    CalibrationView,
    ForecastView,
    ObservationsView,
    PerformanceView,
    PlantsView,
    PlantView,
    ProductionView,
    ResetView,
)

urlpatterns = [
    path("", PlantsView.as_view()),
    path("<int:plant_id>/", PlantView.as_view()),
    path("<int:plant_id>/restore/", RestorePlantView.as_view()),
    path("<int:plant_id>/observations/", ObservationsView.as_view()),
    path("<int:plant_id>/production/", ProductionView.as_view()),
    path("<int:plant_id>/performance/", PerformanceView.as_view()),
    path("<int:plant_id>/calibrations/", CalibrationsView.as_view()),
    path("<int:plant_id>/calibrations/<int:calibration_id>/", CalibrationView.as_view()),
    path("<int:plant_id>/calibrations/<int:calibration_id>/activate/", ActivateView.as_view()),
    path("<int:plant_id>/calibration/reset/", ResetView.as_view()),
    path("<int:plant_id>/forecast/", ForecastView.as_view()),
]
