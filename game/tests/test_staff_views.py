import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from challenges import catalog
from game import services
from game.models import GameSession

CODE = '987654'
TRACKING_WEBSITE_ID = 'test-site-id'


class StaffTestCase(TestCase):
    def setUp(self):
        catalog.clear_cache()
        self.staff = User.objects.create_user('booth', password='x', is_staff=True)
        self.client.force_login(self.staff)
        self.lookup = reverse('game:staff_lookup')
        self.prize = reverse('game:staff_prize')

    def make_game(self, finished=True, solved=1, attempts=3, nick='neo', code=CODE, elapsed=75, finished_ago=0):
        game = services.start_game(nick)
        fields = dict(code=code, solved=solved, attempts=attempts)
        if finished:
            now = game.started_at + timedelta(seconds=elapsed)
            fields['finished_at'] = now - timedelta(seconds=finished_ago)
            fields['last_solved_at'] = now if solved else None
        GameSession.objects.filter(pk=game.pk).update(**fields)
        return GameSession.objects.get(pk=game.pk)


class AccessTests(StaffTestCase):
    def test_anonymous_and_non_staff_are_redirected_to_login(self):
        self.make_game()
        User.objects.create_user('player', password='x')
        for setup in (lambda c: c.logout(), lambda c: c.login(username='player', password='x')):
            client = Client()
            setup(client)
            for resp in (client.get(self.lookup, {'code': CODE}), client.post(self.prize, {'code': CODE})):
                self.assertEqual(resp.status_code, 302)
                self.assertIn('/admin/login/', resp['Location'])
            self.assertIn('next=/staff', client.get(self.lookup, {'code': CODE})['Location'])
        self.assertIsNone(GameSession.objects.get(code=CODE).prize_given_at)

    def test_post_without_csrf_token_is_forbidden(self):
        self.make_game()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.post(self.prize, {'code': CODE}).status_code, 403)


class LookupTests(StaffTestCase):
    def test_no_code_shows_form_only(self):
        resp = self.client.get(self.lookup)
        self.assertContains(resp, 'name="code"')
        self.assertContains(resp, 'inputmode="numeric"')
        self.assertNotContains(resp, 'staff-card')

    def test_malformed_and_unknown_codes(self):
        self.assertContains(self.client.get(self.lookup, {'code': '12'}), 'Enter a 6-digit code')
        self.assertContains(self.client.get(self.lookup, {'code': '111111'}), 'No game with code')

    def test_code_with_spaces_finds_game(self):
        self.make_game()
        self.assertContains(self.client.get(self.lookup, {'code': '987 654'}), 'neo')

    def test_finished_game_shows_result_and_button(self):
        self.make_game()
        resp = self.client.get(self.lookup, {'code': CODE})
        for text in ('neo', 'Solved', 'Attempts', '#1 of 1', '1:15', 'Mark prize given'):
            self.assertContains(resp, text)

    def test_unfinished_game_shows_in_progress_and_no_button(self):
        self.make_game(finished=False)
        resp = self.client.get(self.lookup, {'code': CODE})
        self.assertContains(resp, 'Game in progress')
        self.assertNotContains(resp, 'Mark prize given')
        self.assertNotContains(resp, 'not ranked')

    def test_finished_game_with_no_solves_shows_dash(self):
        self.make_game(solved=0)
        resp = self.client.get(self.lookup, {'code': CODE})
        self.assertRegex(resp.content.decode(), r'Solve time</span>\s*<span class="leader"></span>\s*<strong>—</strong>')

    def test_unranked_finished_game_keeps_button(self):
        self.make_game()
        with mock.patch.object(services, 'rank_of', return_value=None):
            resp = self.client.get(self.lookup, {'code': CODE})
        self.assertContains(resp, 'not ranked')
        self.assertContains(resp, 'Mark prize given')


class PrizeTests(StaffTestCase):
    def test_post_marks_once_and_redirects(self):
        self.make_game()
        resp = self.client.post(self.prize, {'code': CODE})
        self.assertRedirects(resp, f'{self.lookup}?code={CODE}', fetch_redirect_response=False)
        stamp = GameSession.objects.get(code=CODE).prize_given_at
        self.assertIsNotNone(stamp)
        page = self.client.get(f'{self.lookup}?code={CODE}')
        self.assertContains(page, 'Prize given to neo')
        self.assertContains(page, 'already given')
        self.assertNotContains(page, 'Mark prize given')

        again = self.client.post(self.prize, {'code': CODE}, follow=True)
        self.assertContains(again, 'already given')
        self.assertEqual(GameSession.objects.get(code=CODE).prize_given_at, stamp)

    def test_post_for_unfinished_game_is_refused(self):
        self.make_game(finished=False)
        resp = self.client.post(self.prize, {'code': CODE}, follow=True)
        self.assertContains(resp, 'still in progress')
        self.assertIsNone(GameSession.objects.get(code=CODE).prize_given_at)

    def test_post_with_unknown_code_shows_error(self):
        resp = self.client.post(self.prize, {'code': '111111'}, follow=True)
        self.assertContains(resp, 'No game with code')


class StaffNoSponsorFooterTests(StaffTestCase):
    def test_staff_pages_have_no_player_sponsor_footer(self):
        for name in ('staff_lookup', 'staff_moderate', 'staff_hall'):
            self.assertNotContains(self.client.get(reverse(f'game:{name}')), 'class="sponsor"', msg_prefix=name)

    @override_settings(TRACKING_WEBSITE_ID=TRACKING_WEBSITE_ID)
    def test_staff_pages_and_fragments_have_no_tracking_script(self):
        for name in ('staff_lookup', 'staff_moderate', 'staff_hall', 'staff_hall_board'):
            self.assertNotContains(self.client.get(reverse(f'game:{name}')), TRACKING_WEBSITE_ID, msg_prefix=name)


class MmssTests(TestCase):
    def test_rounds_down_and_handles_none(self):
        from game.templatetags.game_text import mmss
        self.assertEqual(mmss(timedelta(seconds=74, milliseconds=900)), '1:14')
        self.assertEqual(mmss(None), '—')


class HiddenLookupTests(StaffTestCase):
    def test_hidden_game_shows_disqualified_and_keeps_prize_button(self):
        game = self.make_game()
        services.hide_game(game.pk)
        resp = self.client.get(self.lookup, {'code': CODE})
        self.assertContains(resp, 'Hidden from Hall of fame (disqualified)')
        self.assertContains(resp, 'Mark prize given')
        self.assertNotContains(resp, 'not ranked')


class HallTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.hall = reverse('game:staff_hall')
        self.board = reverse('game:staff_hall_board')

    def fixture(self):
        self.make_game(nick='ann', solved=3, attempts=5, code='111111', finished_ago=30)
        self.make_game(nick='bob', solved=2, attempts=4, code='222222', finished_ago=20)
        self.make_game(nick='cy', solved=2, attempts=4, code='333333', finished_ago=10)
        self.make_game(nick='dee', solved=1, attempts=1, code='444444', finished_ago=0)
        return ['111111', '222222', '333333', '444444']

    def test_anonymous_redirects_and_fragment_is_403(self):
        client = Client()
        resp = client.get(self.hall)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/admin/login/', resp['Location'])
        self.assertEqual(client.get(self.board).status_code, 403)
        User.objects.create_user('player', password='x')
        client.login(username='player', password='x')
        self.assertEqual(client.get(self.board).status_code, 403)

    def test_rows_in_rank_order_with_shared_places(self):
        self.fixture()
        html = self.client.get(self.board).content.decode()
        self.assertLess(html.index('ann'), html.index('bob'))
        self.assertLess(html.index('bob'), html.index('dee'))
        for text in ('Hall of fame', 'Just finished', '3 / ', '#1', '#2', '#4'):
            self.assertIn(text, html)
        self.assertNotIn('#3', html)

    def test_no_codes_and_hidden_nick_absent_on_page_and_fragment(self):
        codes = self.fixture()
        services.hide_game(GameSession.objects.get(nick='cy').pk)
        for url in (self.hall, self.board):
            html = self.client.get(url).content.decode()
            for code in codes:
                self.assertNotIn(code, html)
            self.assertNotIn('>cy<', html)
            self.assertIn('>ann<', html)

    def test_nick_is_escaped(self):
        # Bypass start_game on purpose: old rows and admin edits can still hold HTML-like nicks.
        game = self.make_game()
        GameSession.objects.filter(pk=game.pk).update(nick='<b>x</b>')
        html = self.client.get(self.board).content.decode()
        self.assertIn('&lt;b&gt;x&lt;/b&gt;', html)
        self.assertNotIn('<b>x</b>', html)

    def test_empty_states(self):
        html = self.client.get(self.hall).content.decode()
        self.assertIn('No finished games yet', html)
        self.assertIn('Nobody has finished yet', html)

    def test_hall_header_carries_large_camlin_logo_and_tagline(self):
        html = self.client.get(self.hall).content.decode()
        for needle in ('class="hall-sponsor"', 'href="https://camlingroup.com/"', 'alt="Camlin Group"',
                       'rel="noopener"', 'class="hall-brand"', 'class="hall-tagline"'):
            self.assertIn(needle, html)
        self.assertLess(html.index('class="hall-brand"'), html.index('class="hall-sponsor"'))

    def test_board_fragment_has_no_sponsor(self):
        self.assertNotContains(self.client.get(self.board), 'hall-sponsor')

    def test_fragment_is_not_cached_and_has_no_page_chrome(self):
        resp = self.client.get(self.board)
        self.assertIn('no-store', resp['Cache-Control'])
        self.assertNotContains(resp, '<html')

    @override_settings(HALL_REFRESH_S=7)
    def test_page_carries_polling_config(self):
        html = self.client.get(self.hall).content.decode()
        self.assertIn(f'data-board-url="{self.board}"', html)
        self.assertIn('data-refresh-ms="7000"', html)

    @override_settings(HALL_TOP_N=2, HALL_RECENT_N=1)
    def test_top_n_and_recent_n_truncate(self):
        self.fixture()
        html = self.client.get(self.board).content.decode()
        for nick, shown in (('ann', True), ('bob', True), ('cy', False), ('dee', True)):
            self.assertEqual(f'>{nick}<' in html, shown, nick)


class ModerationTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.moderate = reverse('game:staff_moderate')
        self.hide = reverse('game:staff_hide')
        self.unhide = reverse('game:staff_unhide')
        self.board = reverse('game:staff_hall_board')
        self.ann = self.make_game(nick='ann', solved=3, attempts=5, code='111111', finished_ago=10)
        self.bob = self.make_game(nick='bob', solved=2, attempts=4, code='222222', finished_ago=0)

    def test_anonymous_and_non_staff_are_redirected_and_nothing_changes(self):
        User.objects.create_user('player', password='x')
        for setup in (lambda c: c.logout(), lambda c: c.login(username='player', password='x')):
            client = Client()
            setup(client)
            for resp in (client.get(self.moderate), client.post(self.hide, {'game_id': self.ann.pk}),
                         client.post(self.unhide, {'game_id': self.ann.pk})):
                self.assertEqual(resp.status_code, 302)
                self.assertIn('/admin/login/', resp['Location'])
        self.assertFalse(GameSession.objects.filter(hidden_at__isnull=False).exists())

    def test_page_lists_rows_with_hide_forms_and_hidden_with_unhide(self):
        services.hide_game(self.bob.pk)
        resp = self.client.get(self.moderate)
        self.assertContains(resp, f'action="{self.hide}"')
        self.assertContains(resp, f'value="{self.ann.pk}"')
        self.assertContains(resp, f'action="{self.unhide}"')
        self.assertContains(resp, f'value="{self.bob.pk}"')
        self.assertContains(resp, 'Hall of fame moderation')
        self.assertContains(self.client.get(self.moderate), 'Unhide')

    def test_no_hidden_games_message(self):
        self.assertContains(self.client.get(self.moderate), 'No hidden games')

    def test_hide_then_unhide_flow(self):
        resp = self.client.post(self.hide, {'game_id': str(self.ann.pk)}, follow=True)
        self.assertRedirects(resp, self.moderate)
        self.assertContains(resp, 'Hidden ann.')
        self.assertTrue(GameSession.objects.get(pk=self.ann.pk).is_hidden)
        self.assertNotIn('>ann<', self.client.get(self.board).content.decode())
        resp = self.client.post(self.unhide, {'game_id': str(self.ann.pk)}, follow=True)
        self.assertContains(resp, 'ann is back on the Hall of fame.')
        self.assertIsNone(GameSession.objects.get(pk=self.ann.pk).hidden_at)
        self.assertIn('>ann<', self.client.get(self.board).content.decode())

    def test_second_hide_warns_and_keeps_timestamp(self):
        self.client.post(self.hide, {'game_id': str(self.ann.pk)})
        first = GameSession.objects.get(pk=self.ann.pk).hidden_at
        resp = self.client.post(self.hide, {'game_id': str(self.ann.pk)}, follow=True)
        self.assertContains(resp, 'already hidden')
        self.assertEqual(GameSession.objects.get(pk=self.ann.pk).hidden_at, first)
        resp = self.client.post(self.unhide, {'game_id': str(self.bob.pk)}, follow=True)
        self.assertContains(resp, 'not hidden')

    def test_unknown_and_malformed_ids_give_error_message(self):
        for url in (self.hide, self.unhide):
            for bad in ('not-a-uuid', '00000000-0000-0000-0000-000000000000', ''):
                resp = self.client.post(url, {'game_id': bad}, follow=True)
                self.assertContains(resp, 'No such game.')
        resp = self.client.post(self.hide, {}, follow=True)
        self.assertContains(resp, 'No such game.')

    def test_post_without_csrf_token_is_forbidden(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.post(self.hide, {'game_id': str(self.ann.pk)}).status_code, 403)

    def test_get_is_not_allowed_on_mutations(self):
        self.assertEqual(self.client.get(self.hide).status_code, 405)

    def test_disqualification_end_to_end_on_done(self):
        player = Client()
        player.get(reverse('game:home'), {'t': services.issue_start_token()})
        player.post(reverse('game:start'), {'nick': 'zed'})
        game = GameSession.objects.get(pk=player.session['game_id'])
        GameSession.objects.filter(pk=game.pk).update(
            code='555555', solved=1, attempts=9, last_solved_at=timezone.now(), finished_at=timezone.now())
        self.assertContains(player.get(reverse('game:done')), '#3 of 3')
        self.client.post(self.hide, {'game_id': str(self.ann.pk)})
        self.assertContains(player.get(reverse('game:done')), '#2 of 2')
        self.client.post(self.hide, {'game_id': str(game.pk)})
        resp = player.get(reverse('game:done'))
        self.assertContains(resp, 'not ranked')
        self.assertContains(resp, '555555')

    def test_lookup_links_to_hall_and_moderation(self):
        resp = self.client.get(self.lookup)
        self.assertContains(resp, f'href="{reverse("game:staff_hall")}"')
        self.assertContains(resp, f'href="{self.moderate}"')


class QrTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.hall = reverse('game:staff_hall')
        self.board = reverse('game:staff_hall_board')

    def qr_svg(self, url=None):
        html = self.client.get(url or self.board).content.decode()
        return re.search(r'<svg.*?</svg>', html, re.S).group(0)

    def test_hall_and_board_show_qr(self):
        for url in (self.hall, self.board):
            html = self.client.get(url).content.decode()
            self.assertIn('<svg', html)
            self.assertIn('Scan to play', html)

    def test_qr_is_stable_within_period_and_changes_next(self):
        rotate = 60
        start = timezone.now().replace(microsecond=0)
        start -= timedelta(seconds=int(start.timestamp()) % rotate)
        with mock.patch.object(timezone, 'now', return_value=start):
            first = self.qr_svg()
        with mock.patch.object(timezone, 'now', return_value=start + timedelta(seconds=rotate - 1)):
            self.assertEqual(self.qr_svg(), first)
        with mock.patch.object(timezone, 'now', return_value=start + timedelta(seconds=rotate)):
            self.assertNotEqual(self.qr_svg(), first)

    def test_board_shows_code_under_qr(self):
        token = services.issue_start_token()
        resp = self.client.get(reverse('game:staff_hall_board'))
        self.assertContains(resp, f'{token[:3]} {token[3:]}')

    def test_start_url_uses_public_base_url_and_token_is_valid(self):
        token = services.issue_start_token()
        request = RequestFactory().get('/')
        self.assertEqual(services.check_start_token(token), services.TOKEN_OK)
        with override_settings(PUBLIC_BASE_URL='https://dash.example'):
            url = services.start_url(request, token)
        self.assertTrue(url.startswith('https://dash.example/?t='))
        self.assertTrue(services.start_url(request, token).startswith('http://testserver/?t='))

    def test_start_url_follows_the_reverse_proxy_headers(self):
        token = services.issue_start_token()
        headers = {'HTTP_X_FORWARDED_HOST': 'dash.example', 'HTTP_X_FORWARDED_PROTO': 'https'}
        with override_settings(
            USE_X_FORWARDED_HOST=True,
            SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO', 'https'),
            ALLOWED_HOSTS=['dash.example', 'testserver'],
        ):
            url = services.start_url(RequestFactory().get('/', **headers), token)
        self.assertTrue(url.startswith('https://dash.example/?t='), url)
        # Flag off (the default): the headers are ignored, not honoured — http and the inner host.
        url = services.start_url(RequestFactory().get('/', **headers), token)
        self.assertTrue(url.startswith('http://testserver/?t='), url)

    def test_board_is_forbidden_for_non_staff(self):
        for setup in (lambda c: c.logout(), lambda c: c.force_login(User.objects.create_user('p', password='x'))):
            client = Client()
            setup(client)
            resp = client.get(self.board)
            self.assertEqual(resp.status_code, 403)
            self.assertNotIn(b'<svg', resp.content)


class TokenTtlTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('game:staff_token_ttl')
        self.moderate = reverse('game:staff_moderate')

    def test_valid_minutes_are_saved(self):
        resp = self.client.post(self.url, {'minutes': '5'}, follow=True)
        self.assertRedirects(resp, self.moderate)
        self.assertEqual(services.gate_settings().token_ttl_s, 300)
        self.assertContains(resp, 'QR codes now expire after 5 min.')

    def test_zero_means_never_and_shows_warning(self):
        resp = self.client.post(self.url, {'minutes': '0'}, follow=True)
        self.assertContains(resp, 'QR codes now never expire.')
        self.assertContains(resp, 'QR codes never expire')
        self.assertEqual(services.gate_settings().token_ttl_s, 0)

    def test_invalid_values_leave_ttl_unchanged(self):
        before = services.gate_settings().token_ttl_s
        for bad in ('1', 'abc', '-3', '1441', ''):
            resp = self.client.post(self.url, {'minutes': bad}, follow=True)
            self.assertContains(resp, 'Enter 0 or 2–1440 minutes.')
        self.assertEqual(services.gate_settings().token_ttl_s, before)

    def test_access_rules(self):
        anon = Client()
        resp = anon.post(self.url, {'minutes': '5'})
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/admin/login/', resp['Location'])
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.staff)
        self.assertEqual(csrf.post(self.url, {'minutes': '5'}).status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_moderate_shows_current_minutes(self):
        html = self.client.get(self.moderate).content.decode()
        self.assertRegex(html, r'name="minutes"[^>]*value="15"')
        self.assertNotIn('QR codes never expire', html)


class CorrectAnswerBonusTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('game:staff_correct_answer_bonus')
        self.moderate = reverse('game:staff_moderate')

    def test_valid_seconds_are_saved(self):
        resp = self.client.post(self.url, {'seconds': '42'}, follow=True)
        self.assertRedirects(resp, self.moderate)
        self.assertEqual(services.game_settings().correct_answer_bonus_s, 42)
        self.assertContains(resp, 'Correct answers now add 42 seconds.')
        self.assertRegex(resp.content.decode(), r'name="seconds"[^>]*value="42"')

    def test_zero_disables_bonus(self):
        resp = self.client.post(self.url, {'seconds': '0'}, follow=True)
        self.assertContains(resp, 'Correct answers now add 0 seconds.')
        self.assertEqual(services.game_settings().correct_answer_bonus_s, 0)

    def test_invalid_values_leave_bonus_unchanged(self):
        before = services.game_settings().correct_answer_bonus_s
        for bad in ('abc', '-1', str(services.CORRECT_ANSWER_BONUS_MAX_S + 1), ''):
            resp = self.client.post(self.url, {'seconds': bad}, follow=True)
            self.assertContains(resp, f'Enter 0–{services.CORRECT_ANSWER_BONUS_MAX_S} seconds.')
        self.assertEqual(services.game_settings().correct_answer_bonus_s, before)

    def test_access_rules(self):
        anon = Client()
        resp = anon.post(self.url, {'seconds': '15'})
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/admin/login/', resp['Location'])
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.staff)
        self.assertEqual(csrf.post(self.url, {'seconds': '15'}).status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_moderate_shows_default_bonus(self):
        self.assertRegex(self.client.get(self.moderate).content.decode(),
                         r'name="seconds"[^>]*value="15"')


class QrSvgTests(TestCase):
    def test_qr_svg_is_inline_svg_with_viewbox(self):
        svg = services.qr_svg('https://dash.example/?t=abc')
        self.assertTrue(svg.startswith('<svg'))
        self.assertIn('viewBox', svg)
