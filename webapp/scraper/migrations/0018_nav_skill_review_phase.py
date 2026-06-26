# Generated for nav_skill_review phase addition

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scraper', '0017_agent_playground_search_criteria'),
    ]

    operations = [
        migrations.AlterField(
            model_name='step',
            name='phase',
            field=models.CharField(choices=[('accessibility_check', 'Accessibility Check'), ('site_analysis', 'Site Analysis'), ('navigation_explore', 'Navigation Explore'), ('navigation_synthesize', 'Navigation Synthesis'), ('navigation_skill_review', 'Navigation Skill Review'), ('navigation_analysis', 'Navigation Analysis'), ('content_analysis', 'Content Analysis'), ('product_analysis', 'Product Analysis'), ('scraper_analysis', 'Scraper Analysis'), ('code_generation', 'Code Generation'), ('testing', 'Testing'), ('field_confirmation', 'Field Confirmation'), ('execution', 'Execution'), ('cleanup', 'Cleanup'), ('skill_learning', 'Skill Learning')], max_length=50),
        ),
        migrations.AlterField(
            model_name='agentplayground',
            name='agent_name',
            field=models.CharField(choices=[('site_analyzer', 'Site Analyzer'), ('navigation_explore', 'Navigation Explore'), ('navigation_synthesize', 'Navigation Synthesize'), ('nav_skill_review', 'Navigation Skill Review'), ('product_analyzer', 'Product Analyzer'), ('scraper_analyzer', 'Scraper Analyzer'), ('code_writer', 'Code Writer'), ('code_tester', 'Code Tester'), ('cleanup', 'Cleanup')], max_length=50),
        ),
    ]
