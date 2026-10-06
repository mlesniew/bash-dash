"""Thin HTTP layer over ``game.services``. The player's game is ``request.session['game_id']``."""

import json

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from challenges import catalog, sandbox

from . import services
from .models import GameSession, GameTicket
from .templatetags.game_text import render_description

HTTP_STATUS = {
    services.RAN: 200,
    services.FINISHED: 409,
    services.TOO_LONG: 400,
    services.EMPTY: 400,
    services.UNAVAILABLE: 503,
    services.BUSY: 503,
    services.INTERNAL: 500,
    services.TIME_UP: 409,
}

MESSAGES = {
    services.FINISHED: 'The game is over.',
    services.TOO_LONG: f'Command too long (max {sandbox.MAX_COMMAND_CHARS} characters).',
    services.EMPTY: 'Type a command first.',
    services.UNAVAILABLE: 'Sandbox unavailable, try again.',
    services.BUSY: 'Server busy, try again in a moment.',
    services.INTERNAL: 'Internal error, try again.',
    services.TIME_UP: "Time's up.",
}
# A valid body is at most ~1.2 KB ({"command": <300 chars, JSON-escaped>}); anything
# bigger is rejected before parsing, which also bounds json's recursion depth.
MAX_BODY_BYTES = 4096
BAD_REQUEST = {'status': 'bad_request', 'message': 'Malformed request.'}
TICKET_SESSION_KEY = 'game_ticket_id'
TICKET_SOURCE_KEY = 'game_ticket_source'

CORRECT = 'Correct!'
TIMED_OUT = 'Timed out (5 s limit)'
INCORRECT = 'Incorrect.'


def verdict(correct: bool, error: str, timed_out: bool) -> str:
    """Player-facing verdict for a counted run (never ``error_internal``)."""
    if correct:
        return CORRECT
    if timed_out:
        return TIMED_OUT
    return error or INCORRECT


def _session_game(request) -> GameSession | None:
    game_id = request.session.get('game_id')
    if not game_id:
        return None
    services.expire_overdue(game_id)
    return GameSession.objects.filter(pk=game_id).first()


def _session_ticket(request) -> GameTicket | None:
    ticket_id = request.session.get(TICKET_SESSION_KEY)
    if not ticket_id:
        return None
    ticket = GameTicket.objects.select_related('game').filter(pk=ticket_id).first()
    if ticket is None:
        request.session.pop(TICKET_SESSION_KEY, None)
        request.session.pop(TICKET_SOURCE_KEY, None)
    return ticket


def _remember_ticket(request, ticket: GameTicket, source: str) -> None:
    request.session[TICKET_SESSION_KEY] = ticket.pk
    request.session[TICKET_SOURCE_KEY] = source


def _challenge_info(challenge) -> dict | None:
    if challenge is None:
        return None
    playable = catalog.main_set()
    index = next(i for i, c in enumerate(playable) if c.slug == challenge.slug) + 1
    return {
        'index': index,
        'total': len(playable),
        'title': challenge.title,
        'description_html': render_description(challenge.description),
    }


NO_GAME = {'status': 'no_game', 'message': 'No game in progress.'}


def _redirect_existing(game: GameSession):
    return redirect('game:done' if game.is_finished else 'game:play')


def _home_context(request, ticket, **extra) -> dict:
    """Context for game/home.html; the nick constraints drive the start form's hint and browser check."""
    return {
        'duration': settings.GAME_DURATION_S,
        'correct_answer_bonus_s': services.game_settings().correct_answer_bonus_s,
        'ticket': ticket,
        'show_handoff': request.session.get(TICKET_SOURCE_KEY) == 'qr',
        'computer_url': services.computer_url(request),
        'ticket_status_url': reverse('game:ticket_status'),
        'nick_max': services.NICK_MAX_CHARS,
        'nick_pattern': services.NICK_HTML_PATTERN,
        'nick_desc': services.NICK_CHARS_DESC,
        **extra,
    }


def _gate_refusal(request, token):
    """403 refusal page when ``token`` is not a valid start token, else None."""
    status = services.check_start_token(token)
    if status == services.TOKEN_OK:
        return None
    return render(request, 'game/gate.html', {
        'expired': status == services.TOKEN_EXPIRED, 'invalid': status == services.TOKEN_INVALID,
        'code': token, 'digits': services.TOKEN_DIGITS}, status=403)


def _render_join(request, error=''):
    return render(request, 'game/join.html', {
        'error': error,
        'ticket_length': 5,
        'gate_digits': services.TOKEN_DIGITS,
    })


@require_GET
def home(request):
    game = _session_game(request)
    if game:
        return _redirect_existing(game)
    ticket = _session_ticket(request)
    if ticket:
        if ticket.game_id:
            services.expire_overdue(ticket.game_id)
            ticket.refresh_from_db()
            if ticket.game.is_finished:
                return redirect('game:done')
            return render(request, 'game/ticket_status.html', {
                'ticket': ticket, 'status_url': reverse('game:ticket_status'),
                'done_url': reverse('game:done'),
            })
        return render(request, 'game/home.html', _home_context(request, ticket))

    raw_token = request.GET.get('t')
    if raw_token is None:
        return _render_join(request)
    token = services.normalize_token(raw_token)
    refusal = _gate_refusal(request, token)
    if refusal:
        return refusal
    ticket = services.issue_game_ticket()
    _remember_ticket(request, ticket, 'qr')
    return render(request, 'game/home.html', _home_context(request, ticket))


@require_POST
def join(request):
    game = _session_game(request)
    if game:
        return _redirect_existing(game)
    existing = _session_ticket(request)
    if existing:
        return redirect('game:home')
    raw = request.POST.get('code', '')
    ticket = services.find_available_ticket(raw)
    if ticket is not None:
        _remember_ticket(request, ticket, 'code')
        return redirect('game:home')

    token = services.normalize_token(raw)
    if services.check_start_token(token) == services.TOKEN_OK:
        ticket = services.issue_game_ticket()
        _remember_ticket(request, ticket, 'code')
        return redirect('game:home')
    return _render_join(request, 'That game code is not valid or has already been used.')


@require_POST
def start(request):
    game = _session_game(request)
    if game:
        return _redirect_existing(game)
    ticket = _session_ticket(request)
    if ticket is None:
        return _render_join(request, 'Scan the booth QR code or enter your game code first.')
    if ticket.game_id:
        return redirect('game:home')
    nick = request.POST.get('nick', '')
    try:
        game, ticket = services.start_game_with_ticket(ticket.pk, nick)
    except ValueError:
        return render(request, 'game/home.html', _home_context(
            request, ticket,
            error=f'Use 1-{services.NICK_MAX_CHARS} {services.NICK_CHARS_DESC} (no spaces).', nick=nick))
    if game is None:
        return redirect('game:home')
    request.session['game_id'] = str(game.pk)
    return redirect('game:play')


@require_GET
def ticket_status(request):
    ticket = _session_ticket(request)
    if ticket is None:
        return JsonResponse({'status': 'no_ticket'}, status=403)
    if ticket.game_id is None:
        return JsonResponse({'status': 'waiting'})
    services.expire_overdue(ticket.game_id)
    game = GameSession.objects.get(pk=ticket.game_id)
    return JsonResponse({'status': 'finished' if game.is_finished else 'in_progress'})


@require_GET
@ensure_csrf_cookie  # play.js reads the csrftoken cookie
def play(request):
    game = _session_game(request)
    if game is None:
        return redirect('game:home')
    if game.is_finished:
        return redirect('game:done')
    challenge = services.current_challenge(game)
    if challenge is None:
        return redirect('game:done')
    last = services.last_attempt(game)
    return render(request, 'game/play.html', {
        'game': game,
        'challenge': challenge,
        'info': _challenge_info(challenge),
        'last': last,
        'remaining_ms': services.remaining_ms(game),
        'last_verdict': verdict(last.correct, last.error, last.timed_out) if last else '',
    })


@require_GET
def state(request):
    """Authoritative remaining time, for clients whose monotonic clock may have paused."""
    game = _session_game(request)
    if game is None:
        return JsonResponse(NO_GAME, status=403)
    return JsonResponse({'remaining_ms': services.remaining_ms(game), 'finished': game.is_finished})


@require_POST
def command(request):
    game = _session_game(request)
    if game is None:
        return JsonResponse(NO_GAME, status=403)
    if len(request.body) > MAX_BODY_BYTES:
        return JsonResponse({**BAD_REQUEST, 'message': 'Request too large.'}, status=413)
    try:
        body = json.loads(request.body)
        cmd = body['command']
        if not isinstance(cmd, str):
            raise TypeError
        cmd.encode()  # lone surrogates: UnicodeEncodeError (a ValueError), not "too long" later
    except (ValueError, KeyError, TypeError, RecursionError):
        return JsonResponse(BAD_REQUEST, status=400)

    outcome = services.submit_command(game.pk, cmd)
    game = outcome.game
    challenge = None if game.is_finished else services.current_challenge(game)
    data = {
        'status': outcome.status,
        'attempts': game.attempts,
        'solved': game.solved,
        'finished': game.is_finished,
        'remaining_ms': services.remaining_ms(game),
        'challenge': _challenge_info(challenge),
    }
    if outcome.status == services.RAN:
        r = outcome.result
        data['message'] = verdict(r.correct, r.error, r.timed_out)
        data['result'] = {'correct': r.correct, 'output': r.output, 'message': data['message']}
    else:
        data['message'] = MESSAGES[outcome.status]
    return JsonResponse(data, status=HTTP_STATUS[outcome.status])


@require_GET
def done(request):
    game = _session_game(request)
    if game is None:
        ticket = _session_ticket(request)
        game = ticket.game if ticket and ticket.game_id else None
        if game is None:
            return redirect('game:home')
        services.expire_overdue(game.pk)
        game.refresh_from_db()
    if not game.is_finished:
        return redirect('game:play' if request.session.get('game_id') else 'game:home')
    total = len(catalog.main_set())
    rank = services.rank_of(game)
    place, ranked_total = rank if rank else (None, None)
    return render(request, 'game/done.html', {
        'game': game,
        'total': total,
        'place': place,
        'ranked_total': ranked_total,
        'timed_out': game.timed_out,
        'all_solved': not game.timed_out and game.solved >= total,
    })
