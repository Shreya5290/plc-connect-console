from django.urls import path
from . import views

urlpatterns = [
    path('', views.CombinedPageView.as_view(), name='index'),
    path('api/bridge-status/', views.bridge_status_api, name='bridge_status_api'),
]
