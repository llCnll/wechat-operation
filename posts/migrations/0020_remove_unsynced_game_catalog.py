from django.db import migrations


def remove_unsynced_game_catalog_entries(apps, schema_editor):
    database = schema_editor.connection.alias
    managed_games = apps.get_model("posts", "ManagedGame")
    active_scopes = apps.get_model("posts", "ActiveGameScope")
    appids = list(
        managed_games.objects.using(database)
        .filter(last_synced_at__isnull=True)
        .values_list("appid", flat=True)
    )
    if not appids:
        return
    active_scopes.objects.using(database).filter(active_game_id__in=appids).delete()
    managed_games.objects.using(database).filter(appid__in=appids).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("posts", "0019_add_comment_export_tasks"),
    ]

    operations = [
        migrations.RunPython(remove_unsynced_game_catalog_entries, migrations.RunPython.noop),
    ]
