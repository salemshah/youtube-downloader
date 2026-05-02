from django.urls import path
from .views import extract, download_high_quality, serve_download

urlpatterns = [
    path('extract/',                          extract,               name='extract'),
    path('download-high-quality/',            download_high_quality, name='download_high_quality'),
    path('serve-download/<str:token>/',       serve_download,        name='serve_download'),
]
