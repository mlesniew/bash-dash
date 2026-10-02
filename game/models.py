import secrets
import uuid

from django.db import models
from django.utils import timezone


def generate_code() -> str:
    """A random 6-digit prize code, zero-padded."""
    return f'{secrets.randbelow(10**6):06d}'


GAME_TICKET_ALPHABET = 'ACDEFHJKMNPQRTUVWXY3479'


def generate_ticket_code() -> str:
    """A readable five-character code that is never reassigned."""
    return ''.join(secrets.choice(GAME_TICKET_ALPHABET) for _ in range(5))


class GameSession(models.Model):
    """One player's game. Counters are denormalised for S-02 (time limit) and S-03 (ranking).

    Finished <=> ``finished_at`` is set. ``current_slug`` None <=> nothing left to solve;
    a timed-out game keeps its ``current_slug``.
    ``code`` is the unique 6-digit prize code shown on the summary page.
    ``prize_given_at`` is set once, when booth staff hand out the prize.
    ``hidden_at`` is set when staff hide (disqualify) the game: it leaves the ranking, result untouched.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    nick = models.CharField(max_length=64)
    # default (not auto_now_add) so start_game can store one instant in started_at and deadline_at.
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    deadline_at = models.DateTimeField(db_index=True)
    # None means nothing left to solve (all solved, or the catalog ran out).
    current_slug = models.CharField(max_length=64, null=True)
    attempts = models.PositiveIntegerField(default=0)
    solved = models.PositiveIntegerField(default=0)
    last_solved_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    code = models.CharField(max_length=6, unique=True, editable=False, default=generate_code)
    prize_given_at = models.DateTimeField(null=True, blank=True, editable=False)
    hidden_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)

    def __str__(self):
        return f'{self.nick} ({self.id})'

    @property
    def is_finished(self) -> bool:
        return self.finished_at is not None

    @property
    def prize_given(self) -> bool:
        return self.prize_given_at is not None

    @property
    def is_hidden(self) -> bool:
        return self.hidden_at is not None

    @property
    def timed_out(self) -> bool:
        """The clock ended this game (it still has a current challenge)."""
        return self.finished_at is not None and self.current_slug is not None


class GameTicket(models.Model):
    """A permanent one-time credential issued after a valid booth QR scan."""

    code = models.CharField(max_length=5, unique=True, editable=False, default=generate_ticket_code)
    created_at = models.DateTimeField(auto_now_add=True)
    game = models.OneToOneField(
        GameSession, null=True, blank=True, editable=False, on_delete=models.PROTECT,
        related_name='ticket',
    )

    def __str__(self):
        return self.code


class Attempt(models.Model):
    """One counted command run. Runs that hit our own bugs (error_internal) are logged, not stored."""

    game = models.ForeignKey(GameSession, on_delete=models.CASCADE, related_name='attempt_set')
    slug = models.CharField(max_length=64)
    command = models.TextField()
    correct = models.BooleanField()
    output = models.TextField()
    error = models.CharField(max_length=255, blank=True)
    timed_out = models.BooleanField()
    duration_ms = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [models.Index(fields=['game', 'created_at'])]

    def __str__(self):
        return f'{self.slug}: {self.command[:40]}'


class GateSettings(models.Model):
    """Staff-editable QR start gate settings. Singleton: the only row is ``pk=1``."""

    token_ttl_s = models.PositiveIntegerField()  # 0 = tokens never expire
    updated_at = models.DateTimeField(auto_now=True)


class GameSettings(models.Model):
    """Staff-editable game settings. Singleton: the only row is ``pk=1``."""

    correct_answer_bonus_s = models.PositiveIntegerField(default=15)
    updated_at = models.DateTimeField(auto_now=True)
