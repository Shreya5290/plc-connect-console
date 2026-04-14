from django.urls import path
from . import views

urlpatterns = [
    path('', views.CombinedPageView.as_view(), name='index'),
]
