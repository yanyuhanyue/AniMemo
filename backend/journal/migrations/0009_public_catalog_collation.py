"""Preserve Intl zh-CN equality before stable catalogue keyset tie breakers."""
from typing import ClassVar

from django.db import migrations


def create_public_collation(apps, schema_editor):
    del apps
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            'CREATE COLLATION IF NOT EXISTS "animemo_public_zh_cn_v1" '
            "(provider = icu, locale = 'zh-Hans-CN', deterministic = false)"
        )
        cursor.execute(
            "SELECT collprovider, collisdeterministic, colliculocale "
            "FROM pg_collation WHERE collname = %s AND collnamespace = current_schema()::regnamespace",
            ["animemo_public_zh_cn_v1"],
        )
        if cursor.fetchone() != ("i", False, "zh-Hans-CN"):
            raise RuntimeError("Public catalogue collation has an incompatible definition")


class Migration(migrations.Migration):
    dependencies: ClassVar[list] = [("journal", "0008_journalmediareference")]
    operations: ClassVar[list] = [migrations.RunPython(create_public_collation, migrations.RunPython.noop)]
