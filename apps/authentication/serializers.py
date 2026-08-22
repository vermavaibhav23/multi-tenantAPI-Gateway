from django.contrib.auth import authenticate, get_user_model
from rest_framework import serializers

User = get_user_model()


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        user = authenticate(username=attrs['email'], password=attrs['password'])
        if user is None:
            user = User.objects.filter(email=attrs['email']).first()
            if user is None or not user.check_password(attrs['password']):
                raise serializers.ValidationError('Invalid email or password.')
        return {'user': user}
