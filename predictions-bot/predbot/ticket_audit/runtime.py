"""Lazy private-audit construction. No calls on import/start/menu; owned sidecars only."""
from dataclasses import dataclass
from pathlib import Path
from predbot import ticket_audit_switch
from .analysis import EvidenceAnalyzer
from .free_research import FreeResearchResolver, OpenFootballAdapter, PublicSourceBudget
from .gemini import GeminiClient, Settings, UsageGuard
from .local import LocalResolver
from .research import ResearchCache
from .schemas import AuditUnavailable
from .diagnostics import diagnosed, note, note_settings, note_quota
from .service import AuditService


@dataclass(frozen=True)
class AuditBackend:
    client: GeminiClient
    service: AuditService


@diagnosed('backend_config')
def build_backend(cfg):
    if not ticket_audit_switch.ENABLED:
        raise AuditUnavailable('ticket_audit_disabled')
    settings = Settings.from_environment(getattr(cfg, 'env_file', None))
    note_settings(settings)
    # Diagnostic SELECT only, including when disabled. No sidecar is created.
    try:
        note_quota(Path(cfg.db_path).resolve().parent / 'ticket_audit_usage.sqlite3')
    except (TypeError, ValueError, OSError):
        pass
    # Selected-model-only product; no legacy model discovery or Search path.
    if settings.model != 'gemini-3.5-flash-lite':
        raise AuditUnavailable('unsupported_private_audit_model')
    if not settings.enabled or not settings.free_tier_confirmed or not settings.api_key:
        raise AuditUnavailable('private_audit_disabled')
    prediction = Path(cfg.db_path).resolve()
    note(stage='backend_usage_guard')
    usage = UsageGuard(prediction.parent / 'ticket_audit_usage.sqlite3', prediction_db=prediction)
    client = GeminiClient(settings, usage)
    note(stage='backend_gate')
    client._gate()  # No HTTP; refuse invalid configuration before handling data.
    note(stage='backend_source_guard')
    sources = PublicSourceBudget(prediction.parent / 'ticket_audit_sources.sqlite3')
    free = FreeResearchResolver([OpenFootballAdapter(sources)], ResearchCache())
    service = AuditService(LocalResolver(prediction, cfg.timezone), free, analyzer=EvidenceAnalyzer(client))
    return AuditBackend(client, service)
