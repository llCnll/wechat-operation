from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reviews", "0011_add_automation_eligibility_evidence")]

    operations = [
        migrations.AddField(
            model_name="replydraft",
            name="faq_rule_id",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="replydraft",
            name="faq_version",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="replydraft",
            name="selected_style",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AddField(
            model_name="replydraft",
            name="style_variants",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
