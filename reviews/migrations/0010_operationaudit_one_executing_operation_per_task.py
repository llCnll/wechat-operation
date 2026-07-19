from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reviews", "0009_ai_automated_moderation")]

    operations = [
        migrations.AddConstraint(
            model_name="operationaudit",
            constraint=models.UniqueConstraint(
                condition=models.Q(result_code="EXECUTING"),
                fields=("task",),
                name="one_executing_operation_per_task",
            ),
        )
    ]
