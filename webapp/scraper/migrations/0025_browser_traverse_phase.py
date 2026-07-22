# Generated for browser_traverse phase — replaces the old
# navigation_explore / navigation_agent / navigation_synthesize phases with a
# single browser_traverse phase.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scraper', '0024_joblisting_date_reliable'),
    ]

    operations = [
        migrations.AlterField(
            model_name='step',
            name='phase',
            field=models.CharField(choices=[('accessibility_check', 'Accessibility Check'), ('site_analysis', 'Site Analysis'), ('browser_traverse', 'Browser Navigation'), ('navigation_skill_review', 'Navigation Skill Review'), ('navigation_analysis', 'Navigation Analysis'), ('content_analysis', 'Content Analysis'), ('product_analysis', 'Product Analysis'), ('scraper_analysis', 'Scraper Analysis'), ('code_generation', 'Code Generation'), ('code_review', 'Code Review'), ('testing', 'Testing'), ('field_confirmation', 'Field Confirmation'), ('execution', 'Execution'), ('cleanup', 'Cleanup'), ('skill_learning', 'Skill Learning'), ('dagster_converter', 'Dagster Conversion'), ('store_job_listings', 'Store Listings')], max_length=50),
        ),
        migrations.AlterField(
            model_name='agentplayground',
            name='agent_name',
            field=models.CharField(choices=[('site_analyzer', 'Site Analyzer'), ('browser_traverse', 'Browser Navigation'), ('nav_skill_review', 'Navigation Skill Review'), ('product_analyzer', 'Product Analyzer'), ('scraper_analyzer', 'Scraper Analyzer'), ('code_writer', 'Code Writer'), ('code_tester', 'Code Tester'), ('cleanup', 'Cleanup')], max_length=50),
        ),
    ]
