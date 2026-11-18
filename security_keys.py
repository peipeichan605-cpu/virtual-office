"""Separate owner authorization, feed names and durable integrity fingerprints.

Legacy integrity keys verify persisted receipts only. They must never authorize
an HTTP request or a new one-time approval. Migration is explicit and idempotent.
"""
import copy
import hashlib
import hmac
import re
import secrets

KEY = re.compile(r'[A-Za-z0-9_-]{22,64}\Z')


def owner_key(config):
    return config.get('owner_key', config['topic'])


def matches_owner_key(config, provided):
    """Untrusted HTTP text must fail closed, including non-ASCII header bytes."""
    if not isinstance(provided, str) or len(provided) > 64:
        return False
    try:
        return hmac.compare_digest(provided.encode('ascii'), owner_key(config).encode('ascii'))
    except (UnicodeError, AttributeError, KeyError, TypeError):
        return False


def integrity_key(config):
    return config.get('internal_hmac_key', config['topic'])


def validate_security_keys(config):
    present = [key in config for key in ('owner_key', 'internal_hmac_key')]
    if any(present) and not all(present):
        raise ValueError('Owner and integrity keys must be migrated together')
    for name in ('owner_key', 'internal_hmac_key'):
        if name in config and (not isinstance(config[name], str) or not KEY.fullmatch(config[name])):
            raise ValueError('Invalid private security key')
    old = config.get('legacy_integrity_keys', [])
    if (not isinstance(old, list) or len(old) > 4
            or any(not isinstance(value, str) or not KEY.fullmatch(value) for value in old)
            or len(set(old)) != len(old)):
        raise ValueError('Invalid legacy integrity keys')
    if old and not all(present):
        raise ValueError('Legacy integrity keys require an explicit migration')
    if all(present) and len({config['topic'], config['owner_key'], config['internal_hmac_key']}) != 3:
        raise ValueError('Feed, owner and integrity keys must be distinct')
    if all(present) and any(value in {config['owner_key'], config['internal_hmac_key']} for value in old):
        raise ValueError('Current security keys cannot be legacy integrity keys')


def upgraded_security(config):
    try:
        validate_security_keys(config)
        return all(key in config for key in ('owner_key', 'internal_hmac_key'))
    except (ValueError, TypeError, KeyError):
        return False


def upgrade_security(config):
    """Return a migrated copy. Caller saves while Office is safely stopped.

    The old topic remains a feed identifier and legacy receipt-verification key;
    it loses owner authority. Existing dedupe records remain verifiable without
    retaining command bodies or replaying any native action.
    """
    validate_security_keys(config)
    result = copy.deepcopy(config)
    if not upgraded_security(result):
        previous = integrity_key(result)
        result['owner_key'] = secrets.token_urlsafe(32)
        result['internal_hmac_key'] = secrets.token_urlsafe(32)
        result['legacy_integrity_keys'] = [previous]
    result['feed'] = None
    validate_security_keys(result)
    return result


def integrity_fingerprint(config, payload):
    return hmac.new(integrity_key(config).encode(), payload, hashlib.sha256).hexdigest()


def integrity_candidates(config, payload):
    keys = (integrity_key(config), *config.get('legacy_integrity_keys', ()))
    return tuple(dict.fromkeys(hmac.new(key.encode(), payload, hashlib.sha256).hexdigest() for key in keys))


def matches_integrity(config, payload, expected):
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
        return False
    return any(hmac.compare_digest(value, expected) for value in integrity_candidates(config, payload))


def security_secrets(config):
    return tuple(dict.fromkeys(value for value in
        (config.get('topic'), config.get('owner_key'), config.get('internal_hmac_key'),
         *config.get('legacy_integrity_keys', ())) if isinstance(value, str) and value))
