from rest_framework import serializers
from django.contrib.auth.models import User
from .models import DesignConfig, Conversation, Message, GeneratedInterface


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email']


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ['username', 'email', 'password']

    def create(self, validated_data):
        user = User.objects.create_user(
            username=validated_data['username'],
            email=validated_data.get('email', ''),
            password=validated_data['password']
        )
        return user


class DesignConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = DesignConfig
        fields = '__all__'
        read_only_fields = ['user', 'created_at']


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = '__all__'
        read_only_fields = ['created_at']


class ConversationSerializer(serializers.ModelSerializer):
    messages = MessageSerializer(many=True, read_only=True)

    class Meta:
        model = Conversation
        fields = '__all__'
        read_only_fields = ['user', 'created_at', 'updated_at']


class GeneratedInterfaceSerializer(serializers.ModelSerializer):
    class Meta:
        model = GeneratedInterface
        fields = '__all__'
        read_only_fields = ['user', 'created_at', 'updated_at']