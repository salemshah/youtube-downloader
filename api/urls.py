from django.urls import path
from .views import download_yt

urlpatterns = [
    path('download-yt/', download_yt, name='download_yt'),
]
