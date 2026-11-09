from django import forms
from django.conf import settings
from .validation import validate_upload


class UploadForm(forms.Form):
    video = forms.FileField(
        label='Відеофайл',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        maximum = settings.MAX_UPLOAD_BYTES / (1024 * 1024)
        formats = ', '.join(settings.ALLOWED_VIDEO_EXTENSIONS)
        self.fields['video'].help_text = f'До {maximum:g} МБ, до {settings.MAX_VIDEO_SECONDS} с. Формати: {formats}.'
        self.fields['video'].widget.attrs.update({
            'class': 'visually-hidden',
            'accept': ','.join(f'.{extension}' for extension in settings.ALLOWED_VIDEO_EXTENSIONS),
        })

    def clean_video(self):
        video = self.cleaned_data['video']
        validate_upload(video)
        return video
