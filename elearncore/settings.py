import os
import sys
from pathlib import Path
from datetime import timedelta
from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

from dotenv import load_dotenv

load_dotenv()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: list[str] | None = None) -> list[str]:
    value = os.getenv(name)
    if value is None:
        return list(default or [])
    return [item.strip() for item in value.split(",") if item.strip()]


# SECURITY WARNING: keep the secret key used in production secret!
ENVIRONMENT = os.getenv('ENVIRONMENT', 'LOCAL').upper()
IS_PRODUCTION = ENVIRONMENT in {"LIVE", "PRODUCTION", "PROD"}

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    if IS_PRODUCTION:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY is required in production.")
    SECRET_KEY = "local-development-only-change-me"

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env_bool('DJANGO_DEBUG', default=not IS_PRODUCTION)

ALLOWED_HOSTS = env_list('DJANGO_ALLOWED_HOSTS', ['localhost', '127.0.0.1', '[::1]'] if not IS_PRODUCTION else [])
if IS_PRODUCTION and not ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS is required in production.")

# CSRF trusted origins for HTTPS (required when behind a proxy)
if IS_PRODUCTION:
    CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS', [
        "https://elearnapi.afrilearntech.com",
        "https://elapi.afrilearntech.com",
        "https://digitallearningapi.moe.gov.lr",
        "https://digitallearning.moe.gov.lr",
        "https://*.afrilearntech.com",
    ])

SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', default=IS_PRODUCTION)
SESSION_COOKIE_SECURE = env_bool('SESSION_COOKIE_SECURE', default=IS_PRODUCTION)
CSRF_COOKIE_SECURE = env_bool('CSRF_COOKIE_SECURE', default=IS_PRODUCTION)
SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', '31536000' if IS_PRODUCTION else '0'))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool('SECURE_HSTS_INCLUDE_SUBDOMAINS', default=IS_PRODUCTION)
SECURE_HSTS_PRELOAD = env_bool('SECURE_HSTS_PRELOAD', default=IS_PRODUCTION)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = 'DENY'
if env_bool('TRUST_PROXY_SSL_HEADER', default=IS_PRODUCTION):
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')


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
if os.getenv('DO_SPACES_BUCKET') and IS_PRODUCTION:
    DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'

    AWS_ACCESS_KEY_ID = os.getenv('DO_SPACES_KEY') or os.getenv('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY = os.getenv('DO_SPACES_SECRET') or os.getenv('AWS_SECRET_ACCESS_KEY')
    AWS_STORAGE_BUCKET_NAME = os.getenv('DO_SPACES_BUCKET')
    AWS_S3_REGION_NAME = os.getenv('DO_SPACES_REGION', 'nyc3')
    AWS_S3_ENDPOINT_URL = os.getenv('DO_SPACES_ENDPOINT', f'https://{AWS_S3_REGION_NAME}.digitaloceanspaces.com')
    AWS_S3_CUSTOM_DOMAIN = os.getenv('DO_SPACES_CUSTOM_DOMAIN')  # optional CDN/custom domain

    # Keep uploads private. API serializers return short-lived signed URLs.
    AWS_DEFAULT_ACL = None
    AWS_QUERYSTRING_AUTH = True
    AWS_QUERYSTRING_EXPIRE = int(os.getenv('AWS_QUERYSTRING_EXPIRE', '300'))
    AWS_S3_FILE_OVERWRITE = False
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
        'api.authentication.PasswordChangeAwareTokenAuthentication',
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

# Celery settings (defaults use local Redis; tests use an in-memory broker).
RUNNING_TESTS = 'test' in sys.argv
if RUNNING_TESTS:
    # Keep production hashers unchanged while avoiding costly fixture hashing in CI.
    PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
CELERY_BROKER_URL = os.getenv(
    'CELERY_BROKER_URL',
    'memory://' if RUNNING_TESTS else os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
)
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'django-db')
CELERY_CACHE_BACKEND = 'django-cache'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_PUBLISH_RETRY = True
CELERY_TASK_PUBLISH_RETRY_POLICY = {
    'max_retries': 3,
    'interval_start': 0,
    'interval_step': 0.2,
    'interval_max': 1,
}
CELERY_BROKER_TRANSPORT_OPTIONS = {
    'socket_connect_timeout': float(os.getenv('CELERY_BROKER_CONNECT_TIMEOUT', '2')),
    'socket_timeout': float(os.getenv('CELERY_BROKER_SOCKET_TIMEOUT', '5')),
}

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
    'ENUM_NAME_OVERRIDES': {
        'UserRoleEnum': 'elearncore.sysutils.constants.UserRole',
        'StudentLevelEnum': 'elearncore.sysutils.constants.StudentLevel',
        'ContentStatusEnum': 'elearncore.sysutils.constants.Status',
        'ContentTypeEnum': 'elearncore.sysutils.constants.ContentType',
        'QuestionTypeEnum': 'elearncore.sysutils.constants.QType',
        'GameTypeEnum': 'elearncore.sysutils.constants.GameType',
        'AssessmentTypeEnum': 'elearncore.sysutils.constants.AssessmentType',
        'MonthEnum': [
            (1, 'January'), (2, 'February'), (3, 'March'),
            (4, 'April'), (5, 'May'), (6, 'June'),
            (7, 'July'), (8, 'August'), (9, 'September'),
            (10, 'October'), (11, 'November'), (12, 'December'),
        ],
    },
}

# django cors headers settings
CORS_ALLOW_ALL_ORIGINS = env_bool('CORS_ALLOW_ALL_ORIGINS', default=False)
CORS_ALLOWED_ORIGINS = env_list('CORS_ALLOWED_ORIGINS', [])
if IS_PRODUCTION and CORS_ALLOW_ALL_ORIGINS:
    raise ImproperlyConfigured("CORS_ALLOW_ALL_ORIGINS cannot be enabled in production.")

API_DOCS_ENABLED = env_bool('API_DOCS_ENABLED', default=DEBUG)

MAX_CSV_UPLOAD_BYTES = int(os.getenv('MAX_CSV_UPLOAD_BYTES', str(2 * 1024 * 1024)))
MAX_CSV_ROWS = int(os.getenv('MAX_CSV_ROWS', '5000'))
MAX_IMAGE_UPLOAD_BYTES = int(os.getenv('MAX_IMAGE_UPLOAD_BYTES', str(5 * 1024 * 1024)))
MAX_DOCUMENT_UPLOAD_BYTES = int(os.getenv('MAX_DOCUMENT_UPLOAD_BYTES', str(100 * 1024 * 1024)))
UPLOAD_SCANNER = os.getenv('UPLOAD_SCANNER', '')
REQUIRE_UPLOAD_SCAN = env_bool('REQUIRE_UPLOAD_SCAN', default=False)
AI_GENERATION_RATE = os.getenv('AI_GENERATION_RATE', '10/hour')
TEMPORARY_PASSWORD_TTL_HOURS = int(os.getenv('TEMPORARY_PASSWORD_TTL_HOURS', '24'))
FRONTEND_BASE_URL = os.getenv(
    'FRONTEND_BASE_URL',
    'https://digitallearning.moe.gov.lr' if IS_PRODUCTION else 'http://localhost:3000',
).rstrip('/')
SELF_SERVICE_REGISTRATION_ENABLED = env_bool('SELF_SERVICE_REGISTRATION_ENABLED', default=False)

# NOTIFICATION SETTINGS
# Resend is the production email provider. Local environments without a key log
# messages to the console instead of silently attempting an unconfigured SMTP connection.
RESEND_API_KEY = os.getenv('RESEND_API_KEY', '').strip()
ANYMAIL = {'RESEND_API_KEY': RESEND_API_KEY}
EMAIL_BACKEND = os.getenv('EMAIL_BACKEND') or (
    'anymail.backends.resend.EmailBackend'
    if RESEND_API_KEY
    else 'django.core.mail.backends.console.EmailBackend'
)
if IS_PRODUCTION and not RESEND_API_KEY:
    raise ImproperlyConfigured('RESEND_API_KEY is required in production.')
DEFAULT_FROM_EMAIL = os.getenv(
    'DEFAULT_FROM_EMAIL',
    os.getenv(
        'DEFAULT_FROM_MAIL',
        'LR Digital Leanrning Platform <onboarding@resend.dev>',
    ),
).strip()
 
# KEYS
SENDER_ID = os.getenv('SMS_SENDER_ID') # 11 characters max

# Get the key from .env file
ARKESEL_API_KEY = os.getenv('ARKESEL_SMS_API_KEY')

