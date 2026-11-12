"""User-session gettext catalogs and localization of service-owned messages."""
import gettext
import os
from pathlib import Path
import re
from functools import lru_cache
from string import Formatter

DOMAIN = 'gpu-dock-manager'
LOCALES = Path(__file__).with_name('locales')
TEXT_FIELDS = {'message', 'reason', 'restore_reason', 'last_error', 'connection_error',
               'blockers', 'description', 'summary', 'title'}
NESTED_MESSAGES = {'Current state · {p0}', '4070 driver switch failed: {p0}{p1}',
                   '{p0}; the original layout was restored.', '{p0}; layout recovery failed: {p1}',
                   '{p0}; failed to restore WirePlumber: {p1}', '{p0}; failed to restore nvidia-powerd: {p1}'}


def select_language(environ=None):
    env = os.environ if environ is None else environ
    explicit = env.get('GPU_DOCK_LANGUAGE')
    category = env.get('LC_ALL') or env.get('LC_MESSAGES') or env.get('LANG') or 'en'
    raw = explicit or env.get('LANGUAGE', '').split(':')[0] or category
    value = raw.split('.')[0].split('@')[0].replace('-', '_').lower()
    if value == 'zh' or value.startswith(('zh_cn', 'zh_sg', 'zh_hans')):
        return 'zh_CN'
    if value.startswith(('zh_tw', 'zh_hk', 'zh_mo', 'zh_hant')):
        return 'zh_TW'
    if value == 'en' or value.startswith('en_'):
        return 'en'
    # Contributors may add another catalog without changing application code.
    for candidate in (value, value.split('_')[0]):
        for path in LOCALES.glob('*/LC_MESSAGES/' + DOMAIN + '.mo'):
            if path.parent.parent.name.lower() == candidate:
                return path.parent.parent.name
    return 'en'


@lru_cache(maxsize=None)
def catalog(language):
    return gettext.translation(DOMAIN, localedir=LOCALES, languages=[language], fallback=True)


def tr(message):
    return catalog(select_language()).gettext(message)


def _parts(template):
    return list(Formatter().parse(template))


@lru_cache(maxsize=None)
def service_patterns(language):
    current = catalog(language)
    # Also recognize messages saved by older Chinese-language service versions.
    aliases = {}
    for source in ('en', 'zh_CN', 'zh_TW'):
        for english, value in getattr(catalog(source), '_catalog', {}).items():
            if isinstance(english, str) and english and isinstance(value, str):
                aliases[value] = english
    exact = {source: current.gettext(english) for source, english in aliases.items()}
    patterns, prefixes = [], []
    for source, english in aliases.items():
        try:
            parts = _parts(source)
        except ValueError:
            continue
        if any(field is not None for _, field, _, _ in parts):
            regex, fields = '', []
            for literal, field, spec, conversion in parts:
                regex += re.escape(literal)
                if field is not None:
                    regex += '(.*?)'
                    fields.append(field)
            patterns.append((len(source), re.compile('^' + regex + '$', re.DOTALL), fields, current.gettext(english), english))
        elif len(source) >= 12 and source.rstrip().endswith((':', '：')):
            prefixes.append((source, current.gettext(english)))
    return exact, sorted(patterns, key=lambda x: x[0], reverse=True), sorted(prefixes, key=lambda x: len(x[0]), reverse=True)


def translate_text(message, language=None, _depth=0):
    """Translate known human messages, leaving paths, IDs and foreign errors intact."""
    if not isinstance(message, str) or not message or _depth > 5:
        return message
    language = language or select_language()
    exact, patterns, prefixes = service_patterns(language)
    if message in exact:
        return exact[message]
    for _, regex, fields, translated, english in patterns:
        match = regex.fullmatch(message)
        if match:
            values = dict(zip(fields, match.groups()))
            # Values were already formatted by the service; do not format a
            # captured string again with numeric specs such as :.0f.
            return ''.join(literal + ((translate_text(values.get(field, ''), language, _depth + 1)
                                      if english in NESTED_MESSAGES else values.get(field, '')) if field is not None else '')
                           for literal, field, _, _ in _parts(translated))
    for prefix, translated in prefixes:
        if message.startswith(prefix):
            return translated + translate_text(message[len(prefix):], language, _depth + 1)
    wrapped = re.fullmatch(r'(.*?GDBus\.Error:[\w.]+: )(.*?)( \(\d+\))?', message, re.DOTALL)
    if wrapped:
        return wrapped[1] + translate_text(wrapped[2], language, _depth + 1) + (wrapped[3] or '')
    for prefix in ('RuntimeError: ', 'PermissionError: ', 'OSError: ', '4070: ', '4070：'):
        if message.startswith(prefix):
            return prefix + translate_text(message[len(prefix):], language, _depth + 1)
    if '\n' in message:
        return '\n'.join(translate_text(line, language, _depth + 1) for line in message.split('\n'))
    return message


def localize_payload(value, key=None):
    if isinstance(value, dict):
        return {name: localize_payload(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [localize_payload(item, key) for item in value]
    if isinstance(value, str) and (key is None or key in TEXT_FIELDS):
        return translate_text(value)
    return value
