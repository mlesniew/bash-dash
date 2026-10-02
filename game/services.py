"""Game rules: starting a game, what a command does to it, and sandbox concurrency.

Views and ``bench_game`` call this module; nothing else knows the rules.

Transaction discipline: never hold a DB transaction open across ``run_command``
(0.15-6 s). Each submit is a short read, the sandbox run with no transaction,
then one short ``transaction.atomic()`` for the Attempt + conditional updates.
"""

import hashlib
import hmac
import logging
import re
import threading
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote

import segno
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import DurationField, ExpressionWrapper, F, Q
from django.db.models.functions import Coalesce, Least
from django.urls import reverse
from django.utils import timezone

from challenges import catalog, sandbox
from challenges.catalog import Challenge

from .models import (
    Attempt, GameSession, GameSettings, GameTicket, GateSettings, generate_code,
    generate_ticket_code,
)

logger = logging.getLogger('game')

# Query form of GameSession.timed_out: finished by the clock, still on a challenge.
TIMED_OUT_Q = Q(finished_at=F('deadline_at'), current_slug__isnull=False)

NICK_MAX_CHARS = 64
# Allow email addresses too: letters, digits, _ and . + @
NICK_RE = re.compile(rf'[A-Za-z0-9_.+@]{{1,{NICK_MAX_CHARS}}}')  # use with fullmatch
NICK_HTML_PATTERN = rf' *{NICK_RE.pattern} *'  # browser pattern: start_game trims outer spaces
NICK_CHARS_DESC = 'letters, digits, _ . + @'  # human-readable allowed set (for hints/errors)
CODE_RE = re.compile(r'^\d{6}$')
CODE_ATTEMPTS = 10
TICKET_CODE_RE = re.compile(r'^[ACDEFHJKMNPQRTUVWXY3479]{5}$')
TICKET_CODE_ATTEMPTS = 20
CORRECT_ANSWER_BONUS_MAX_S = 3600

# SubmitOutcome.status values. Only RAN is counted as an attempt.
RAN = 'ran'
FINISHED = 'finished'
TOO_LONG = 'too_long'
EMPTY = 'empty'
UNAVAILABLE = 'unavailable'
BUSY = 'busy'
TIME_UP = 'time_up'
INTERNAL = 'internal'


@dataclass(frozen=True)
class SubmitOutcome:
    status: str
    result: sandbox.SandboxResult | None
    game: GameSession


# Per-process cap on concurrent sandbox runs. The harness does not go through here,
# so its own --parallel is never throttled by game settings.
_semaphore = None
_semaphore_lock = threading.Lock()


def _get_semaphore() -> threading.BoundedSemaphore:
    global _semaphore
    with _semaphore_lock:
        if _semaphore is None:
            _semaphore = threading.BoundedSemaphore(settings.SANDBOX_MAX_CONCURRENT)
        return _semaphore


def _reset_semaphore() -> None:
    """Test hook: the next ``_get_semaphore`` is re-sized from settings."""
    global _semaphore
    with _semaphore_lock:
        _semaphore = None


def start_game(nick: str) -> GameSession:
    nick = nick.strip()
    if not NICK_RE.fullmatch(nick):
        raise ValueError(f'nick must be 1-{NICK_MAX_CHARS} characters: {NICK_CHARS_DESC}')
    first = catalog.first_playable()
    if first is None:
        raise RuntimeError('no playable challenges')
    now = timezone.now()
    for _ in range(CODE_ATTEMPTS):
        try:
            with transaction.atomic():
                return GameSession.objects.create(
                    nick=nick, current_slug=first.slug, started_at=now,
                    deadline_at=now + timedelta(seconds=settings.GAME_DURATION_S),
                    code=generate_code())
        except IntegrityError:
            continue
    raise RuntimeError('could not allocate a unique prize code')


def issue_game_ticket() -> GameTicket:
    """Create a permanent ticket; code values are retained and never reused."""
    for _ in range(TICKET_CODE_ATTEMPTS):
        try:
            with transaction.atomic():
                return GameTicket.objects.create(code=generate_ticket_code())
        except IntegrityError:
            continue
    raise RuntimeError('could not allocate a unique game ticket code')


def normalize_ticket_code(raw: str) -> str | None:
    code = ''.join((raw or '').split()).upper()
    return code if TICKET_CODE_RE.fullmatch(code) else None


def find_available_ticket(raw: str) -> GameTicket | None:
    code = normalize_ticket_code(raw)
    if code is None:
        return None
    return GameTicket.objects.filter(code=code, game__isnull=True).first()


def start_game_with_ticket(ticket_id, nick: str) -> tuple[GameSession | None, GameTicket]:
    """Atomically consume a ticket and start one game; the first concurrent Start wins."""
    nick = nick.strip()
    if not NICK_RE.fullmatch(nick):
        raise ValueError(f'nick must be 1-{NICK_MAX_CHARS} characters: {NICK_CHARS_DESC}')

    first = catalog.first_playable()
    if first is None:
        raise RuntimeError('no playable challenges')
    now = timezone.now()
    for _ in range(CODE_ATTEMPTS):
        try:
            with transaction.atomic():
                ticket = GameTicket.objects.select_for_update().get(pk=ticket_id)
                if ticket.game_id is not None:
                    return None, ticket
                game = GameSession.objects.create(
                    nick=nick, current_slug=first.slug, started_at=now,
                    deadline_at=now + timedelta(seconds=settings.GAME_DURATION_S),
                    code=generate_code(),
                )
                ticket.game = game
                ticket.save(update_fields=['game'])
                return game, ticket
        except IntegrityError:
            continue
    raise RuntimeError('could not allocate a unique prize code')


def expire_overdue(game_id=None, now=None) -> int:
    """Finish overdue games at their deadline. Idempotent; returns the number finished."""
    now = now or timezone.now()
    qs = GameSession.objects.filter(finished_at__isnull=True, deadline_at__lte=now)
    if game_id is not None:
        qs = qs.filter(pk=game_id)
    return qs.update(finished_at=F('deadline_at'))


def ranked_games():
    """Finished games in ranking order: solved desc, attempts asc, solve time asc.

    The single definition of the PRD ranking rule. Hidden (disqualified) games are not ranked.
    Overdue games are finished first. ``elapsed`` is NULL when nothing was solved.
    """
    expire_overdue()
    return GameSession.objects.filter(finished_at__isnull=False, hidden_at__isnull=True).annotate(
        elapsed=ExpressionWrapper(F('last_solved_at') - F('started_at'), output_field=DurationField()),
    ).order_by('-solved', 'attempts', 'elapsed', 'started_at')


def rank_of(game: GameSession) -> tuple[int, int] | None:
    """``(place, total)`` among finished games (competition ranking), or None if unfinished."""
    ranked = ranked_games()
    me = ranked.filter(pk=game.pk).first()
    if me is None:
        return None
    better = Q(solved__gt=me.solved) | Q(solved=me.solved, attempts__lt=me.attempts)
    if me.elapsed is not None:
        better |= Q(solved=me.solved, attempts=me.attempts, elapsed__lt=me.elapsed)
    return 1 + ranked.filter(better).count(), ranked.count()


def _set_hidden(game_id, hide: bool, now=None) -> tuple[GameSession, bool]:
    try:
        qs = GameSession.objects.filter(pk=game_id, hidden_at__isnull=hide)
        changed = qs.update(hidden_at=(now or timezone.now()) if hide else None) == 1
        return GameSession.objects.get(pk=game_id), changed
    except (ValidationError, ValueError):
        raise GameSession.DoesNotExist(f'bad game id: {game_id!r}') from None


def hide_game(game_id, now=None) -> tuple[GameSession, bool]:
    """Disqualify a game from the ranking. Returns ``(game, True)`` if this call hid it."""
    return _set_hidden(game_id, True, now)


def unhide_game(game_id) -> tuple[GameSession, bool]:
    """Undo ``hide_game``. Returns ``(game, True)`` if this call unhid it."""
    return _set_hidden(game_id, False)


def hidden_games():
    return GameSession.objects.filter(hidden_at__isnull=False).order_by('-hidden_at')


@dataclass(frozen=True)
class BoardRow:
    game_id: object
    nick: str
    solved: int
    attempts: int
    place: int


@dataclass(frozen=True)
class Board:
    top: list
    recent: list
    ranked_total: int


def hall_of_fame(top_n: int, recent_n: int) -> Board:
    """Top ``top_n`` and the ``recent_n`` latest finished games; places from one ranked pass."""
    rows = []
    prev_key = prev_place = None
    for i, g in enumerate(ranked_games(), start=1):
        key = (g.solved, g.attempts, g.elapsed)
        place = prev_place if key == prev_key else i
        prev_key, prev_place = key, place
        rows.append((g, BoardRow(g.pk, g.nick, g.solved, g.attempts, place)))
    latest = sorted(rows, key=lambda r: (r[0].finished_at, r[0].pk), reverse=True)[:recent_n]
    return Board(top=[r for _, r in rows[:top_n]], recent=[r for _, r in latest], ranked_total=len(rows))


def remaining_ms(game: GameSession, now=None) -> int:
    if game.is_finished:
        return 0
    now = now or timezone.now()
    return max(0, int((game.deadline_at - now).total_seconds() * 1000))


def current_challenge(game: GameSession) -> Challenge | None:
    """The game's current playable challenge; ``None`` means finished.

    A slug cut mid-game moves the player to the next playable challenge; if none
    follows, the game finishes (without a solve).
    """
    if game.current_slug is None:
        return None
    ch = catalog.playable(game.current_slug)
    if ch is not None:
        return ch
    old = game.current_slug
    nxt = catalog.next_playable(old)
    if nxt is not None:
        GameSession.objects.filter(pk=game.pk, current_slug=old).update(current_slug=nxt.slug)
    else:
        GameSession.objects.filter(pk=game.pk, current_slug=old, finished_at__isnull=True).update(
            current_slug=None, finished_at=timezone.now())
    game.refresh_from_db()
    return catalog.playable(game.current_slug) if game.current_slug else None


def submit_command(game_id, command: str) -> SubmitOutcome:
    now = timezone.now()
    expire_overdue(game_id, now)
    game = GameSession.objects.get(pk=game_id)
    if game.is_finished and not game.timed_out:  # all solved, or the catalog ran out
        return SubmitOutcome(FINISHED, None, game)
    if now >= game.deadline_at:
        return SubmitOutcome(TIME_UP, None, game)
    challenge = current_challenge(game)
    if challenge is None:
        return SubmitOutcome(FINISHED, None, game)
    if not command.strip():
        return SubmitOutcome(EMPTY, None, game)
    if len(command) > sandbox.MAX_COMMAND_CHARS:
        return SubmitOutcome(TOO_LONG, None, game)

    sandbox.reap_stale_once()
    sem = _get_semaphore()
    if not sem.acquire(timeout=settings.SANDBOX_QUEUE_TIMEOUT_S):
        return SubmitOutcome(BUSY, None, game)
    try:
        result = sandbox.run_command(challenge, command)
    except sandbox.SandboxUnavailable as exc:
        logger.warning('sandbox unavailable: %s', exc)
        return SubmitOutcome(UNAVAILABLE, None, game)
    except ValueError:  # backstop: length is checked above
        return SubmitOutcome(TOO_LONG, None, game)
    finally:
        sem.release()

    if result.error_internal:
        logger.error('internal sandbox error on %s for command %r: %s',
                     challenge.slug, command, result.error_internal)
        return SubmitOutcome(INTERNAL, result, game)

    counted = _record(game, challenge, command, result)
    expire_overdue(game.pk)
    game.refresh_from_db()
    return SubmitOutcome(RAN if counted else FINISHED, result if counted else None, game)


def _record(game: GameSession, challenge: Challenge, command: str, result: sandbox.SandboxResult) -> bool:
    """Count the run and store its Attempt, unless the game finished (other than by timeout) while it ran.

    A run sent before the deadline counts even if the game was expired meanwhile; the
    entry check in ``submit_command`` is what rejects commands sent after it.

    The counter update goes first and gates the insert, so Attempt rows and
    ``GameSession.attempts`` always agree. Returns whether the run was counted.
    """
    now = timezone.now()
    with transaction.atomic():
        counted = GameSession.objects.filter(Q(finished_at__isnull=True) | TIMED_OUT_Q, pk=game.pk).update(
            attempts=F('attempts') + 1)
        if not counted:
            return False
        Attempt.objects.create(
            game=game, slug=challenge.slug, command=command, correct=result.correct,
            output=result.output, error=result.error[:255], timed_out=result.timed_out,
            duration_ms=round(result.duration_s * 1000),
        )
        if result.correct:
            nxt = catalog.next_playable(challenge.slug)
            # The current_slug guard makes a duplicate correct submit advance once.
            # Timestamps are capped at the pre-bonus deadline. A command submitted in time may
            # reopen a timeout that was recorded while its sandbox run was still in progress.
            stamp = Least(now, F('deadline_at'))
            updates = {
                'solved': F('solved') + 1,
                'last_solved_at': stamp,
                'current_slug': nxt.slug if nxt else None,
                'finished_at': None if nxt else Coalesce(F('finished_at'), stamp),
            }
            if nxt:
                updates['deadline_at'] = (
                    F('deadline_at') + timedelta(seconds=game_settings().correct_answer_bonus_s)
                )
            GameSession.objects.filter(pk=game.pk, current_slug=challenge.slug).update(
                **updates,
            )
    return True


def last_attempt(game: GameSession) -> Attempt | None:
    return game.attempt_set.order_by('-created_at', '-pk').first()


def normalize_code(raw: str) -> str | None:
    """A 6-digit prize code from staff input (whitespace ignored), or None if malformed."""
    code = re.sub(r'\s+', '', raw or '')
    return code if CODE_RE.match(code) else None


def find_by_code(code: str) -> GameSession | None:
    """The game with this prize code, with an overdue game already finished."""
    game = GameSession.objects.filter(code=code).first()
    if game is None:
        return None
    expire_overdue(game.pk)
    return GameSession.objects.get(pk=game.pk)


def mark_prize_given(game_id, now=None) -> tuple[GameSession, bool]:
    """Mark the prize given, once. Returns ``(game, marked_by_this_call)``.

    False means it was already marked or the game is unfinished (see ``game.is_finished``).
    A single guarded UPDATE keeps concurrent presses safe.
    """
    now = now or timezone.now()
    expire_overdue(game_id)
    marked = GameSession.objects.filter(
        pk=game_id, finished_at__isnull=False, prize_given_at__isnull=True).update(prize_given_at=now)
    return GameSession.objects.get(pk=game_id), bool(marked)


def solve_time(game: GameSession) -> timedelta | None:
    """Time from start to the last solve; None when nothing was solved."""
    if game.last_solved_at is None:
        return None
    return game.last_solved_at - game.started_at


def game_settings() -> GameSettings:
    return GameSettings.objects.get_or_create(pk=1)[0]


def set_correct_answer_bonus(seconds: int) -> GameSettings:
    if not 0 <= seconds <= CORRECT_ANSWER_BONUS_MAX_S:
        raise ValueError(f'bonus must be 0-{CORRECT_ANSWER_BONUS_MAX_S} seconds')
    obj = game_settings()
    obj.correct_answer_bonus_s = seconds
    obj.save()
    return obj


# QR start gate (S-06)
TOKEN_OK = 'ok'
TOKEN_MISSING = 'missing'
TOKEN_INVALID = 'invalid'
TOKEN_EXPIRED = 'expired'
TOKEN_TTL_MIN_S = 120
TOKEN_TTL_MAX_S = 86400
_TOKEN_SALT = 'game.start-token'
TOKEN_DIGITS = 6


def gate_settings() -> GateSettings:
    # Seed within the staff bounds, so a stray env value cannot close the gate or show odd minutes.
    seed = settings.START_TOKEN_TTL_S
    if seed:
        seed = min(max(seed, TOKEN_TTL_MIN_S), TOKEN_TTL_MAX_S)
    return GateSettings.objects.get_or_create(pk=1, defaults={'token_ttl_s': seed})[0]


def set_token_ttl(ttl_s: int) -> GateSettings:
    if ttl_s != 0 and not TOKEN_TTL_MIN_S <= ttl_s <= TOKEN_TTL_MAX_S:
        raise ValueError(f'ttl must be 0 or {TOKEN_TTL_MIN_S}-{TOKEN_TTL_MAX_S} seconds')
    obj = gate_settings()
    obj.token_ttl_s = ttl_s
    obj.save()
    return obj


def _epoch(now) -> int:
    return int((now or timezone.now()).timestamp())


def _code_for_bucket(bucket: int) -> str:
    digest = hmac.new(settings.SECRET_KEY.encode(), f'{_TOKEN_SALT}:{bucket}'.encode(), hashlib.sha256).digest()
    return f'{int.from_bytes(digest[:8], "big") % 10 ** TOKEN_DIGITS:0{TOKEN_DIGITS}d}'


def normalize_token(token) -> str:
    """Drop whitespace so a code typed as "123 456" matches."""
    return ''.join((token or '').split())


def issue_start_token(now=None) -> str:
    """Six-digit code, stable within one rotation period. With no expiry (ttl 0) it never rotates."""
    if gate_settings().token_ttl_s == 0:
        return _code_for_bucket(0)
    return _code_for_bucket(_epoch(now) // settings.START_TOKEN_ROTATE_S)


def check_start_token(token, now=None) -> str:
    token = normalize_token(token)
    if not token:
        return TOKEN_MISSING
    ttl = gate_settings().token_ttl_s
    if ttl == 0:
        return TOKEN_OK if hmac.compare_digest(token, _code_for_bucket(0)) else TOKEN_INVALID
    rotate = settings.START_TOKEN_ROTATE_S
    current = _epoch(now) // rotate
    # Scan back past the longest lifetime staff can set, to tell "expired" from "wrong".
    for bucket in range(current, current - TOKEN_TTL_MAX_S // rotate - 2, -1):
        if hmac.compare_digest(token, _code_for_bucket(bucket)):
            return TOKEN_EXPIRED if _epoch(now) - bucket * rotate > ttl else TOKEN_OK
    return TOKEN_INVALID


def start_url(request, token: str) -> str:
    path = f'{reverse("game:home")}?t={quote(token)}'
    if settings.PUBLIC_BASE_URL:
        return f'{settings.PUBLIC_BASE_URL}{path}'
    return request.build_absolute_uri(path)


def computer_url(request) -> str:
    path = reverse('game:home')
    if settings.PUBLIC_BASE_URL:
        return f'{settings.PUBLIC_BASE_URL}{path}'
    return request.build_absolute_uri(path)


def qr_svg(url: str) -> str:
    """Inline, viewBox-only SVG. Always dark on white: scanners need contrast in any theme."""
    return segno.make(url, error='m').svg_inline(omitsize=True, border=2, dark='#000', light='#fff')
