from django import template
from django.template.defaultfilters import filesizeformat

register = template.Library()


@register.filter
def get_item(dictionary, key):
    return dictionary.get(key, False)
