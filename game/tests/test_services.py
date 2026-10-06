import re
import threading
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone

from challenges import catalog, sandbox
from challenges.tests.fakes import write_excluded
from game import services
from game.models import Attempt, GameSession, GameSettings, GameTicket, GateSettings
from game.tests.fakes import result


class ServiceTestCase(TestCase):
    def setUp(self):
        catalog.clear_cache()
        self.addCleanup(catalog.clear_cache)
        services._reset_semaphore()
        self.addCleanup(services._reset_semaphore)
        reap = mock.patch.object(sandbox, 'reap_stale_once')
        self.reap = reap.start()
        self.addCleanup(reap.stop)
        run = mock.patch.object(sandbox, 'run_command', return_value=result())
        self.run_command = run.start()
        self.addCleanup(run.stop)
        self.order = [c.slug for c in catalog.main_set()]

    def cut(self, *slugs):
        ctx = override_settings(CHALLENGES_EXCLUDED=write_excluded(''.join(f'{s}: "cut"\n' for s in slugs)))
        ctx.enable()
        self.addCleanup(ctx.disable)
        catalog.clear_cache()

    def fresh(self, game):
        return GameSession.objects.get(pk=game.pk)


class StartGameTests(ServiceTestCase):
    def test_valid_nick_starts_on_first_playable_challenge(self):
        game = services.start_game('  neo  ')
        game = self.fresh(game)
        self.assertEqual(game.nick, 'neo')
        self.assertEqual(game.current_slug, 'hello_world')
        self.assertEqual((game.attempts, game.solved), (0, 0))
        self.assertIsNotNone(game.started_at)
        self.assertFalse(game.is_finished)

    def test_empty_too_long_and_disallowed_nicks_are_rejected(self):
        bad = ('', '   ', 'x' * 65, 'ab cd', 'abc-def', 'zażółć', 'neo!', '<b>x</b>', 'a\nb', '٣')
        for nick in bad:
            with self.assertRaises(ValueError, msg=repr(nick)):
                services.start_game(nick)
        self.assertEqual(GameSession.objects.count(), 0)

    def test_valid_nicks_are_accepted_and_stripped(self):
        for nick in ('x' * 64, 'Neo_42', '_', '123', 'neo.smith+tag@example.com', 'a+b@c.d'):
            self.assertEqual(services.start_game(nick).nick, nick)
        self.assertEqual(services.start_game('  neo_1  ').nick, 'neo_1')


class PrizeCodeTests(ServiceTestCase):
    def test_started_game_has_six_digit_code(self):
        game = self.fresh(services.start_game('neo'))
        self.assertRegex(game.code, r'^\d{6}$')

    def test_leading_zeros_are_preserved(self):
        with mock.patch.object(services, 'generate_code', return_value='000042'):
            game = services.start_game('neo')
        self.assertEqual(self.fresh(game).code, '000042')

    def test_collision_is_retried_and_transaction_stays_usable(self):
        first = services.start_game('a')
        with mock.patch.object(services, 'generate_code', side_effect=[first.code, '123456']):
            game = services.start_game('b')
        self.assertEqual(self.fresh(game).code, '123456')
        self.assertEqual(GameSession.objects.count(), 2)

    def test_exhausted_retries_raise_and_create_nothing(self):
        first = services.start_game('a')
        with mock.patch.object(services, 'generate_code', return_value=first.code):
            with self.assertRaises(RuntimeError):
                services.start_game('b')
        self.assertEqual(GameSession.objects.count(), 1)

    def test_duplicate_code_violates_db_constraint(self):
        first = services.start_game('a')
        now = timezone.now()
        with self.assertRaises(IntegrityError):
            GameSession.objects.create(nick='b', deadline_at=now, code=first.code)


class GameTicketTests(ServiceTestCase):
    def test_issued_code_uses_only_unambiguous_alphabet(self):
        for _ in range(20):
            ticket = services.issue_game_ticket()
            self.assertRegex(ticket.code, r'^[ACDEFHJKMNPQRTUVWXY3479]{5}$')
            self.assertFalse(set(ticket.code) & set('O0I1LB8G6S5Z2'))

    def test_normalization_is_case_insensitive_and_ignores_spaces(self):
        ticket = GameTicket.objects.create(code='ACD39')
        self.assertEqual(services.normalize_ticket_code(' acd 39 '), ticket.code)
        self.assertEqual(services.find_available_ticket('acd39'), ticket)
        for bad in ('', 'ACD3', 'ACD399', 'ACDI9', 'ACDL9', 'ACD09', 'ACD-9'):
            self.assertIsNone(services.normalize_ticket_code(bad))

    def test_code_collision_is_retried_and_used_codes_are_retained(self):
        GameTicket.objects.create(code='ACD39')
        with mock.patch.object(services, 'generate_ticket_code', side_effect=['ACD39', 'FHJ47']):
            ticket = services.issue_game_ticket()
        self.assertEqual(ticket.code, 'FHJ47')
        self.assertEqual(GameTicket.objects.count(), 2)

    def test_start_consumes_ticket_once_and_links_game(self):
        ticket = services.issue_game_ticket()
        game, claimed = services.start_game_with_ticket(ticket.pk, ' neo ')
        self.assertEqual(game.nick, 'neo')
        self.assertEqual(claimed.game_id, game.pk)
        again, claimed = services.start_game_with_ticket(ticket.pk, 'other')
        self.assertIsNone(again)
        self.assertEqual(claimed.game_id, game.pk)
        self.assertEqual(GameSession.objects.count(), 1)

    def test_invalid_nick_does_not_consume_ticket(self):
        ticket = services.issue_game_ticket()
        with self.assertRaises(ValueError):
            services.start_game_with_ticket(ticket.pk, 'bad nick')
        ticket.refresh_from_db()
        self.assertIsNone(ticket.game_id)


class RankingFixtureMixin(ServiceTestCase):
    def make(self, solved=0, attempts=0, elapsed=None, finished=True, deadline_in=300):
        now = timezone.now()
        start = now - timedelta(seconds=1000)
        game = services.start_game('p')
        GameSession.objects.filter(pk=game.pk).update(
            solved=solved, attempts=attempts, started_at=start,
            deadline_at=now + timedelta(seconds=deadline_in),
            last_solved_at=start + timedelta(seconds=elapsed) if elapsed is not None else None,
            finished_at=now - timedelta(seconds=1) if finished else None)
        return self.fresh(game)

    def place(self, game):
        return services.rank_of(game)[0]


class RankingTests(RankingFixtureMixin):
    def test_orders_by_solved_then_attempts_then_elapsed(self):
        a = self.make(solved=3, attempts=9, elapsed=100)
        b = self.make(solved=2, attempts=2, elapsed=10)
        c = self.make(solved=3, attempts=5, elapsed=300)
        d = self.make(solved=3, attempts=5, elapsed=200)
        self.assertEqual([self.place(g) for g in (c, d, a, b)], [2, 1, 3, 4])
        self.assertEqual([g.pk for g in services.ranked_games()], [d.pk, c.pk, a.pk, b.pk])

    def test_ties_share_a_place_and_next_is_skipped(self):
        a = self.make(solved=2, attempts=3, elapsed=50)
        b = self.make(solved=2, attempts=3, elapsed=50)
        c = self.make(solved=3, attempts=3, elapsed=50)
        d = self.make(solved=1, attempts=1, elapsed=5)
        self.assertEqual([self.place(g) for g in (c, a, b, d)], [1, 2, 2, 4])

    def test_zero_solved_games_rank_by_attempts_only(self):
        a = self.make(attempts=4)
        b = self.make(attempts=4)
        c = self.make(attempts=1)
        d = self.make(solved=1, attempts=50, elapsed=999)
        self.assertEqual([self.place(g) for g in (d, c, a, b)], [1, 2, 3, 3])

    def test_unfinished_games_are_excluded(self):
        a = self.make(solved=1, attempts=1, elapsed=5)
        live = self.make(solved=5, attempts=1, elapsed=5, finished=False)
        self.assertIsNone(services.rank_of(live))
        self.assertEqual(services.rank_of(a), (1, 1))

    def test_overdue_unfinished_game_is_expired_and_counted(self):
        a = self.make(solved=1, attempts=1, elapsed=5)
        overdue = self.make(solved=2, attempts=2, elapsed=5, finished=False, deadline_in=-5)
        self.assertEqual(services.rank_of(a), (2, 2))
        self.assertTrue(self.fresh(overdue).is_finished)

    def test_timed_out_and_all_solved_games_are_ranked(self):
        timed_out = self.make(solved=1, attempts=2, elapsed=5)
        done = self.make(solved=3, attempts=4, elapsed=5)
        GameSession.objects.filter(pk=done.pk).update(current_slug=None)
        self.assertEqual(services.rank_of(self.fresh(timed_out)), (2, 2))
        self.assertEqual(services.rank_of(self.fresh(done)), (1, 2))


class HiddenAndHallTests(RankingFixtureMixin):
    def test_hidden_game_is_unranked_and_others_move_up(self):
        a = self.make(solved=3, attempts=1, elapsed=10)
        b = self.make(solved=2, attempts=1, elapsed=10)
        self.assertEqual(services.rank_of(b), (2, 2))
        services.hide_game(a.pk)
        self.assertIsNone(services.rank_of(self.fresh(a)))
        self.assertEqual(services.rank_of(b), (1, 1))
        services.unhide_game(a.pk)
        self.assertEqual(services.rank_of(b), (2, 2))

    def test_hide_and_unhide_are_idempotent_and_keep_result(self):
        a = self.make(solved=2, attempts=4, elapsed=10)
        GameSession.objects.filter(pk=a.pk).update(prize_given_at=timezone.now())
        a = self.fresh(a)
        now = timezone.now()
        game, changed = services.hide_game(a.pk, now=now)
        self.assertTrue(changed)
        self.assertTrue(game.is_hidden)
        game, changed = services.hide_game(a.pk, now=now + timedelta(seconds=9))
        self.assertFalse(changed)
        self.assertEqual(game.hidden_at, now)
        after = self.fresh(a)
        self.assertEqual((after.solved, after.attempts, after.code, after.prize_given_at),
                         (a.solved, a.attempts, a.code, a.prize_given_at))
        self.assertEqual(services.unhide_game(a.pk)[1], True)
        self.assertEqual(services.unhide_game(a.pk)[1], False)
        self.assertIsNone(self.fresh(a).hidden_at)

    def test_unknown_or_malformed_id_raises_does_not_exist(self):
        import uuid
        for bad in (uuid.uuid4(), 'not-a-uuid', ''):
            for fn in (services.hide_game, services.unhide_game):
                with self.assertRaises(GameSession.DoesNotExist):
                    fn(bad)

    def test_hall_of_fame_top_recent_and_exclusions(self):
        g = [self.make(solved=3, attempts=3, elapsed=50),
             self.make(solved=2, attempts=3, elapsed=50),
             self.make(solved=2, attempts=3, elapsed=50),
             self.make(solved=1, attempts=1, elapsed=5),
             self.make(attempts=2)]
        for i, game in enumerate(g):  # finish order: g[0] oldest ... g[4] newest
            GameSession.objects.filter(pk=game.pk).update(
                finished_at=timezone.now() - timedelta(minutes=10 - i))
        hidden = self.make(solved=5, attempts=1, elapsed=1)
        services.hide_game(hidden.pk)
        self.make(solved=5, attempts=1, elapsed=1, finished=False)
        overdue = self.make(solved=1, attempts=9, elapsed=5, finished=False, deadline_in=-5)
        board = services.hall_of_fame(top_n=4, recent_n=3)
        self.assertEqual(board.ranked_total, 6)
        self.assertEqual([r.place for r in board.top], [1, 2, 2, 4])
        self.assertEqual(len(board.top), 4)
        self.assertNotIn(hidden.pk, [r.game_id for r in board.top + board.recent])
        self.assertEqual(len(board.recent), 3)
        self.assertEqual(board.recent[0].game_id, overdue.pk)  # expired just now -> deadline is newest
        self.assertEqual([r.game_id for r in board.recent[1:]], [g[4].pk, g[3].pk])

    def test_hall_places_match_rank_of(self):
        games = [self.make(solved=s, attempts=a, elapsed=e) for s, a, e in
                 [(2, 3, 50), (2, 3, 50), (3, 9, 1), (0, 4, None), (0, 4, None), (0, 1, None), (1, 1, 5)]]
        board = services.hall_of_fame(top_n=100, recent_n=100)
        places = {r.game_id: r.place for r in board.top}
        self.assertEqual(len(places), len(games))
        for game in games:
            self.assertEqual(places[game.pk], services.rank_of(game)[0])
        self.assertEqual({r.game_id: r.place for r in board.recent}, places)

    def test_hidden_games_lists_newest_hidden_first(self):
        a, b = self.make(solved=1), self.make(solved=1)
        now = timezone.now()
        services.hide_game(a.pk, now=now)
        services.hide_game(b.pk, now=now + timedelta(seconds=5))
        self.assertEqual([g.pk for g in services.hidden_games()], [b.pk, a.pk])


class SubmitCommandTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.game = services.start_game('neo')

    def test_incorrect_command_is_counted_and_stays(self):
        deadline = self.fresh(self.game).deadline_at
        self.run_command.return_value = result(False, output='nope\n', error='Test failed')
        outcome = services.submit_command(self.game.id, 'ls')
        self.assertEqual(outcome.status, 'ran')
        self.assertFalse(outcome.result.correct)
        game = self.fresh(self.game)
        self.assertEqual((game.attempts, game.solved, game.current_slug), (1, 0, 'hello_world'))
        self.assertEqual(game.deadline_at, deadline)
        attempt = Attempt.objects.get()
        self.assertEqual((attempt.slug, attempt.command, attempt.output, attempt.error),
                         ('hello_world', 'ls', 'nope\n', 'Test failed'))
        self.assertEqual(attempt.duration_ms, 120)

    def test_correct_command_counts_solves_and_advances(self):
        deadline = self.fresh(self.game).deadline_at
        self.run_command.return_value = result(True, output='hello world\n')
        outcome = services.submit_command(self.game.id, 'echo hello world')
        self.assertEqual(outcome.status, 'ran')
        game = self.fresh(self.game)
        self.assertEqual((game.attempts, game.solved), (1, 1))
        self.assertIsNotNone(game.last_solved_at)
        self.assertEqual(game.current_slug, self.order[1])
        self.assertEqual(game.deadline_at, deadline + timedelta(seconds=15))
        self.assertEqual(outcome.game.current_slug, self.order[1])
        # the command reaches the sandbox verbatim, for the challenge it was on
        ch, cmd = self.run_command.call_args.args
        self.assertEqual((ch.slug, cmd), ('hello_world', 'echo hello world'))

    def test_solving_the_last_challenge_finishes_the_game(self):
        deadline = self.fresh(self.game).deadline_at
        GameSession.objects.filter(pk=self.game.pk).update(current_slug=self.order[-1])
        self.run_command.return_value = result(True)
        services.submit_command(self.game.id, 'x')
        game = self.fresh(self.game)
        self.assertIsNone(game.current_slug)
        self.assertIsNotNone(game.finished_at)
        self.assertTrue(game.is_finished)
        self.assertEqual(game.solved, 1)
        self.assertEqual(game.deadline_at, deadline)

    def test_submit_on_finished_game_is_not_counted(self):
        GameSession.objects.filter(pk=self.game.pk).update(current_slug=self.order[-1])
        self.run_command.return_value = result(True)
        services.submit_command(self.game.id, 'x')
        outcome = services.submit_command(self.game.id, 'y')
        self.assertEqual(outcome.status, 'finished')
        self.assertEqual(self.fresh(self.game).attempts, 1)
        self.assertEqual(self.run_command.call_count, 1)

    def test_rejections_are_not_counted_and_never_reach_the_sandbox(self):
        for cmd, status in (('   ', 'empty'), ('', 'empty'), ('x' * 301, 'too_long')):
            self.assertEqual(services.submit_command(self.game.id, cmd).status, status)
        self.run_command.assert_not_called()
        self.assertEqual(self.fresh(self.game).attempts, 0)
        self.assertEqual(Attempt.objects.count(), 0)

    def test_sandbox_unavailable_is_not_counted(self):
        self.run_command.side_effect = sandbox.SandboxUnavailable('daemon down')
        self.assertEqual(services.submit_command(self.game.id, 'ls').status, 'unavailable')
        self.assertEqual(self.fresh(self.game).attempts, 0)

    def test_internal_error_is_not_counted_stored_or_shown_and_is_logged(self):
        self.run_command.return_value = result(error_internal='sandbox exited with status 2')
        with self.assertLogs('game', level='ERROR') as logs:
            outcome = services.submit_command(self.game.id, 'ls -la')
        self.assertEqual(outcome.status, 'internal')
        self.assertIn('hello_world', logs.output[0])
        self.assertIn('ls -la', logs.output[0])
        self.assertEqual(self.fresh(self.game).attempts, 0)
        self.assertEqual(Attempt.objects.count(), 0)

    def test_timed_out_run_is_counted(self):
        self.run_command.return_value = result(timed_out=True, duration_s=5.2)
        outcome = services.submit_command(self.game.id, 'sleep 60')
        self.assertEqual(outcome.status, 'ran')
        self.assertEqual(self.fresh(self.game).attempts, 1)
        self.assertTrue(Attempt.objects.get().timed_out)

    def test_reaps_orphans_before_running(self):
        services.submit_command(self.game.id, 'ls')
        self.reap.assert_called()

    @override_settings(SANDBOX_MAX_CONCURRENT=1, SANDBOX_QUEUE_TIMEOUT_S=0.01)
    def test_busy_when_no_sandbox_slot_frees_up_in_time(self):
        services._reset_semaphore()
        sem = services._get_semaphore()
        self.assertTrue(sem.acquire(timeout=1))
        try:
            outcome = services.submit_command(self.game.id, 'ls')
        finally:
            sem.release()
        self.assertEqual(outcome.status, 'busy')
        self.run_command.assert_not_called()
        self.assertEqual(self.fresh(self.game).attempts, 0)

    @override_settings(SANDBOX_MAX_CONCURRENT=1, SANDBOX_QUEUE_TIMEOUT_S=0.01)
    def test_slot_is_released_when_sandbox_is_unavailable(self):
        services._reset_semaphore()
        self.run_command.side_effect = [sandbox.SandboxUnavailable('down'), result()]
        self.assertEqual(services.submit_command(self.game.id, 'ls').status, 'unavailable')
        self.assertEqual(services.submit_command(self.game.id, 'ls').status, 'ran')

    def test_duplicate_correct_submit_advances_exactly_once(self):
        # The second submit is loaded on the same slug before the first one commits.
        calls = []

        def run(challenge, command):
            calls.append(challenge.slug)
            if len(calls) == 1:
                services.submit_command(self.game.id, command)
            return result(True)

        self.run_command.side_effect = run
        deadline = self.fresh(self.game).deadline_at
        services.submit_command(self.game.id, 'echo hello world')
        self.assertEqual(calls, ['hello_world', 'hello_world'])
        game = self.fresh(self.game)
        self.assertEqual((game.attempts, game.solved, game.current_slug), (2, 1, self.order[1]))
        self.assertEqual(game.deadline_at, deadline + timedelta(seconds=15))

    def test_run_that_overlaps_the_game_finishing_is_neither_stored_nor_counted(self):
        # Only an all-solved finish closes the game to in-flight runs: a timeout mid-run still counts
        # (sent-before rule), see TimeLimitTests.
        def run(challenge, command):
            GameSession.objects.filter(pk=self.game.pk).update(current_slug=None, finished_at=timezone.now())
            return result(False, output='late')

        self.run_command.side_effect = run
        outcome = services.submit_command(self.game.id, 'ls')
        self.assertEqual(outcome.status, 'finished')
        self.assertTrue(outcome.game.is_finished)
        self.assertEqual(Attempt.objects.count(), 0)
        self.assertEqual(self.fresh(self.game).attempts, 0)

    def test_last_attempt_is_the_most_recent_run(self):
        self.assertIsNone(services.last_attempt(self.game))
        self.run_command.side_effect = [result(output='one'), result(output='two')]
        services.submit_command(self.game.id, 'a')
        services.submit_command(self.game.id, 'b')
        self.assertEqual(services.last_attempt(self.game).command, 'b')


class CutMidGameTests(ServiceTestCase):
    def test_cut_current_slug_resolves_to_next_playable_and_persists(self):
        game = services.start_game('neo')
        GameSession.objects.filter(pk=game.pk).update(current_slug=self.order[1])
        self.cut(self.order[1])
        ch = services.current_challenge(self.fresh(game))
        self.assertEqual(ch.slug, self.order[2])
        self.assertEqual(self.fresh(game).current_slug, self.order[2])

    def test_cut_last_slug_finishes_the_game_without_a_solve(self):
        game = services.start_game('neo')
        GameSession.objects.filter(pk=game.pk).update(current_slug=self.order[-1])
        self.cut(self.order[-1])
        self.assertIsNone(services.current_challenge(self.fresh(game)))
        game = self.fresh(game)
        self.assertIsNone(game.current_slug)
        self.assertIsNotNone(game.finished_at)
        self.assertEqual(game.solved, 0)
        self.assertEqual(services.submit_command(game.id, 'ls').status, 'finished')


def past(game_id, seconds=1):
    """Move a game's deadline into the past (no time mocking)."""
    GameSession.objects.filter(pk=game_id).update(deadline_at=timezone.now() - timedelta(seconds=seconds))


class TimeLimitTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.game = services.start_game('neo')

    def test_start_sets_deadline_from_started_at_and_setting(self):
        game = self.fresh(self.game)
        self.assertEqual(game.deadline_at, game.started_at + timedelta(seconds=300))
        with override_settings(GAME_DURATION_S=42):
            other = self.fresh(services.start_game('trinity'))
        self.assertEqual(other.deadline_at, other.started_at + timedelta(seconds=42))

    def test_submit_after_deadline_is_time_up_and_not_counted(self):
        past(self.game.pk)
        outcome = services.submit_command(self.game.id, 'ls')
        self.assertEqual(outcome.status, 'time_up')
        self.run_command.assert_not_called()
        game = self.fresh(self.game)
        self.assertEqual(game.attempts, 0)
        self.assertEqual(Attempt.objects.count(), 0)
        self.assertEqual(game.finished_at, game.deadline_at)
        self.assertEqual(game.current_slug, 'hello_world')
        self.assertTrue(game.timed_out)
        # checked before validation
        self.assertEqual(services.submit_command(self.game.id, '').status, 'time_up')

    def test_all_solved_game_after_deadline_says_finished(self):
        GameSession.objects.filter(pk=self.game.pk).update(
            current_slug=None, finished_at=timezone.now())
        past(self.game.pk)
        self.assertEqual(services.submit_command(self.game.id, 'y').status, 'finished')
        self.run_command.assert_not_called()

    def test_sent_before_recorded_after_is_counted_and_bonus_keeps_game_open(self):
        def run(challenge, command):
            past(self.game.pk)
            return result(True)

        self.run_command.side_effect = run
        outcome = services.submit_command(self.game.id, 'echo hello world')
        self.assertEqual(outcome.status, 'ran')
        game = self.fresh(self.game)
        self.assertEqual((game.attempts, game.solved, game.current_slug), (1, 1, self.order[1]))
        self.assertLessEqual(game.last_solved_at, game.deadline_at)
        self.assertIsNone(game.finished_at)
        self.assertFalse(outcome.game.is_finished)
        self.assertEqual(Attempt.objects.count(), 1)

    def test_correct_run_reopens_timeout_recorded_while_command_was_running(self):
        def run(challenge, command):
            past(self.game.pk)
            services.expire_overdue(self.game.pk)
            return result(True)

        self.run_command.side_effect = run
        outcome = services.submit_command(self.game.id, 'echo hello world')
        self.assertEqual(outcome.status, 'ran')
        game = self.fresh(self.game)
        self.assertEqual((game.attempts, game.solved), (1, 1))
        self.assertIsNone(game.finished_at)
        self.assertGreater(game.deadline_at, timezone.now())

    def test_grace_solve_of_last_challenge_is_stamped_at_deadline(self):
        GameSession.objects.filter(pk=self.game.pk).update(current_slug=self.order[-1])

        def run(challenge, command):
            past(self.game.pk)
            return result(True)

        self.run_command.side_effect = run
        services.submit_command(self.game.id, 'x')
        game = self.fresh(self.game)
        self.assertIsNone(game.current_slug)
        self.assertEqual(game.finished_at, game.deadline_at)
        self.assertEqual(game.last_solved_at, game.deadline_at)
        self.assertFalse(game.timed_out)

    def test_timed_out_property(self):
        past(self.game.pk)
        services.expire_overdue(self.game.pk)
        self.assertTrue(self.fresh(self.game).timed_out)
        self.assertFalse(self.game.timed_out)  # unfinished, stale instance
        GameSession.objects.filter(pk=self.game.pk).update(current_slug=None)
        self.assertFalse(self.fresh(self.game).timed_out)

    def test_expire_overdue_only_touches_overdue_unfinished_games(self):
        overdue = services.start_game('a')
        in_time = services.start_game('b')
        done = services.start_game('c')
        past(overdue.pk)
        past(done.pk)
        stamp = timezone.now() - timedelta(minutes=10)
        GameSession.objects.filter(pk=done.pk).update(finished_at=stamp)
        self.assertEqual(services.expire_overdue(), 1)
        self.assertEqual(self.fresh(overdue).finished_at, self.fresh(overdue).deadline_at)
        self.assertIsNone(self.fresh(in_time).finished_at)
        self.assertEqual(self.fresh(done).finished_at, stamp)
        self.assertEqual(services.expire_overdue(), 0)  # idempotent

    def test_remaining_ms_is_clamped_and_zero_when_finished(self):
        now = self.fresh(self.game).started_at
        self.assertEqual(services.remaining_ms(self.fresh(self.game), now=now), 300_000)
        self.assertEqual(services.remaining_ms(self.fresh(self.game), now=now + timedelta(seconds=100.5)), 199_500)
        self.assertEqual(services.remaining_ms(self.fresh(self.game), now=now + timedelta(hours=1)), 0)
        GameSession.objects.filter(pk=self.game.pk).update(finished_at=now)
        self.assertEqual(services.remaining_ms(self.fresh(self.game), now=now), 0)

    def test_cut_finish_does_not_overwrite_timeout_stamp(self):
        GameSession.objects.filter(pk=self.game.pk).update(current_slug=self.order[-1])
        self.cut(self.order[-1])
        past(self.game.pk)
        services.expire_overdue(self.game.pk)
        stamped = self.fresh(self.game)
        services.current_challenge(stamped)
        self.assertEqual(self.fresh(self.game).finished_at, stamped.deadline_at)


class PrizeTests(ServiceTestCase):
    CODE = '987654'

    def finished(self, **kw):
        game = services.start_game('neo')
        now = timezone.now()
        fields = dict(code=self.CODE, finished_at=now, solved=1, last_solved_at=now)
        fields.update(kw)
        GameSession.objects.filter(pk=game.pk).update(**fields)
        return self.fresh(game)

    def test_normalize_code(self):
        for raw, want in [('987654', '987654'), (' 987 654 ', '987654'), ('000042', '000042'),
                          ('12345', None), ('1234567', None), ('abcdef', None), ('', None)]:
            self.assertEqual(services.normalize_code(raw), want, raw)

    def test_find_by_code(self):
        game = self.finished()
        self.assertEqual(services.find_by_code(self.CODE), game)
        self.assertIsNone(services.find_by_code('111111'))

    def test_find_by_code_expires_overdue_game(self):
        game = services.start_game('neo')
        GameSession.objects.filter(pk=game.pk).update(
            code=self.CODE, deadline_at=timezone.now() - timedelta(seconds=1))
        self.assertTrue(services.find_by_code(self.CODE).is_finished)

    def test_mark_prize_given_only_once(self):
        game = self.finished()
        first, marked = services.mark_prize_given(game.pk)
        self.assertTrue(marked)
        self.assertIsNotNone(first.prize_given_at)
        self.assertTrue(first.prize_given)
        second, marked = services.mark_prize_given(game.pk)
        self.assertFalse(marked)
        self.assertEqual(second.prize_given_at, first.prize_given_at)

    def test_mark_prize_refused_for_unfinished_game(self):
        game = services.start_game('neo')
        result_game, marked = services.mark_prize_given(game.pk)
        self.assertFalse(marked)
        self.assertIsNone(result_game.prize_given_at)

    def test_mark_prize_expires_overdue_game_first(self):
        game = services.start_game('neo')
        GameSession.objects.filter(pk=game.pk).update(deadline_at=timezone.now() - timedelta(seconds=1))
        result_game, marked = services.mark_prize_given(game.pk)
        self.assertTrue(marked)
        self.assertTrue(result_game.is_finished)

    def test_mark_prize_unknown_id_raises(self):
        import uuid
        with self.assertRaises(GameSession.DoesNotExist):
            services.mark_prize_given(uuid.uuid4())

    def test_solve_time(self):
        game = services.start_game('neo')
        self.assertIsNone(services.solve_time(game))
        game.last_solved_at = game.started_at + timedelta(seconds=75)
        self.assertEqual(services.solve_time(game), timedelta(seconds=75))


class GameSettingsTests(TestCase):
    def test_default_correct_answer_bonus_is_15_seconds(self):
        self.assertEqual(services.game_settings().correct_answer_bonus_s, 15)

    def test_set_correct_answer_bonus(self):
        for seconds in (0, 42, services.CORRECT_ANSWER_BONUS_MAX_S):
            self.assertEqual(services.set_correct_answer_bonus(seconds).correct_answer_bonus_s, seconds)
        self.assertEqual(GameSettings.objects.count(), 1)

    def test_set_correct_answer_bonus_rejects_out_of_range_values(self):
        before = services.game_settings().correct_answer_bonus_s
        for seconds in (-1, services.CORRECT_ANSWER_BONUS_MAX_S + 1):
            with self.assertRaises(ValueError):
                services.set_correct_answer_bonus(seconds)
        self.assertEqual(services.game_settings().correct_answer_bonus_s, before)


class StartTokenTests(TestCase):
    NOW = timezone.now().replace(microsecond=0)

    def at(self, seconds):
        return self.NOW + timedelta(seconds=seconds)

    def test_token_is_six_digits(self):
        self.assertRegex(services.issue_start_token(self.NOW), r'^\d{6}$')

    def test_token_typed_with_spaces_is_ok(self):
        token = services.issue_start_token(self.NOW)
        spaced = f'{token[:3]} {token[3:]}'
        self.assertEqual(services.check_start_token(spaced, self.NOW), services.TOKEN_OK)

    def test_zero_ttl_uses_one_fixed_code(self):
        services.set_token_ttl(0)
        self.assertEqual(services.issue_start_token(self.NOW), services.issue_start_token(self.at(10 ** 6)))

    def test_wrong_code_is_invalid(self):
        good = services.issue_start_token(self.NOW)
        wrong = f'{(int(good) + 1) % 10 ** 6:06d}'
        self.assertEqual(services.check_start_token(wrong, self.NOW), services.TOKEN_INVALID)

    def test_fresh_token_is_ok(self):
        token = services.issue_start_token(self.NOW)
        self.assertEqual(services.check_start_token(token, self.NOW), services.TOKEN_OK)

    def test_token_is_stable_within_a_rotation_period_and_changes_after(self):
        rotate = 60
        epoch = int(self.NOW.timestamp())
        start = self.NOW - timedelta(seconds=epoch % rotate)
        a = services.issue_start_token(start)
        self.assertEqual(a, services.issue_start_token(start + timedelta(seconds=rotate - 1)))
        self.assertNotEqual(a, services.issue_start_token(start + timedelta(seconds=rotate)))

    def test_expires_just_after_ttl_but_not_at_ttl(self):
        services.set_token_ttl(300)
        token = services.issue_start_token(self.NOW)
        issued = int(self.NOW.timestamp()) // 60 * 60
        at_ttl = datetime.fromtimestamp(issued + 300, tz=dt_timezone.utc)
        self.assertEqual(services.check_start_token(token, at_ttl), services.TOKEN_OK)
        self.assertEqual(services.check_start_token(token, at_ttl + timedelta(seconds=1)),
                         services.TOKEN_EXPIRED)

    def test_ttl_zero_never_expires(self):
        services.set_token_ttl(0)
        token = services.issue_start_token(self.NOW)
        self.assertEqual(services.check_start_token(token, self.at(10 ** 7)), services.TOKEN_OK)

    def test_lowering_ttl_expires_issued_token(self):
        services.set_token_ttl(900)
        token = services.issue_start_token(self.NOW)
        later = self.at(400)
        self.assertEqual(services.check_start_token(token, later), services.TOKEN_OK)
        services.set_token_ttl(120)
        self.assertEqual(services.check_start_token(token, later), services.TOKEN_EXPIRED)

    def test_missing_and_invalid_tokens(self):
        self.assertEqual(services.check_start_token(None), services.TOKEN_MISSING)
        self.assertEqual(services.check_start_token(''), services.TOKEN_MISSING)
        self.assertEqual(services.check_start_token('garbage'), services.TOKEN_INVALID)
        token = services.issue_start_token(self.NOW)
        self.assertEqual(services.check_start_token(token[:-1] + ('a' if token[-1] != 'a' else 'b'), self.NOW),
                         services.TOKEN_INVALID)

    def test_token_from_the_future_is_invalid(self):
        token = services.issue_start_token(self.at(3600))
        self.assertEqual(services.check_start_token(token, self.NOW), services.TOKEN_INVALID)

    def test_set_token_ttl_bounds(self):
        for bad in (1, 119, 86401, -1):
            with self.assertRaises(ValueError):
                services.set_token_ttl(bad)
        for good in (0, 120, 86400):
            self.assertEqual(services.set_token_ttl(good).token_ttl_s, good)

    @override_settings(START_TOKEN_TTL_S=777)
    def test_gate_settings_seeds_from_setting(self):
        self.assertEqual(services.gate_settings().token_ttl_s, 777)

    def test_gate_settings_seed_is_clamped_to_staff_bounds(self):
        for seed, expected in ((0, 0), (30, services.TOKEN_TTL_MIN_S), (10**6, services.TOKEN_TTL_MAX_S)):
            GateSettings.objects.all().delete()
            with override_settings(START_TOKEN_TTL_S=seed):
                self.assertEqual(services.gate_settings().token_ttl_s, expected)
