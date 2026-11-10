from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("nuget", "0008_nugetrepository_retain_package_versions")]
    operations = [migrations.AddField(
        model_name="nugetpackagecontent", name="published",
        field=models.DateTimeField(blank=True, null=True),
    )]
