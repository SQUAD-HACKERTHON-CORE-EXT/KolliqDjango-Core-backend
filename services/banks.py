"""
services/banks.py — Live Nigerian Bank List
============================================
Fetches from Paystack GET /bank, caches in Redis for 24 hours.
Falls back to the hardcoded list if Paystack is unreachable.

WHY NOT HARDCODE:
  - New banks (OPay, Moniepoint, etc.) are added regularly
  - Bank codes occasionally change
  - Closed banks should disappear from the list
  - Paystack sometimes adds DVA-capable banks
  - Your frontend dropdown is only as good as this list

USAGE:
  from services.banks import get_bank_list, get_bank_name, get_bank_by_code

  # In NigerianBankListView:
  banks = get_bank_list()  # list of { name, code }

  # In BankAccountSaveView:
  name = get_bank_name('058')  # → 'Guaranty Trust Bank'
"""

import logging
from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_KEY        = 'paystack:bank_list'
CACHE_KEY_MAP    = 'paystack:bank_code_map'
CACHE_TTL        = 60 * 60 * 24   # 24 hours

# Static fallback — used only if Paystack API is unreachable
_STATIC_BANKS = [
    {'name': 'Access Bank',              'code': '044'},
    {'name': 'Citibank Nigeria',         'code': '023'},
    {'name': 'Ecobank Nigeria',          'code': '050'},
    {'name': 'Fidelity Bank',            'code': '070'},
    {'name': 'First Bank of Nigeria',    'code': '011'},
    {'name': 'First City Monument Bank', 'code': '214'},
    {'name': 'Globus Bank',              'code': '00103'},
    {'name': 'Guaranty Trust Bank',      'code': '058'},
    {'name': 'Heritage Bank',            'code': '030'},
    {'name': 'Keystone Bank',            'code': '082'},
    {'name': 'Kuda Bank',                'code': '50211'},
    {'name': 'Moniepoint MFB',           'code': '50515'},
    {'name': 'OPay',                     'code': '100004'},
    {'name': 'Palmpay',                  'code': '100033'},
    {'name': 'Polaris Bank',             'code': '076'},
    {'name': 'Providus Bank',            'code': '101'},
    {'name': 'Stanbic IBTC Bank',        'code': '221'},
    {'name': 'Standard Chartered Bank',  'code': '068'},
    {'name': 'Sterling Bank',            'code': '232'},
    {'name': 'Titan Trust Bank',         'code': '102'},
    {'name': 'Union Bank of Nigeria',    'code': '032'},
    {'name': 'United Bank for Africa',   'code': '033'},
    {'name': 'Unity Bank',               'code': '215'},
    {'name': 'VFD Microfinance Bank',    'code': '566'},
    {'name': 'Wema Bank',                'code': '035'},
    {'name': 'Zenith Bank',              'code': '057'},
]


def get_bank_list(force_refresh: bool = False) -> list[dict]:
    """
    Returns list of { name, code } dicts.
    Tries cache first, then Paystack API, then static fallback.
    """
    if not force_refresh:
        cached = cache.get(CACHE_KEY)
        if cached:
            return cached

    try:
        from services.paystack import PaystackService
        svc   = PaystackService()
        raw   = svc.list_banks()            # GET /bank?country=nigeria&perPage=200
        banks = [
            {'name': b['name'], 'code': b['code']}
            for b in raw
            if b.get('code') and b.get('name') and b.get('active', True)
        ]
        banks.sort(key=lambda b: b['name'])

        if banks:
            cache.set(CACHE_KEY, banks, CACHE_TTL)
            # Also cache the code→name map
            code_map = {b['code']: b['name'] for b in banks}
            cache.set(CACHE_KEY_MAP, code_map, CACHE_TTL)
            logger.info(f'Bank list refreshed from Paystack: {len(banks)} banks cached')
            return banks

    except Exception as e:
        logger.warning(f'get_bank_list: Paystack unavailable ({e}), using static fallback')

    return _STATIC_BANKS


def get_bank_name(bank_code: str) -> str:
    """
    Return bank display name for a given code.
    Tries cache first, then live list, then static.
    """
    code_map = cache.get(CACHE_KEY_MAP)
    if code_map:
        return code_map.get(bank_code, bank_code)

    # Cache miss — rebuild from bank list (which itself may be cached)
    banks = get_bank_list()
    code_map = {b['code']: b['name'] for b in banks}
    return code_map.get(bank_code, bank_code)


def get_bank_by_code(bank_code: str) -> dict | None:
    """Return full { name, code } dict for a given code, or None."""
    banks = get_bank_list()
    return next((b for b in banks if b['code'] == bank_code), None)


def get_dva_providers() -> list[dict]:
    """
    Returns banks currently supporting Paystack DVA.
    Cached for 6 hours — providers change less often than the full bank list.
    """
    cache_key = 'paystack:dva_providers'
    cached = cache.get(cache_key)
    if cached:
        return cached

    try:
        from services.paystack import PaystackService
        providers = PaystackService().get_dva_providers()
        if providers:
            cache.set(cache_key, providers, 60 * 60 * 6)
            return providers
    except Exception as e:
        logger.warning(f'get_dva_providers: Paystack unavailable ({e})')

    # Hardcoded fallback — these are the known stable providers
    return [
        {'name': 'Wema Bank',        'slug': 'wema-bank'},
        {'name': 'Titan Trust Bank', 'slug': 'titan-trust'},
        {'name': 'Providus Bank',    'slug': 'providus'},
        {'name': 'Sterling Bank',    'slug': 'sterling'},
    ]


def validate_dva_bank_setting():
    """
    Called at app startup (AppConfig.ready) to confirm PAYSTACK_DVA_BANK
    is in the current list of supported DVA providers.
    Logs a warning if not — doesn't crash startup.
    """
    from django.conf import settings
    configured = getattr(settings, 'PAYSTACK_DVA_BANK', 'wema-bank')
    providers  = get_dva_providers()
    slugs      = [p.get('slug', p.get('provider_slug', '')) for p in providers]

    if configured not in slugs:
        logger.warning(
            f'PAYSTACK_DVA_BANK="{configured}" is not in current DVA providers: {slugs}. '
            f'Update settings.PAYSTACK_DVA_BANK to one of: {slugs}'
        )
    else:
        logger.info(f'DVA bank validated: {configured} ✓')