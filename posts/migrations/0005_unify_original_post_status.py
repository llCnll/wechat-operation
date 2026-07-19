from django.db import migrations


NORMAL = 2
BLOCKED = 4
SUNK = 8
DELETED = 128
MODERATION_MASK = BLOCKED | SUNK


def migrate_deleted_status(apps, schema_editor):
    OriginalPost = apps.get_model("posts", "OriginalPost")
    for post in OriginalPost.objects.filter(business_deleted=True).iterator():
        status = post.status if post.status is not None else NORMAL
        post.status = (int(status) & MODERATION_MASK) | DELETED
        post.save(update_fields=["status"])


def restore_deleted_status(apps, schema_editor):
    OriginalPost = apps.get_model("posts", "OriginalPost")
    for post in OriginalPost.objects.iterator():
        status = post.status if post.status is not None else NORMAL
        post.business_deleted = (int(status) & DELETED) != 0
        post.save(update_fields=["business_deleted"])


class Migration(migrations.Migration):
    dependencies = [("posts", "0004_originalpost_ballot_text")]

    operations = [
        migrations.RunPython(migrate_deleted_status, restore_deleted_status),
        migrations.RemoveField(model_name="originalpost", name="business_deleted"),
        migrations.RemoveField(model_name="originalpost", name="deleted_at"),
    ]
