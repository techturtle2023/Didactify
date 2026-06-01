from django.contrib import admin
from .models import DesignConfig, Conversation, Message, GeneratedInterface

@admin.register(DesignConfig)
class DesignConfigAdmin(admin.ModelAdmin):
    list_display = ['user', 'name', 'style', 'font', 'created_at']
    list_filter = ['style', 'font']

@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ['user', 'title', 'created_at', 'updated_at']

@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ['conversation', 'role', 'created_at']
    list_filter = ['role']

@admin.register(GeneratedInterface)
class GeneratedInterfaceAdmin(admin.ModelAdmin):
    list_display = ['user', 'title', 'style', 'font', 'created_at']
    list_filter = ['style', 'font']