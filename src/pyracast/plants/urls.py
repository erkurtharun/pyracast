from django.urls import path

from pyracast.change_requests.api import IncidentRequestsView, ProductionRequestsView
from pyracast.incidents.api import CancelView, IncidentsView, IncidentView, ResolveView
from pyracast.plants.api import (
    ActivateView,
    CalibrationAutomationView,
    CalibrationsView,
    CalibrationView,
    ForecastView,
    ObservationsView,
    PerformanceView,
    PlantsView,
    PlantView,
    ProductionChangesView,
    ProductionPreviewView,
    ProductionView,
    ResetView,
)
from pyracast.plants.epias_api import EpiasConnectionView, EpiasHistoryView

urlpatterns = [
    path("", PlantsView.as_view()),
    path("<int:plant_id>/", PlantView.as_view()),
    path("<int:plant_id>/change-requests/production/", ProductionRequestsView.as_view()),
    path("<int:plant_id>/change-requests/incidents/", IncidentRequestsView.as_view()),
    path("<int:plant_id>/outages/", IncidentsView.as_view()),
    path("<int:plant_id>/outages/<int:incident_id>/", IncidentView.as_view()),
    path("<int:plant_id>/outages/<int:incident_id>/resolve/", ResolveView.as_view()),
    path("<int:plant_id>/outages/<int:incident_id>/cancel/", CancelView.as_view()),
    path("<int:plant_id>/observations/", ObservationsView.as_view()),
    path("<int:plant_id>/production/", ProductionView.as_view()),
    path("<int:plant_id>/production/changes/", ProductionChangesView.as_view()),
    path("<int:plant_id>/production/preview/", ProductionPreviewView.as_view()),
    path("<int:plant_id>/production-source/", EpiasConnectionView.as_view()),
    path("<int:plant_id>/reported-production/", EpiasHistoryView.as_view()),
    path("<int:plant_id>/performance/", PerformanceView.as_view()),
    path("<int:plant_id>/calibrations/", CalibrationsView.as_view()),
    path("<int:plant_id>/calibrations/<int:calibration_id>/", CalibrationView.as_view()),
    path("<int:plant_id>/calibrations/<int:calibration_id>/activate/", ActivateView.as_view()),
    path("<int:plant_id>/calibration/reset/", ResetView.as_view()),
    path("<int:plant_id>/calibration/automation/", CalibrationAutomationView.as_view()),
    path("<int:plant_id>/forecast/", ForecastView.as_view()),
]
