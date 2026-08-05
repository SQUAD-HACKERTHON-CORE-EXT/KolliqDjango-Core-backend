import environ
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(DEBUG=(bool, False))
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('SECRET_KEY')
DEBUG = env('DEBUG')
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost').split(',')

DJANGO_APPS = [
    'unfold',
    'unfold.contrib.filters',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

THIRD_PARTY_APPS = [
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'rest_framework.authtoken',
    'drf_spectacular',
    'corsheaders',
    'django_celery_beat',
]

LOCAL_APPS = [
    'apps.common',
    'apps.users',
    'apps.wallets',
    'apps.jobs',
    'apps.payments',
    'apps.scoring',
    'apps.financial_services',
    'apps.partner',
    'apps.marketplace',
    'services',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'kolliq.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
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

WSGI_APPLICATION = 'kolliq.wsgi.application'

# Database
DATABASES = {
    'default': {
        **env.db('DATABASE_URL', default='sqlite:///db.sqlite3'),
        'CONN_MAX_AGE': 0,
        'OPTIONS': {
            'connect_timeout': 10,
            'keepalives': 1,
            'keepalives_idle': 60,
            'keepalives_interval': 10,
            'keepalives_count': 5,
        }
    }
}

REDIS_URL = env('REDIS_URL', default='redis://localhost:6379/0')

# Cache / Redis
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': REDIS_URL,
        'OPTIONS': {
            'socket_connect_timeout': 5,
            'socket_timeout': 5,
            'retry_on_timeout': True,
        }
    }
}


# Celery
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'

# Auth
AUTH_USER_MODEL = 'users.User'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.TokenAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.AllowAny',
    ),
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
    ),
    'EXCEPTION_HANDLER': 'kolliq.utils.custom_exception_handler',
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

from datetime import timedelta
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=7),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
    'ROTATE_REFRESH_TOKENS': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Kolliq API',
    'DESCRIPTION': 'Kolliq - Micro-work & Gig Economy Platform API',
    'VERSION': '1.0.0',
    'SERVE_PERMISSIONS': ['rest_framework.permissions.AllowAny'],
    'RETRIEVE_TRAILING_SLASH': False,
    'SCHEMA_PATH_PREFIX': r'/api/',
    'CONTACT': {
        'name': 'Kolliq Support',
        'email': 'support@kolliq.com',
    },
    'LICENSE': {
        'name': 'Proprietary',
        'url': 'https://kolliq.com/license',
    },
    'AUTHENTICATION_FLOWS': {
        'jwt': {
            'type': 'http',
            'scheme': 'bearer',
            'bearerFormat': 'JWT',
            'description': 'JWT token authentication. Obtain token via /api/users/token/ endpoint.',
        },
    },
    'SECURITY': [
        {'jwt': []},
    ],
    'SECURITY_SCHEMES': {
        'jwt': {
            'type': 'http',
            'scheme': 'bearer',
            'bearerFormat': 'JWT',
            'description': 'JWT token authentication. Obtain token via /api/users/token/ endpoint.',
        },
    },
    'SORT_OPERATION_PARAMETERS': True,
    'SORT_SECURITY_SCHEMES': True,
    'ENUM_ADD_EXPLICIT_BLANK_NULL_CHOICE': True,
    'POSTPROCESSING_HOOKS': [],
    'TAGS_SORTER': None,
    'OPERATION_SORTER': None,

}

if DEBUG:
    CORS_ALLOW_ALL_ORIGINS = True
else:
    CORS_ALLOW_ALL_ORIGINS = False
    CORS_ALLOWED_ORIGINS = env.list('CORS_ALLOWED_ORIGINS', default=[])
 


CORS_ALLOW_HEADERS = [
    'accept',
    'accept-encoding',
    'authorization',
    'content-type',
    'origin',
    'x-internal-secret',     # Node → Django internal webhook auth
    'x-partner-secret',      # Partner API auth
    'x-squad-signature',     # Squad webhook signature
    'x-requested-with',
]

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Squad
PAYSTACK_SECRET_KEY     = os.environ.get('PAYSTACK_SECRET_KEY', '')
PAYSTACK_BASE_URL       = os.environ.get('PAYSTACK_BASE_URL', 'https://api.paystack.co')
PAYSTACK_DVA_BANK       = os.environ.get('PAYSTACK_DVA_BANK', 'test-bank')

KOLLIQ_ESCROW_DVA_NUMBER = os.environ.get('KOLLIQ_ESCROW_DVA_NUMBER', '')
KOLLIQ_ESCROW_DVA_BANK   = os.environ.get('KOLLIQ_ESCROW_DVA_BANK', 'test-bank')  # display name for employers
KOLLIQ_ESCROW_DVA_NAME   = os.environ.get('KOLLIQ_ESCROW_DVA_NAME', 'Kolliq Escrow')
# Africa's Talking
AT_USERNAME = env('AT_USERNAME', default='sandbox')
AT_API_KEY = env('AT_API_KEY', default='')
AT_SENDER_ID = env('AT_SENDER_ID', default='KOLLIQ')

# Platform Config
PLATFORM_FEE_PERCENT = env.int('PLATFORM_FEE_PERCENT', default=5)
ARISE_WALLET_ID = env.int('ARISE_WALLET_ID', default=1)
DEMO_FLOAT_WALLET_ID = env.int('DEMO_FLOAT_WALLET_ID', default=2)

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Lagos'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── System Virtual Accounts (created once at deploy, never change) ──────────



# ── Demo Float for simulated loans/insurance disbursements ──────────────────
DEMO_FLOAT_WALLET_ID = env.int('DEMO_FLOAT_WALLET_ID', default=2)
DEMO_FLOAT_INITIAL_BALANCE = env.int('DEMO_FLOAT_INITIAL_BALANCE', default=1000000)

# ── Financial Services Config ───────────────────────────────────────────────
LOAN_SCORE_THRESHOLD = env.int('LOAN_SCORE_THRESHOLD', default=50)
INSURANCE_SCORE_THRESHOLD = env.int('INSURANCE_SCORE_THRESHOLD', default=70)
SAVINGS_SCORE_THRESHOLD = env.int('SAVINGS_SCORE_THRESHOLD', default=20)
LOAN_INTEREST_RATE_MONTHLY = env.float('LOAN_INTEREST_RATE_MONTHLY', default=5.0)
INSURANCE_DAILY_PREMIUM = env.int('INSURANCE_DAILY_PREMIUM', default=200)   # naira
INSURANCE_COVERAGE_LIMIT = env.int('INSURANCE_COVERAGE_LIMIT', default=50000)
SAVINGS_ANNUAL_INTEREST_RATE = env.float('SAVINGS_ANNUAL_INTEREST_RATE', default=5.0)

# ── Partner API ─────────────────────────────────────────────────────────────
PARTNER_API_SECRET = env('PARTNER_API_SECRET', default='change-me-in-production')
FINANCIAL_PARTNER_MODE = env('FINANCIAL_PARTNER_MODE', default='simulated')
# When a real microfinance partner connects: switch to 'live'
# and swap disbursement logic in financial_services/views.py

# ── Celery Beat Schedule ─────────────────────────────────────────────────────
from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    # Daily savings interest — runs at midnight Lagos time
    'accrue-savings-interest-daily': {
        'task': 'apps.financial_services.tasks.accrue_daily_savings_interest',
        'schedule': crontab(hour=0, minute=0),
    },
    # Insurance premium deduction — runs at 6am daily
    'deduct-insurance-premiums-daily': {
        'task': 'apps.financial_services.tasks.deduct_daily_insurance_premiums',
        'schedule': crontab(hour=6, minute=0),
    },
    # Loan repayment check — runs every Monday at 8am
    'check-loan-repayments-weekly': {
        'task': 'apps.financial_services.tasks.process_weekly_loan_repayments',
        'schedule': crontab(hour=8, minute=0, day_of_week=1),
    },
    # Fraud detection sweep — runs every hour
    'fraud-sweep-hourly': {
        'task': 'apps.financial_services.tasks.fraud_detection_sweep',
        'schedule': crontab(minute=0),
    },
    # Marketplace: expire old listings at 1am
    'expire-old-listings-daily': {
        'task': 'apps.marketplace.tasks.expire_old_listings',
        'schedule': crontab(hour=1, minute=0),
    },
    # Marketplace: fraud sweep every hour
    'flag-suspicious-listings-hourly': {
        'task': 'apps.marketplace.tasks.flag_suspicious_listings',
        'schedule': crontab(minute=30),  # offset from main fraud sweep
    },
    # ── Reconciliation — runs every 6 hours ───────────────────────────────────
    'reconcile-merchant-account': {
        'task':     'apps.payments.tasks.reconciliation.reconcile_merchant_account',
        'schedule': crontab(minute=0, hour='*/6'),   # 00:00, 06:00, 12:00, 18:00
        'options': {
            'expires': 60 * 60,   # task expires after 1 hour if not picked up
        },
     'reconcile-withdrawals': {
            'task': 'apps.wallets.payout_tasks.reconcile_pending_withdrawals',
            'schedule': crontab(minute='*/30'),
        }
    },

    # ── Daily summary report — midnight Lagos time (UTC+1) ────────────────────
    'reconcile-daily-summary': {
        'task':     'apps.payments.tasks.reconciliation.reconcile_merchant_account',
        'schedule': crontab(minute=0, hour=23),      # 11pm UTC = midnight WAT
        'options': {
            'expires': 60 * 60,
        },
    },
}

CSRF_TRUSTED_ORIGINS = env.list(
    'CORS_ALLOWED_ORIGINS',
    default=[
        'http://localhost:8040',
        'http://localhost:3000',
        'https://*.ngrok-free.dev',
    ]
)
# ── Paste into your settings.py (or settings/base.py) ────────────────────────
"""
Reconciliation Settings
========================
Add these to your Django settings file.
"""

from celery.schedules import crontab

# ── Reconciliation thresholds ─────────────────────────────────────────────────

# Fire alert if drift exceeds this naira amount
RECONCILIATION_DRIFT_THRESHOLD = 5000.00   # ₦5,000

# Fire alert if drift exceeds this % of expected balance
RECONCILIATION_DRIFT_PERCENT   = 2.0       # 2%

# Who to email when drift is critical
RECONCILIATION_ALERT_EMAILS = [
    'xpsiders@gmail.com',   # replace with your real addresses
    'tech@kolliq.app',
]

# Slack incoming webhook URL (optional — set to None to disable)
SLACK_ALERT_WEBHOOK_URL = None  # e.g. 'https://hooks.slack.com/services/xxx/yyy/zzz'

EMAIL_BACKEND       = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST          = 'smtp.gmail.com'
EMAIL_PORT          = 587
EMAIL_USE_TLS       = True
EMAIL_HOST_USER     = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL  = f'Kolliq Alerts <{EMAIL_HOST_USER}>'

ADMIN_NOTIFY_EMAILS = os.environ.get('ADMIN_NOTIFY_EMAILS', '').split(',') if os.environ.get('ADMIN_NOTIFY_EMAILS') else []


