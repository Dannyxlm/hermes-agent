"""Human input policy and transient answer validation; never log input values."""
from __future__ import annotations

import json
import math

HUMAN_METHODS = frozenset({'approval', 'clarify', 'setup_choose', 'sudo', 'secret',
    'vault.unlock_prompt', 'vault.save_login', 'vault.code', 'display.install.sudo'})
SECURE_METHODS = HUMAN_METHODS - {'approval', 'clarify', 'setup_choose'}
MAX_ANSWER_BYTES = 65536
APPROVAL_RESPONSE_FIELDS = ('scoped_approval_response', 'request_epoch', 'request_revision', 'reason', 'auth')


def approval_response_metadata(params: dict, choice: str) -> tuple[bool, dict]:
    """Enhanced selectors are all-or-nothing; authentication is receipt metadata only."""
    from tools.approval import normalize_approval_auth
    enhanced = any(params.get(key) is not None for key in APPROVAL_RESPONSE_FIELDS)
    if not enhanced:
        return False, {}
    if (type(params.get('scoped_approval_response')) is not int or params['scoped_approval_response'] != 1
            or not isinstance(params.get('request_epoch'), str) or not params['request_epoch']
            or type(params.get('request_revision')) is not int or params['request_revision'] < 1):
        raise ValueError('Scoped approval envelope required')
    reason = params.get('reason')
    if reason is not None and (choice != 'deny' or not isinstance(reason, str) or len(reason) > 2000):
        raise ValueError('A bounded suggestion must deny the original action')
    result = {'reason': reason} if reason is not None else {}
    if (auth := normalize_approval_auth(params.get('auth'))) is not None:
        result['auth'] = auth
    return True, result


def approval_response_settled(result: dict) -> bool:
    return (type(result.get('resolved')) is int and result['resolved'] == 1
            and result.get('status') in {None, 'accepted', 'already_resolved'})


def approval_envelope_supported(params: dict) -> bool:
    expiry = params.get('expires_at')
    return (type(params.get('scoped_approval_response')) is int and params['scoped_approval_response'] == 1
            and isinstance(params.get('request_epoch'), str) and bool(params['request_epoch'])
            and type(params.get('request_revision')) is int and params['request_revision'] >= 1
            and type(expiry) in (int, float) and math.isfinite(expiry))


def validate_team_approval(pending: dict, choice: str, *, enhanced: bool, metadata: dict) -> None:
    from tools.approval_gateway_wait import _consent_kind
    if choice not in ('once', 'deny') or choice not in (pending.get('choices') or []):
        raise PermissionError('Approval choice is not offered to Team')
    if enhanced and not approval_envelope_supported(pending):
        raise PermissionError('Scoped approval response is unsupported for this request')
    if _consent_kind(pending) == 'payment' and choice == 'once' and (not enhanced or not metadata.get('auth')):
        raise PermissionError('Payment requires scoped review and recognized authentication metadata')


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
        multi = bool(params.get('multi_select'))
        if len(picks) != len(set(picks)) or (len(picks) > 1 and not multi):
            raise ValueError('Invalid setup choice')
        label = value.get('label')
        labels = label if isinstance(label, list) else [label] if label else []
        options = {o['id']: o['label'] for o in params.get('options') or []}
        if options:
            # Server-drawn rows: a pick must be one of them and its label must be that row's name.
            if not set(picks) <= options.keys():
                raise ValueError('Invalid setup choice')
            if labels and labels != [options[pick] for pick in picks]:
                raise ValueError('Invalid setup label')
        else:
            # App-drawn rows (accent/theme/layout/connectors/plugins) or a free-text question card:
            # the client owns the ids, so only shape is checked. A list needs a multi-select card,
            # and any labels name the picks one-for-one in the same shape.
            if isinstance(picked, list) and not multi:
                raise ValueError('Invalid setup choice')
            if label is not None and (isinstance(label, list) != isinstance(picked, list)
                                      or len(label if isinstance(label, list) else [label]) != len(picks)):
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
