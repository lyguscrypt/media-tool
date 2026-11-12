from django.urls import path

from . import views

app_name = "media"
urlpatterns = [
    path("", views.index, name="index"),
    path("outputs/<str:name>/", views.output_file, name="output"),
]
