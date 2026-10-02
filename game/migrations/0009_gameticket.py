from django.db import migrations, models
import django.db.models.deletion
import game.models


class Migration(migrations.Migration):

    dependencies = [
        ('game', '0008_alter_gamesession_nick'),
    ]

    operations = [
        migrations.CreateModel(
            name='GameTicket',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(default=game.models.generate_ticket_code, editable=False, max_length=5, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('game', models.OneToOneField(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='ticket', to='game.gamesession')),
            ],
        ),
    ]
