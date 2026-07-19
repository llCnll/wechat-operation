from django.db import migrations, models


def add_default_game(apps, schema_editor):
    apps.get_model("posts", "ManagedGame").objects.get_or_create(
        appid="wx6d2454a8afcd9706", defaults={"name": "城主别慌张"}
    )


class Migration(migrations.Migration):
    dependencies = [("posts", "0005_unify_original_post_status")]

    operations = [
        migrations.CreateModel(
            name="ManagedGame",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("appid", models.CharField(max_length=128, unique=True)),
                ("name", models.CharField(max_length=255)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ("name", "appid")},
        ),
        migrations.RunPython(add_default_game, migrations.RunPython.noop),
    ]
