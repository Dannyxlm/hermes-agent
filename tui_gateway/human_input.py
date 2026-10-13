"""Human input policy and transient answer validation; never log input values."""
from __future__ import annotations

import json
import math

HUMAN_METHODS = frozenset({'approval', 'clarify', 'setup_choose', 'sudo', 'secret',
    'vault.unlock_prompt', 'vault.save_login', 'vault.code', 'display.install.sudo'})
SECURE_METHODS = HUMAN_METHODS - {'approval', 'clarify', 'setup_choose'}
MAX_ANSWER_BYTES = 65536


def human_timeout() -> float:
    """Resolve once inside the producer's owning profile scope."""
    from hermes_cli.config import load_config
    policy = load_config().get('human_input')
    value = policy.get('timeout', 300) if isinstance(policy, dict) else 300
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return 300.0
    return float(value)


def validate_answer(method: str, params: dict, result: dict) -> dict:
    from pydantic import ValidationError
    from tui_gateway.contracts import SERVER_REQUESTS
    try:
        raw = json.dumps(result, allow_nan=False)
        if len(raw.encode()) > MAX_ANSWER_BYTES:
            raise ValueError()
        model = SERVER_REQUESTS[method].result.model_validate_json(raw, strict=True)
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise ValueError('Invalid human input result') from None
    value = model.model_dump(mode='json', exclude_unset=True)
    if method == 'approval':
        from tui_gateway.contracts.server_requests import ApprovalKind
        choice = value['choice']
        inferred = ApprovalKind.for_context(params.get('surface'), params.get('tool_name'),
                                             payment=params.get('payment') is not None)
        consent = {ApprovalKind.payment, ApprovalKind.mcp_trust, ApprovalKind.mcp_elicitation}
        once_only = inferred in consent or params.get('kind') in consent
        forbidden = ((choice in ('session', 'always') and (once_only or params.get('allow_session') is False or params.get('smart_denied')))
                     or (choice == 'always' and params.get('allow_permanent') is False))
        if choice not in params.get('choices', []) or forbidden or value.get('all'):
            raise PermissionError('Approval scope is not offered')
        if value.get('reason') is not None and choice != 'deny':
            raise ValueError('A suggestion must deny the original action')
    elif method == 'clarify':
        answers = value.get('answers')
        qids = {q['qid'] for q in params.get('questions', [])}
        if answers is not None and not set(answers) <= qids:
            raise ValueError('Unknown question')
    elif method == 'setup_choose':
        picked = value.get('picked')
        picks = picked if isinstance(picked, list) else [picked] if picked else []
        options = {o['id']: o['label'] for o in params.get('options') or []}
        if len(picks) != len(set(picks)) or not set(picks) <= options.keys() or (len(picks) > 1 and not params.get('multi_select')):
            raise ValueError('Invalid setup choice')
        label = value.get('label')
        labels = label if isinstance(label, list) else [label] if label else []
        if labels and labels != [options[pick] for pick in picks]:
            raise ValueError('Invalid setup label')
    elif method == 'vault.save_login' and value.get('value'):
        try:
            login = json.loads(value['value'])
            if not isinstance(login, dict) or set(login) != {'identifier', 'password'} or not all(isinstance(v, str) for v in login.values()):
                raise ValueError()
        except (ValueError, TypeError, RecursionError):
            raise ValueError('Invalid secure input') from None
    return value


def approval_quick_once(params: dict) -> bool:
    """Explicit safe quick-consent projection, never request/answer authority.

    Inspect the full producer cut BEFORE dropping extras. Tool name or absent flags
    alone cannot certify read-only intent. This predicate is shared with U6.
    """
    return (params.get('risk') == 'read_only' and params.get('kind') == 'tool'
            and params.get('tool_name') in {'read_file', 'search_files', 'web_search', 'web_extract'}
            and not params.get('surface') and params.get('payment') is None
            and params.get('destructive', False) is False
            and params.get('sensitive', False) is False
            and 'once' in (params.get('choices') or []))


def safe_params(method: str, params: dict) -> dict:
    """Keep renderable context, never producer-owned credential defaults/extra metadata."""
    from tui_gateway.contracts import SERVER_REQUESTS
    fields = SERVER_REQUESTS[method].params.model_fields
    allowed = set(fields) | {field.alias for field in fields.values() if field.alias}
    safe = {key: value for key, value in params.items() if key in allowed}
    safe.pop('metadata', None)
    if method == 'approval':
        safe['quick_once'] = approval_quick_once(params)
        safe['risk'] = params.get('risk', 'unknown')
    return safe
