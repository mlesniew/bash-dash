from django.urls import path

from . import staff_views, views

app_name = 'game'

urlpatterns = [
    path('', views.home, name='home'),
    path('join', views.join, name='join'),
    path('start', views.start, name='start'),
    path('ticket/status', views.ticket_status, name='ticket_status'),
    path('play', views.play, name='play'),
    path('play/command', views.command, name='command'),
    path('play/state', views.state, name='state'),
    path('done', views.done, name='done'),
    path('staff', staff_views.lookup, name='staff_lookup'),
    path('staff/hall', staff_views.hall, name='staff_hall'),
    path('staff/hall/board', staff_views.hall_board, name='staff_hall_board'),
    path('staff/moderate', staff_views.moderate, name='staff_moderate'),
    path('staff/hide', staff_views.hide, name='staff_hide'),
    path('staff/unhide', staff_views.unhide, name='staff_unhide'),
    path('staff/token-ttl', staff_views.set_token_ttl, name='staff_token_ttl'),
    path('staff/correct-answer-bonus', staff_views.set_correct_answer_bonus,
         name='staff_correct_answer_bonus'),
    path('staff/prize', staff_views.give_prize, name='staff_prize'),
]
