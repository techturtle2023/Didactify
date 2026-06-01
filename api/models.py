from django.db import models
from django.contrib.auth.models import User
from django.contrib.auth.models import User

class DesignConfig(models.Model):
    """Configuraciones de diseño guardadas por el usuario"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='design_configs')
    name = models.CharField(max_length=100)
    style = models.CharField(max_length=50)  # moderno, minimalista, educativo, oscuro
    font = models.CharField(max_length=50)
    palette_name = models.CharField(max_length=50, blank=True)
    palette_colors = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.name}"


class Conversation(models.Model):
    """Conversaciones del usuario con la IA"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='conversations')
    title = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} - {self.title}"


class Message(models.Model):
    """Mensajes dentro de una conversación"""
    ROLE_CHOICES = [
        ('user', 'Usuario'),
        ('assistant', 'Asistente'),
    ]
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.role} - {self.conversation.title}"


class GeneratedInterface(models.Model):
    """Interfaces HTML generadas y guardadas"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='interfaces')
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='interfaces', null=True, blank=True)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    html_code = models.TextField()
    style = models.CharField(max_length=50, blank=True)
    font = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} - {self.title}"
    
class MultiPageProject(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='multipage_projects')
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    pages = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.user.username})"