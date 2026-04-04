import os
from pathlib import Path
from datetime import timedelta

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

from dotenv import load_dotenv

load_dotenv()


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY')

ENVIRONMENT = os.getenv('ENVIRONMENT', 'LOCAL').upper()

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = ["*"]

# CSRF trusted origins for HTTPS (required when behind a proxy)
if ENVIRONMENT in ["LIVE", "PRODUCTION", "PROD"]:
    CSRF_TRUSTED_ORIGINS = [
        "https://elearnapi.afrilearntech.com",
        "https://elapi.afrilearntech.com",
        "https://digitallearningapi.moe.gov.lr",
        "https://digitallearning.moe.gov.lr",
        "https://*.afrilearntech.com",
    ]


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # internal apps
    'accounts.apps.AccountsConfig',
    'api.apps.ApiConfig',
    'content.apps.ContentConfig',
    'forum.apps.ForumConfig',
    'agentic.apps.AgenticConfig',
    'messsaging.apps.MesssagingConfig',

    # Third party apps
    'corsheaders',
    'rest_framework',
    'knox',
    'drf_spectacular',
    'django_filters',
    'storages',
    'django_celery_results',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'elearncore.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'elearncore.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

if ENVIRONMENT in ["LIVE", "PRODUCTION", "PROD", "BOX", "AFRIBOX"]:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.getenv('DB_NAME', ''),
            'USER': os.getenv('DB_USER', ''),
            'PASSWORD': os.getenv('DB_PASSWORD', ''),
            'HOST': os.getenv('DB_HOST', ''),
            'PORT': os.getenv('DB_PORT', '5432'),
            # Reuse DB connections between requests to reduce connection churn.
            'CONN_MAX_AGE': int(os.getenv('DB_CONN_MAX_AGE', '300')),
            'CONN_HEALTH_CHECKS': True,
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# custom user model
AUTH_USER_MODEL = 'accounts.User'


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles/'

STATICFILES_DIRS = [
    BASE_DIR / "static",
]

MEDIA_URL = '/assets/'
MEDIA_ROOT = BASE_DIR / "assets"

# Use Spaces if DO_SPACES_BUCKET is provided; otherwise fall back to local MEDIA settings above.
ENVIRONMENT = os.getenv('ENVIRONMENT', 'LOCAL').upper()
if os.getenv('DO_SPACES_BUCKET') and ENVIRONMENT in ["LIVE", "PRODUCTION", "PROD"]:
    DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'

    AWS_ACCESS_KEY_ID = os.getenv('DO_SPACES_KEY') or os.getenv('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY = os.getenv('DO_SPACES_SECRET') or os.getenv('AWS_SECRET_ACCESS_KEY')
    AWS_STORAGE_BUCKET_NAME = os.getenv('DO_SPACES_BUCKET')
    AWS_S3_REGION_NAME = os.getenv('DO_SPACES_REGION', 'nyc3')
    AWS_S3_ENDPOINT_URL = os.getenv('DO_SPACES_ENDPOINT', f'https://{AWS_S3_REGION_NAME}.digitaloceanspaces.com')
    AWS_S3_CUSTOM_DOMAIN = os.getenv('DO_SPACES_CUSTOM_DOMAIN')  # optional CDN/custom domain

    # Public media files by default; change to None/'' for private files
    AWS_DEFAULT_ACL = 'public-read'
    AWS_S3_OBJECT_PARAMETERS = {
        'CacheControl': 'max-age=86400',
    }

    # MEDIA_URL: prefer custom CDN domain if provided, otherwise use Spaces endpoint + bucket
    if AWS_S3_CUSTOM_DOMAIN:
        MEDIA_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/'
    else:
        MEDIA_URL = f'{AWS_S3_ENDPOINT_URL}/{AWS_STORAGE_BUCKET_NAME}/'


# Caching
if os.getenv('REDIS_URL') and ENVIRONMENT in ["LIVE", "PRODUCTION", "PROD", "BOX", "AFRIBOX"]:
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': os.getenv('REDIS_URL'),
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
                'PASSWORD': os.getenv('REDIS_PASSWORD', None),
                'CONNECTION_POOL_KWARGS': {'max_connections': 50, 'retry_on_timeout': True},
            },
            'KEY_PREFIX': 'elearncore',
            'TIMEOUT': int(os.getenv('CACHE_DEFAULT_TIMEOUT', '300')),
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'elearncore-local',
            'TIMEOUT': 300,
        }
    }

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Django REST Framework Configuration
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'knox.auth.TokenAuthentication',
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_PAGINATION_CLASS': 'api.pagination.StandardResultsSetPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}
# knox - expire tokens after 3 hours of inactivity (sliding window)
REST_KNOX = {
    'TOKEN_TTL': timedelta(hours=3),
    'AUTO_REFRESH': True,
}

# Celery settings (defaults use local Redis)
CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', os.getenv('REDIS_URL', 'redis://localhost:6379/0'))
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'django-db')
CELERY_CACHE_BACKEND = 'django-cache'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE

# DRF Spectacular Configuration
SPECTACULAR_SETTINGS = {
    'TITLE': 'Liberia eLearn API',
    'DESCRIPTION': 'API Documentation for Liberia eLearn',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': True,
    'COMPONENT_SPLIT_REQUEST': True,
    'SECURITY': [
        {'TokenAuth': []},
    ],
}

# django cors headers settings
CORS_ALLOW_ALL_ORIGINS = True

# NOTIFICATION SETTINGS
# email settings
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.gmail.com'
EMAIL_PORT = 587
EMAIL_HOST_USER = ''
EMAIL_HOST_PASSWORD = ''
EMAIL_USE_TLS = True
EMAIL_USE_SSL = False
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_MAIL')
 
# KEYS
SENDER_ID = os.getenv('SMS_SENDER_ID') # 11 characters max

# Get the key from .env file
ARKESEL_API_KEY = os.getenv('ARKESEL_SMS_API_KEY')

