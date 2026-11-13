from django.db import models


class SubtitleLanguage(models.TextChoices):
    AUTO = '', 'Визначити автоматично'
    UKRAINIAN = 'uk', 'Українська'
    ENGLISH = 'en', 'Англійська'
    POLISH = 'pl', 'Польська'
    GERMAN = 'de', 'Німецька'
    FRENCH = 'fr', 'Французька'
    SPANISH = 'es', 'Іспанська'
    ITALIAN = 'it', 'Італійська'
    CZECH = 'cs', 'Чеська'
    PORTUGUESE = 'pt', 'Португальська'
    RUSSIAN = 'ru', 'Російська'
