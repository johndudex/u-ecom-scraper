import json

from django import forms

from .models import ScrapeJob, Site


class ScrapeJobForm(forms.ModelForm):
    url = forms.URLField(
        label="Website URL",
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "https://www.example.com",
        }),
    )
    product_url = forms.URLField(
        label="Product Listing URL (optional)",
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "https://www.example.com/shop/all (auto-discovered if empty)",
        }),
    )
    currency = forms.CharField(
        label="Target Currency (optional)",
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "USD (auto-detect if empty)",
        }),
    )

    class Meta:
        model = ScrapeJob
        fields = ["url", "product_url", "currency"]


class SiteForm(forms.ModelForm):
    input_urls_json = forms.CharField(
        label="Item URLs",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 6,
                "placeholder": '["https://www.example.com/item/1", "https://www.example.com/item/2"]',
            }
        ),
        help_text="Paste a JSON array of URLs, or upload a JSON file below.",
    )
    input_urls_file = forms.FileField(
        label="Upload URLs JSON",
        required=False,
        help_text="JSON file containing an array of URLs.",
    )
    site_type = forms.ChoiceField(
        label="Site Type",
        required=False,
        widget=forms.Select(attrs={
            "class": "form-control",
        }),
    )

    class Meta:
        model = Site
        fields = ["url", "sample_url", "currency"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = kwargs.get("instance")
        if instance and instance.input_urls:
            self.fields["input_urls_json"].initial = json.dumps(
                instance.input_urls, indent=2
            )
        try:
            from src.content_types import SITE_TYPE_CHOICES
            self.fields["site_type"].choices = SITE_TYPE_CHOICES
        except ImportError:
            pass

    def clean_input_urls_json(self):
        value = self.cleaned_data.get("input_urls_json", "").strip()
        if not value:
            return []
        try:
            urls = json.loads(value)
            if not isinstance(urls, list):
                raise forms.ValidationError("Must be a JSON array of URLs.")
            for i, url in enumerate(urls):
                if not isinstance(url, str) or not url.strip():
                    raise forms.ValidationError(
                        f"Item {i + 1} is not a valid URL string."
                    )
            return [u.strip() for u in urls if u.strip()]
        except json.JSONDecodeError:
            raise forms.ValidationError("Invalid JSON format.")

    def clean_input_urls_file(self):
        f = self.cleaned_data.get("input_urls_file")
        if not f:
            return None
        try:
            data = json.loads(f.read().decode("utf-8"))
            if not isinstance(data, list):
                raise forms.ValidationError("File must contain a JSON array of URLs.")
            return [u.strip() for u in data if isinstance(u, str) and u.strip()]
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise forms.ValidationError("Invalid JSON file.")

    def clean_url(self):
        url = self.cleaned_data.get("url", "").strip().rstrip("/")
        return url

    def clean_sample_url(self):
        url = self.cleaned_data.get("sample_url", "").strip()
        return url

    def save(self, commit=True):
        instance = super().save(commit=False)
        urls = self.cleaned_data.get("input_urls_json", [])
        file_urls = self.cleaned_data.get("input_urls_file")
        if file_urls:
            urls = file_urls
        instance.input_urls = urls

        if instance.slug:
            pass
        else:
            from urllib.parse import urlparse

            parsed = urlparse(instance.url)
            domain = parsed.netloc.lower().replace("www.", "").split(":")[0]
            slug = ""
            for ch in domain:
                if ch.isalnum():
                    slug += ch
                elif ch in (".", "-"):
                    slug += "-"
                else:
                    slug += "-"
            instance.slug = slug.strip("-")

        if commit:
            instance.save()
        return instance
