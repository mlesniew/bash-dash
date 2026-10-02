import json
import re
from datetime import timedelta
from unittest import mock

import docker.errors
import requests.exceptions

from django.contrib.staticfiles import finders
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from challenges import catalog, sandbox
from game import services, views
from game.models import GameSession, GameTicket
from game.templatetags.game_text import render_description
from game.tests.fakes import result

EXTERNAL_HREF = re.compile(r'<a\s[^>]*href\s*=\s*["\']?((?:https?:)?//[^"\'\s>]*)', re.I)
ALLOWED_EXTERNAL = 'https://camlingroup.com/'
TRACKING_WEBSITE_ID = 'test-site-id'


class ViewTestCase(TestCase):
    def setUp(self):
        catalog.clear_cache()
        services._reset_semaphore()
        for target in ('reap_stale_once', 'run_command'):
            patcher = mock.patch.object(sandbox, target)
            setattr(self, target, patcher.start())
            self.addCleanup(patcher.stop)
        self.run_command.return_value = result()
        self.order = catalog.main_set()

    def start(self, nick='neo'):
        if views.TICKET_SESSION_KEY not in self.client.session:
            self.client.get(reverse('game:home'), {'t': services.issue_start_token()})
        return self.client.post(reverse('game:start'), {'nick': nick})

    def ticket(self, client=None):
        client = client or self.client
        return GameTicket.objects.get(pk=client.session[views.TICKET_SESSION_KEY])

    def game(self):
        return GameSession.objects.get(pk=self.client.session['game_id'])

    def command(self, cmd, client=None, **extra):
        return (client or self.client).post(reverse('game:command'), json.dumps({'command': cmd}),
                                            content_type='application/json', **extra)


class HomeAndStartTests(ViewTestCase):
    def test_home_renders_rules_and_nick_form(self):
        resp = self.client.get(reverse('game:home'), {'t': services.issue_start_token()})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="nick"')
        self.assertContains(resp, f'maxlength="{services.NICK_MAX_CHARS}"')
        self.assertContains(resp, f'pattern="{services.NICK_HTML_PATTERN}"')
        self.assertContains(resp, f'max {services.NICK_MAX_CHARS}')
        self.assertContains(resp, f'Use 1-{services.NICK_MAX_CHARS} letters')
        self.assertContains(resp, 'aria-describedby="nick-hint"')
        self.assertContains(resp, 'Start')
        self.assertContains(resp, 'attempt')
        self.assertContains(resp, '5 minutes')
        self.assertContains(resp, 'Every correct answer adds <strong>15 seconds</strong>')

    def test_home_uses_configured_bonus_and_hides_disabled_bonus(self):
        token = services.issue_start_token()
        services.set_correct_answer_bonus(42)
        self.assertContains(self.client.get(reverse('game:home'), {'t': token}),
                            'Every correct answer adds <strong>42 seconds</strong>')
        services.set_correct_answer_bonus(0)
        self.assertNotContains(self.client.get(reverse('game:home'), {'t': token}),
                               'Every correct answer adds')

    def test_start_with_valid_nick_sets_session_and_redirects_to_play(self):
        resp = self.start('neo')
        self.assertRedirects(resp, reverse('game:play'))
        self.assertEqual(self.game().nick, 'neo')

    def test_start_with_invalid_nick_shows_error_and_creates_no_game(self):
        resp = self.start('   ')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="error"')
        self.assertEqual(GameSession.objects.count(), 0)
        self.assertNotIn('game_id', self.client.session)

    def test_start_with_rule_breaking_nick_shows_rule_and_keeps_input_and_ticket(self):
        self.client.get(reverse('game:home'), {'t': services.issue_start_token()})
        ticket = self.ticket()
        resp = self.client.post(reverse('game:start'), {'nick': 'bad nick!'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'Use 1-{services.NICK_MAX_CHARS} {services.NICK_CHARS_DESC} (no spaces).')
        self.assertContains(resp, 'value="bad nick!"')
        self.assertEqual(self.ticket().pk, ticket.pk)
        self.assertEqual(GameSession.objects.count(), 0)
        self.assertNotIn('game_id', self.client.session)

    def test_home_and_start_with_active_game_redirect_to_play(self):
        self.start('neo')
        self.assertRedirects(self.client.get(reverse('game:home')), reverse('game:play'))
        self.assertRedirects(self.start('again'), reverse('game:play'))
        self.assertEqual(GameSession.objects.count(), 1)


class GateTests(ViewTestCase):
    def expired_token(self):
        return services.issue_start_token(timezone.now() - timedelta(seconds=services.gate_settings().token_ttl_s + 120))

    def test_home_without_token_offers_game_code_entry(self):
        resp = self.client.get(reverse('game:home'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Continue on this computer')
        self.assertContains(resp, 'name="code"')
        self.assertNotContains(resp, 'name="nick"')

    def test_join_accepts_manual_six_digit_gate_code(self):
        token = services.issue_start_token()
        resp = self.client.post(reverse('game:join'), {'code': f'{token[:3]} {token[3:]}'})
        self.assertRedirects(resp, reverse('game:home'))
        resp = self.client.get(reverse('game:home'))
        self.assertContains(resp, 'name="nick"')
        self.assertNotContains(resp, 'Use a computer for the best chance at a high score')
        self.assertNotContains(resp, 'Or continue here')
        self.assertEqual(GameTicket.objects.count(), 1)

    def test_invalid_qr_refusal_page_has_manual_gate_code_form(self):
        bad = '000000' if services.issue_start_token() != '000000' else '111111'
        resp = self.client.get(reverse('game:home'), {'t': bad})
        self.assertContains(resp, 'name="t"', status_code=403)

    def test_home_with_wrong_code_says_invalid(self):
        resp = self.client.get(reverse('game:home'), {'t': '000000' if services.issue_start_token() != '000000' else '111111'})
        self.assertContains(resp, 'That code is not valid.', status_code=403)

    def test_manually_typed_code_with_space_opens_form(self):
        token = services.issue_start_token()
        resp = self.client.post(reverse('game:join'), {'code': f'{token[:3]} {token[3:]}'}, follow=True)
        self.assertContains(resp, 'name="nick"')

    def test_home_with_expired_token_says_expired(self):
        resp = self.client.get(reverse('game:home'), {'t': self.expired_token()})
        self.assertContains(resp, 'That code has expired.', status_code=403)

    def test_home_with_valid_token_issues_individual_ticket(self):
        token = services.issue_start_token()
        resp = self.client.get(reverse('game:home'), {'t': token})
        self.assertEqual(resp.status_code, 200)
        ticket = self.ticket()
        self.assertContains(resp, ticket.code)
        self.assertContains(resp, 'Use a computer for the best chance at a high score')
        self.assertContains(resp, 'Quotes, pipes, brackets and other special characters')
        self.assertContains(resp, 'Or continue here')
        self.assertRegex(ticket.code, r'^[ACDEFHJKMNPQRTUVWXY3479]{5}$')

    def test_start_without_ticket_creates_no_game(self):
        resp = self.client.post(reverse('game:start'), {'nick': 'neo'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'enter your game code first')
        self.assertEqual(GameSession.objects.count(), 0)

    def test_ticket_remains_valid_after_qr_token_expires(self):
        token = services.issue_start_token()
        self.client.get(reverse('game:home'), {'t': token})
        ttl = services.gate_settings().token_ttl_s
        later = timezone.now() + timedelta(seconds=ttl + 120)
        with mock.patch.object(timezone, 'now', return_value=later):
            resp = self.client.post(reverse('game:start'), {'nick': 'neo'})
        self.assertRedirects(resp, reverse('game:play'))
        self.assertEqual(GameSession.objects.count(), 1)

    def test_nick_error_keeps_ticket(self):
        self.client.get(reverse('game:home'), {'t': services.issue_start_token()})
        ticket = self.ticket()
        resp = self.client.post(reverse('game:start'), {'nick': ' '})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="error"')
        self.assertEqual(self.ticket().pk, ticket.pk)

    def test_player_with_game_resumes_without_token(self):
        self.start()
        self.assertRedirects(self.client.get(reverse('game:home')), reverse('game:play'))
        self.assertRedirects(self.client.post(reverse('game:start'), {'nick': 'x'}), reverse('game:play'))
        GameSession.objects.filter(pk=self.game().pk).update(current_slug=None, finished_at=timezone.now())
        self.assertRedirects(self.client.get(reverse('game:home')), reverse('game:done'))


class ComputerHandoffTests(ViewTestCase):
    def setUp(self):
        super().setUp()
        self.phone = self.client
        self.computer = Client()
        self.phone.get(reverse('game:home'), {'t': services.issue_start_token()})
        self.ticket_obj = self.ticket(self.phone)

    def test_refresh_reuses_same_ticket(self):
        first = self.ticket_obj
        self.phone.get(reverse('game:home'), {'t': services.issue_start_token()})
        self.assertEqual(self.ticket(self.phone).pk, first.pk)
        self.assertEqual(GameTicket.objects.count(), 1)

    def test_computer_redeems_case_insensitive_code_and_sees_start_form(self):
        code = f' {self.ticket_obj.code[:2].lower()} {self.ticket_obj.code[2:].lower()} '
        resp = self.computer.post(reverse('game:join'), {'code': code}, follow=True)
        self.assertContains(resp, 'name="nick"')
        self.assertNotContains(resp, 'Use a computer for the best chance at a high score')
        self.assertEqual(self.ticket(self.computer), self.ticket_obj)

    def test_computer_start_updates_phone_and_phone_sees_finished_summary(self):
        self.computer.post(reverse('game:join'), {'code': self.ticket_obj.code})
        resp = self.computer.post(reverse('game:start'), {'nick': 'desktop'})
        self.assertRedirects(resp, reverse('game:play'))
        game = GameSession.objects.get(pk=self.computer.session['game_id'])
        self.ticket_obj.refresh_from_db()
        self.assertEqual(self.ticket_obj.game_id, game.pk)

        self.assertEqual(self.phone.get(reverse('game:ticket_status')).json(), {'status': 'in_progress'})
        phone_home = self.phone.get(reverse('game:home'))
        self.assertContains(phone_home, 'Game in progress')
        self.assertNotContains(phone_home, 'name="command"')
        self.assertNotContains(phone_home, 'name="nick"')
        self.assertNotIn('game_id', self.phone.session)

        GameSession.objects.filter(pk=game.pk).update(finished_at=timezone.now())
        self.assertEqual(self.phone.get(reverse('game:ticket_status')).json(), {'status': 'finished'})
        summary = self.phone.get(reverse('game:done'))
        self.assertContains(summary, 'desktop')
        self.assertContains(summary, game.code)

    def test_only_first_device_can_start_and_used_code_cannot_be_redeemed(self):
        self.computer.post(reverse('game:join'), {'code': self.ticket_obj.code})
        self.computer.post(reverse('game:start'), {'nick': 'desktop'})
        loser = self.phone.post(reverse('game:start'), {'nick': 'phone'})
        self.assertRedirects(loser, reverse('game:home'))
        self.assertEqual(GameSession.objects.count(), 1)
        self.assertEqual(GameSession.objects.get().nick, 'desktop')

        third = Client()
        resp = third.post(reverse('game:join'), {'code': self.ticket_obj.code})
        self.assertContains(resp, 'not valid or has already been used')
        self.assertNotIn(views.TICKET_SESSION_KEY, third.session)

    def test_status_without_ticket_is_forbidden(self):
        resp = Client().get(reverse('game:ticket_status'))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json(), {'status': 'no_ticket'})

    @override_settings(PUBLIC_BASE_URL='https://play.example')
    def test_phone_shows_configured_computer_url(self):
        resp = self.phone.get(reverse('game:home'))
        self.assertContains(resp, 'https://play.example/')


class PlayTests(ViewTestCase):
    def test_play_without_game_redirects_home(self):
        self.assertRedirects(self.client.get(reverse('game:play')), reverse('game:home'),
                             fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse('game:done')), reverse('game:home'),
                             fetch_redirect_response=False)

    def test_play_shows_first_challenge_of_total(self):
        self.start()
        resp = self.client.get(reverse('game:play'))
        self.assertContains(resp, f'1 / {len(self.order)}')
        self.assertContains(resp, self.order[0].title)
        self.assertContains(resp, 'autocapitalize="off"')
        self.assertContains(resp, 'maxlength="300"')

    def test_play_after_reload_shows_last_attempt_escaped(self):
        self.start()
        self.run_command.return_value = result(output='<b>bold</b>\n', error='Test failed')
        self.command('echo "<b>bold</b>"')
        resp = self.client.get(reverse('game:play'))
        self.assertContains(resp, '&lt;b&gt;bold&lt;/b&gt;')
        self.assertNotContains(resp, '<b>bold</b>')
        self.assertContains(resp, 'Test failed')
        self.assertContains(resp, 'data-attempts="1"')

    def test_finishing_redirects_play_to_done(self):
        self.start()
        GameSession.objects.filter(pk=self.game().pk).update(current_slug=self.order[-1].slug)
        self.run_command.return_value = result(True)
        data = self.command('x').json()
        self.assertTrue(data['finished'])
        self.assertIsNone(data['challenge'])
        self.assertRedirects(self.client.get(reverse('game:play')), reverse('game:done'))
        self.assertRedirects(self.client.get(reverse('game:home')), reverse('game:done'))
        resp = self.client.get(reverse('game:done'))
        self.assertContains(resp, 'neo')
        self.assertContains(resp, f'1 / {len(self.order)}')


class CommandJsonTests(ViewTestCase):
    def setUp(self):
        super().setUp()
        self.start()

    def test_incorrect_run_returns_200_with_result_and_counters(self):
        self.run_command.return_value = result(output='file\n', error='Test failed')
        resp = self.command('ls')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['status'], 'ran')
        self.assertEqual((data['attempts'], data['solved'], data['finished']), (1, 0, False))
        self.assertEqual(data['result'], {'correct': False, 'output': 'file\n', 'message': 'Test failed'})
        self.assertEqual(data['challenge']['index'], 1)
        self.assertEqual(data['challenge']['total'], len(self.order))

    def test_correct_run_returns_next_challenge(self):
        self.run_command.return_value = result(True, output='hello world\n')
        data = self.command('echo hello world').json()
        self.assertEqual(data['result']['message'], 'Correct!')
        self.assertEqual(data['challenge']['index'], 2)
        self.assertEqual(data['challenge']['title'], self.order[1].title)
        self.assertIn('description_html', data['challenge'])

    def test_timed_out_run_message(self):
        self.run_command.return_value = result(timed_out=True)
        data = self.command('sleep 60').json()
        self.assertEqual(data['result']['message'], 'Timed out (5 s limit)')
        self.assertEqual(data['attempts'], 1)

    def assert_uncounted(self, resp, code, status):
        self.assertEqual(resp.status_code, code, status)
        data = resp.json()
        self.assertEqual(data['status'], status)
        self.assertEqual(data['attempts'], 0)
        self.assertNotIn('result', data)
        self.assertTrue(data['message'])
        self.assertIn('challenge', data)

    def test_rejected_commands_return_400(self):
        self.assert_uncounted(self.command('   '), 400, 'empty')
        self.assert_uncounted(self.command('x' * 301), 400, 'too_long')

    def test_sandbox_unavailable_returns_503(self):
        self.run_command.side_effect = sandbox.SandboxUnavailable('down')
        with self.assertLogs('game', level='WARNING'):
            self.assert_uncounted(self.command('ls'), 503, 'unavailable')

    def test_internal_error_returns_500_without_leaking_detail(self):
        self.run_command.return_value = result(error_internal='boom secret detail')
        with self.assertLogs('game', level='ERROR'):
            resp = self.command('ls')
        self.assert_uncounted(resp, 500, 'internal')
        self.assertNotIn('secret', resp.content.decode())

    def test_busy_returns_503(self):
        with mock.patch.object(services, 'submit_command',
                               return_value=services.SubmitOutcome('busy', None, self.game())):
            resp = self.command('ls')
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()['status'], 'busy')

    def test_finished_game_returns_409(self):
        GameSession.objects.filter(pk=self.game().pk).update(current_slug=self.order[-1].slug)
        self.run_command.return_value = result(True)
        self.command('x')
        resp = self.command('y')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()['status'], 'finished')

    def test_no_game_returns_403(self):
        resp = self.command('ls', client=Client())
        self.assertEqual(resp.status_code, 403)

    def test_invalid_json_body_is_400(self):
        resp = self.client.post(reverse('game:command'), 'not json', content_type='application/json')
        self.assertEqual(resp.status_code, 400)


    def test_oversized_body_is_rejected_before_parsing(self):
        body = json.dumps({'command': 'x', 'pad': 'y' * 5000})
        resp = self.client.post(reverse('game:command'), body, content_type='application/json')
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.json()['status'], 'bad_request')
        self.run_command.assert_not_called()

    def test_deeply_nested_json_is_400_not_500(self):
        # The size cap already stops this; lift it to prove the parser guard on its own.
        with mock.patch.object(views, 'MAX_BODY_BYTES', 10 ** 6):
            resp = self.client.post(reverse('game:command'), '[' * 200_000, content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['status'], 'bad_request')

    def test_max_length_command_is_accepted(self):
        resp = self.command('\u00e9' * sandbox.MAX_COMMAND_CHARS)  # 300 two-byte chars, JSON-escaped
        self.assertEqual(resp.status_code, 200)

    def test_lone_surrogate_in_command_is_bad_request_not_too_long(self):
        body = '{"command": "echo \\ud800"}'
        resp = self.client.post(reverse('game:command'), body, content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['status'], 'bad_request')
        self.run_command.assert_not_called()


class DockerDownTests(TestCase):
    """The real ``run_command``: only the Docker client fails, as when the daemon is down."""

    def setUp(self):
        catalog.clear_cache()
        services._reset_semaphore()
        reap = mock.patch.object(sandbox, 'reap_stale_once')
        reap.start()
        self.addCleanup(reap.stop)
        self.client.get(reverse('game:home'), {'t': services.issue_start_token()})
        self.client.post(reverse('game:start'), {'nick': 'neo'})

    def test_daemon_unreachable_returns_json_503_and_is_not_counted(self):
        for exc in (docker.errors.DockerException('no socket'), requests.exceptions.ConnectionError('gone')):
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch.object(sandbox, '_get_client', side_effect=exc):
                resp = self.client.post(reverse('game:command'), json.dumps({'command': 'ls'}),
                                        content_type='application/json')
                self.assertEqual(resp.status_code, 503)
                self.assertEqual(resp.json()['status'], 'unavailable')
        game = GameSession.objects.get(pk=self.client.session['game_id'])
        self.assertEqual(game.attempts, 0)

class CsrfTests(ViewTestCase):
    def test_command_post_requires_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        game = services.start_game('neo')
        session = client.session
        session['game_id'] = str(game.pk)
        session.save()
        self.assertEqual(client.get(reverse('game:play')).status_code, 200)
        self.assertEqual(self.command('ls', client=client).status_code, 403)
        token = client.cookies['csrftoken'].value
        self.assertEqual(self.command('ls', client=client, HTTP_X_CSRFTOKEN=token).status_code, 200)


class NoAnswerLinksTests(ViewTestCase):
    def player_pages(self):
        token = services.issue_start_token()
        pages = {'gate': self.client.get(reverse('game:home')),
                 'home': self.client.get(reverse('game:home'), {'t': token})}
        self.start()
        pages['play'] = self.client.get(reverse('game:play'))
        GameSession.objects.filter(pk=self.game().pk).update(current_slug=self.order[-1].slug)
        self.run_command.return_value = result(True)
        self.command('x')
        pages['done'] = self.client.get(reverse('game:done'))
        return pages

    def test_pages_contain_no_external_links_except_sponsor(self):
        for name, resp in self.player_pages().items():
            for href in EXTERNAL_HREF.findall(resp.content.decode()):
                self.assertEqual(href, ALLOWED_EXTERNAL, name)

    def test_guard_rejects_other_external_hosts(self):
        html = '<a class="x" href="https://example.com/">x</a><a href="//evil.test/">y</a>'
        self.assertEqual(EXTERNAL_HREF.findall(html), ['https://example.com/', '//evil.test/'])

    def test_player_pages_carry_sponsor_footer(self):
        for name, resp in self.player_pages().items():
            for needle in ('class="sponsor"', 'href="https://camlingroup.com/"',
                           'rel="noopener"', 'target="_blank"', 'alt="Camlin Group"'):
                self.assertContains(resp, needle, status_code=resp.status_code, msg_prefix=name)

    @override_settings(TRACKING_WEBSITE_ID=TRACKING_WEBSITE_ID)
    def test_player_pages_carry_configured_tracking_script_in_head_once(self):
        for name, resp in self.player_pages().items():
            html = resp.content.decode()
            self.assertEqual(html.count(TRACKING_WEBSITE_ID), 1, name)
            self.assertIn('defer src="/script.js"', html, name)
            self.assertIn('data-exclude-search="true"', html, name)
            self.assertLess(html.index(TRACKING_WEBSITE_ID), html.index('</head>'), name)

    @override_settings(TRACKING_WEBSITE_ID='')
    def test_player_pages_omit_tracking_script_when_unconfigured(self):
        for name, resp in self.player_pages().items():
            self.assertNotIn('data-website-id=', resp.content.decode(), name)

    @override_settings(TRACKING_WEBSITE_ID=TRACKING_WEBSITE_ID)
    def test_start_error_pages_do_not_carry_tracking_script(self):
        token = services.issue_start_token()
        self.client.get(reverse('game:home'), {'t': token})
        responses = (
            self.client.post(reverse('game:start'), {'nick': ' '}),
            Client().post(reverse('game:start'), {'nick': 'neo'}),
        )
        for resp in responses:
            self.assertNotIn(TRACKING_WEBSITE_ID, resp.content.decode())

    def test_logo_is_a_collected_static_file(self):
        self.assertTrue(finders.find('game/camlin-logo.png'))


class DescriptionFilterTests(TestCase):
    def test_html_is_escaped(self):
        out = render_description('Run <script>alert(1)</script> & stop')
        self.assertNotIn('<script>', out)
        self.assertIn('&lt;script&gt;', out)
        self.assertIn('&amp;', out)

    def test_backticks_become_code_and_newlines_br(self):
        out = render_description('Use `ls -l` here.\nThen `pwd`.')
        self.assertEqual(out, 'Use <code>ls -l</code> here.<br>Then <code>pwd</code>.')

    def test_fenced_blocks_become_pre_without_inner_markup(self):
        out = render_description('Example:\n```\na `b` <c>\nline2\n```\nDone')
        self.assertEqual(out, 'Example:<pre>a `b` &lt;c&gt;\nline2</pre>Done')

    def test_every_catalog_description_renders(self):
        for ch in catalog.all_main_set():
            self.assertNotIn('```', render_description(ch.description), ch.slug)


class TimeLimitViewTests(ViewTestCase):
    def expire(self):
        GameSession.objects.filter(pk=self.game().pk).update(
            deadline_at=timezone.now() - timedelta(seconds=1))

    def test_expired_game_redirects_to_done_and_done_renders(self):
        self.start()
        self.expire()
        self.assertRedirects(self.client.get(reverse('game:play')), reverse('game:done'))
        self.assertRedirects(self.client.get(reverse('game:home')), reverse('game:done'))
        self.assertEqual(self.client.get(reverse('game:done')).status_code, 200)

    def test_late_command_is_409_time_up(self):
        self.start()
        self.expire()
        resp = self.command('ls')
        self.assertEqual(resp.status_code, 409)
        data = resp.json()
        self.assertEqual((data['status'], data['finished'], data['message']), ('time_up', True, "Time's up."))
        self.assertEqual(data['remaining_ms'], 0)
        self.run_command.assert_not_called()

    def test_command_response_carries_remaining_ms_but_no_game_does_not(self):
        self.start()
        data = self.command('ls').json()
        self.assertAlmostEqual(data['remaining_ms'], 300_000, delta=5_000)
        anon = self.command('ls', client=Client())
        self.assertEqual(anon.json()['status'], 'no_game')
        self.assertNotIn('remaining_ms', anon.json())


class TimerAndDoneUiTests(ViewTestCase):
    def expire(self):
        GameSession.objects.filter(pk=self.game().pk).update(
            deadline_at=timezone.now() - timedelta(seconds=1))

    def test_state_endpoint_fresh_game(self):
        self.start()
        data = self.client.get(reverse('game:state')).json()
        self.assertFalse(data['finished'])
        self.assertAlmostEqual(data['remaining_ms'], 300_000, delta=5_000)

    def test_state_endpoint_expired_game(self):
        self.start()
        self.expire()
        self.assertEqual(self.client.get(reverse('game:state')).json(), {'remaining_ms': 0, 'finished': True})

    def test_state_endpoint_without_game_is_no_game(self):
        resp = self.client.get(reverse('game:state'))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()['status'], 'no_game')

    def test_state_endpoint_is_get_only(self):
        self.start()
        self.assertEqual(self.client.post(reverse('game:state')).status_code, 405)

    def test_play_renders_timer(self):
        self.start()
        resp = self.client.get(reverse('game:play'))
        self.assertContains(resp, 'id="timer"')
        ms = int(re.search(r'data-remaining-ms="(\d+)"', resp.content.decode()).group(1))
        self.assertAlmostEqual(ms, 300_000, delta=5_000)
        self.assertRegex(resp.content.decode(), r'id="timer"[^>]*>[45]:[0-5]\d<')

    def test_done_says_time_is_up(self):
        self.start()
        self.expire()
        self.assertContains(self.client.get(reverse('game:done')), "Time's up!")

    def test_done_says_all_solved(self):
        self.start()
        GameSession.objects.filter(pk=self.game().pk).update(
            current_slug=None, finished_at=timezone.now(), solved=len(self.order))
        self.assertContains(self.client.get(reverse('game:done')), 'All challenges solved!')

    def test_done_is_neutral_when_catalog_ran_out(self):
        self.start()
        GameSession.objects.filter(pk=self.game().pk).update(
            current_slug=None, finished_at=timezone.now(), solved=0)
        resp = self.client.get(reverse('game:done'))
        self.assertContains(resp, 'Finished!')
        self.assertNotContains(resp, 'All challenges solved!')
        self.assertNotContains(resp, 'up!')

    def test_home_duration_follows_setting(self):
        home = reverse('game:home')
        token = services.issue_start_token()
        self.assertContains(self.client.get(home, {'t': token}), '5 minutes')
        with override_settings(GAME_DURATION_S=120):
            self.assertContains(self.client.get(home, {'t': token}), '2 minutes')


class SummaryTests(ViewTestCase):
    CODE = '987654'

    def finish(self, solved=1, attempts=3):
        self.start()
        GameSession.objects.filter(pk=self.game().pk).update(
            code=self.CODE, solved=solved, attempts=attempts, last_solved_at=timezone.now(),
            finished_at=timezone.now())

    def test_done_shows_stats_place_and_code(self):
        self.finish(solved=1, attempts=3)
        html = self.client.get(reverse('game:done')).content.decode()
        self.assertIn('#1 of 1', html)
        self.assertRegex(html, r'Attempts</span>\s*<span class="leader"></span>\s*<strong>3</strong>')
        self.assertRegex(html, r'class="prize-code"[^>]*>\s*987654\s*<')

    def test_code_is_not_leaked_on_play_or_json(self):
        self.start()
        GameSession.objects.filter(pk=self.game().pk).update(code=self.CODE)
        self.assertNotContains(self.client.get(reverse('game:play')), self.CODE)
        self.assertNotContains(self.command('ls'), self.CODE)
        self.assertNotContains(self.client.get(reverse('game:state')), self.CODE)

    def test_done_redirects_still_hold(self):
        self.assertRedirects(self.client.get(reverse('game:done')), reverse('game:home'),
                             fetch_redirect_response=False)
        self.start()
        self.assertRedirects(self.client.get(reverse('game:done')), reverse('game:play'))

    def test_hidden_game_done_shows_not_ranked_and_code(self):
        self.finish(solved=1, attempts=3)
        services.hide_game(self.game().pk)
        resp = self.client.get(reverse('game:done'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'not ranked')
        self.assertContains(resp, self.CODE)
