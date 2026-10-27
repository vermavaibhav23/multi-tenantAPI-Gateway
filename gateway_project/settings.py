import os
import sys
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / 'apps'))

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'dev-secret-key-change-me-0123456789abcdef')
DEBUG = os.getenv('DEBUG', 'True').lower() == 'true'
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', '*').split(',') if os.getenv('ALLOWED_HOSTS') else ['*']

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework_simplejwt',
    'users',
    'authentication',
    'routes',
    'gateway',
    'analytics',
    'client_auth',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'gateway.middleware.GatewayAuthenticationMiddleware',
]

ROOT_URLCONF = 'gateway_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'gateway_project.wsgi.application'

MYSQL_DB = os.getenv('MYSQL_DATABASE', 'gateway_db')
MYSQL_USER = os.getenv('MYSQL_USER', 'gateway')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD', 'gateway')
MYSQL_HOST = os.getenv('MYSQL_HOST', 'localhost')
MYSQL_PORT = int(os.getenv('MYSQL_PORT', '3306'))
MYSQL_CLUSTER_ENABLED = os.getenv('MYSQL_CLUSTER_ENABLED', 'False').lower() == 'true'
MYSQL_READ_PORT = int(os.getenv('MYSQL_READ_PORT', '6447'))
USE_SQLITE = os.getenv('USE_SQLITE', 'True').lower() == 'true'

if USE_SQLITE:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.mysql',
            'NAME': MYSQL_DB,
            'USER': MYSQL_USER,
            'PASSWORD': MYSQL_PASSWORD,
            'HOST': MYSQL_HOST,
            'PORT': MYSQL_PORT,
            'OPTIONS': {'charset': 'utf8mb4'},
        }
    }
    if MYSQL_CLUSTER_ENABLED:
        DATABASES['replica'] = {
            **DATABASES['default'],
            'PORT': MYSQL_READ_PORT,
            'CONN_MAX_AGE': 60,
        }
        DATABASES['default']['CONN_MAX_AGE'] = 60

# Always keep SQLite unit tests on the single default connection.
DATABASE_ROUTERS = ['gateway_project.db_router.PrimaryReplicaRouter']

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

AUTH_USER_MODEL = 'users.User'
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': False,
    'BLACKLIST_AFTER_ROTATION': True,
}

# A separate signing domain from the platform-staff SimpleJWT tokens.
# Configure CLIENT_JWT_SIGNING_KEY separately and identically on all gateway replicas.
CLIENT_JWT_SIGNING_KEY = os.getenv('CLIENT_JWT_SIGNING_KEY') or (SECRET_KEY + ':client-auth')
CLIENT_JWT_ISSUER = 'servlink-client-auth'
CLIENT_JWT_AUDIENCE = 'servlink-gateway'
CLIENT_ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
CLIENT_REFRESH_TOKEN_LIFETIME = timedelta(days=7)

REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')
# Docker Compose uses Redis Cluster; stand-alone remains supported for local tests.
REDIS_CLUSTER_ENABLED = os.getenv('REDIS_CLUSTER_ENABLED', 'False').lower() == 'true'
REDIS_CLUSTER_URL = os.getenv('REDIS_CLUSTER_URL', 'redis://redis-node-1:6379')
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'gateway-local-cache',
    }
}

# Operator-approved VIRTUAL target origins for preview mode; no network calls are made.
_DEFAULT_BACKEND_ORIGINS = ','.join(f'http://{name}:8000' for name in (
    'fashion-user-service', 'fashion-product-service', 'fashion-order-service',
    'banking-user-service', 'banking-account-service', 'banking-payment-service',
    'healthcare-doctor-service', 'healthcare-appointment-service', 'healthcare-patient-service',
    'banking-transaction-service',
))
GATEWAY_ALLOWED_BACKEND_ORIGINS = [
    value.strip() for value in os.getenv('GATEWAY_ALLOWED_BACKEND_ORIGINS', _DEFAULT_BACKEND_ORIGINS).split(',')
    if value.strip()
]
