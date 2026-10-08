"""Reviewed free-capability policy, separate from live project discovery.

Pricing/model pages reviewed 2026-10-07. Never infer cost or entitlement from
models.list. Unknown models/aliases fail closed until explicitly reviewed.
"""
from dataclasses import dataclass
import re

DEFAULT_MODEL = 'gemini-3.5-flash-lite'  # Live text generation validated 2026-10-07.
LEGACY_FREE_SEARCH_MODEL = 'gemini-2.5-flash'
PRICING_DOC = 'https://ai.google.dev/gemini-api/docs/pricing'
DEPRECATIONS_DOC = 'https://ai.google.dev/gemini-api/docs/deprecations'


@dataclass(frozen=True)
class FreeCapabilities:
    text: bool
    structured: bool
    image: bool
    search: bool
    thinking_level: str | None = None
    legacy_restricted: bool = False


REVIEWED_MODELS = {
    'gemini-2.5-flash': FreeCapabilities(True, True, True, True, legacy_restricted=True),
    'gemini-2.5-flash-lite': FreeCapabilities(True, True, True, True, legacy_restricted=True),
    'gemini-3.5-flash-lite': FreeCapabilities(True, True, True, False, 'MINIMAL'),
    'gemini-3.6-flash': FreeCapabilities(True, True, True, False, 'MINIMAL'),
    'gemini-3.7-flash': FreeCapabilities(True, True, True, False, 'LOW'),
    'gemini-3.8-flash': FreeCapabilities(True, True, True, False, 'LOW'),
}


def generation_candidates(records):
    """Intersect actual advertised methods with reviewed stable free models.

    No hard-coded *discovery*, no latest aliases, preview, TTS, embedding or image
    generation. Prefer a remaining legacy free-Search candidate, then current Lite,
    then stable newer Flash. Stop probing on the first generation success.
    """
    candidates = []
    for record in records:
        if not isinstance(record, dict):
            continue
        name = record.get('name', '')
        model = name.removeprefix('models/') if isinstance(name, str) else ''
        policy = REVIEWED_MODELS.get(model)
        methods = record.get('supportedGenerationMethods', [])
        if policy and policy.text and isinstance(methods, list) and 'generateContent' in methods:
            candidates.append(model)
    def rank(model):
        policy = REVIEWED_MODELS[model]
        numeric = tuple(int(n) for n in re.findall(r'\d+', model))
        return (0 if policy.search else 1, 0 if 'lite' in model else 1, tuple(-n for n in numeric))
    return sorted(set(candidates), key=rank)
