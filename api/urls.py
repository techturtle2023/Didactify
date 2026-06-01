from django.urls import path
from . import views
from .views import generate_multipage, list_multipage_projects, delete_multipage_project

urlpatterns = [
    # Autenticación
    path('auth/register/', views.RegisterView.as_view(), name='register'),
    path('auth/login/', views.LoginView.as_view(), name='login'),
    path('auth/logout/', views.LogoutView.as_view(), name='logout'),
    path('auth/me/', views.CurrentUserView.as_view(), name='current-user'),

    # Conversaciones
    path('conversations/', views.ConversationListCreateView.as_view(), name='conversations'),
    path('conversations/<int:pk>/', views.ConversationDetailView.as_view(), name='conversation-detail'),

    # Generar interfaz con IA
    path('generate/', views.GenerateInterfaceView.as_view(), name='generate'),

    # Interfaces guardadas
    path('interfaces/', views.InterfaceListCreateView.as_view(), name='interfaces'),
    path('interfaces/<int:pk>/', views.InterfaceDetailView.as_view(), name='interface-detail'),

    # Configuraciones de diseño
    path('design-configs/', views.DesignConfigListCreateView.as_view(), name='design-configs'),

    # Multi-página
    path('multipage/generate/', generate_multipage, name='generate_multipage'),
    path('multipage/projects/', list_multipage_projects, name='list_multipage'),
    path('multipage/projects/<int:pk>/', delete_multipage_project, name='delete_multipage'),
]