from typing import ClassVar

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies: ClassVar = [("journal", "0010_public_catalog_collation")]

    operations: ClassVar = [
        migrations.AddIndex(
            model_name="journalentry",
            index=models.Index(fields=["poster_file"], name="journal_poster_file_idx"),
        ),
    ]
