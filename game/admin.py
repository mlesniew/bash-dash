"""Read-only inspection of games during the event. Prize desk tooling lives at /staff."""

from django.contrib import admin

from .models import Attempt, GameSession, GameTicket


class ReadOnlyMixin:
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class AttemptInline(ReadOnlyMixin, admin.TabularInline):
    model = Attempt
    extra = 0
    fields = ('created_at', 'slug', 'command', 'correct', 'timed_out', 'error', 'duration_ms')
    readonly_fields = fields


@admin.register(GameSession)
class GameSessionAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ('nick', 'code', 'started_at', 'deadline_at', 'solved', 'attempts', 'finished_at',
                    'prize_given_at', 'hidden_at')
    list_filter = (('prize_given_at', admin.EmptyFieldListFilter), ('hidden_at', admin.EmptyFieldListFilter))
    search_fields = ('nick', 'code')
    inlines = [AttemptInline]


@admin.register(GameTicket)
class GameTicketAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ('code', 'created_at', 'game')
    search_fields = ('code', 'game__nick', 'game__code')
